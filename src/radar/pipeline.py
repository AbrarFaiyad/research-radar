"""Daily pipeline runner: stage isolation, a cross-process lock, and JSON run records.

Each stage runs even if an earlier one failed, so a logged-out LinkedIn session still lets the
backlog digest and the email go out. Every run leaves `data/runs/<timestamp>.json`, which the
control panel and `radar status` read.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config

STALE_LOCK_S = 3 * 3600


class LockHeld(RuntimeError):
    pass


class RunLock:
    """File lock so the scheduled run, the panel, and the CLI never run jobs at once."""

    def __init__(self, cfg: Config, owner: str = "cli"):
        self.path = cfg.data_dir / "run.lock"
        self.owner = owner

    def holder(self) -> dict | None:
        try:
            info = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        if time.time() - info.get("since", 0) > STALE_LOCK_S:
            return None
        return info

    def __enter__(self) -> RunLock:  # noqa: PYI034
        self.path.parent.mkdir(parents=True, exist_ok=True)
        held = self.holder()
        if held and held.get("pid") != os.getpid():
            raise LockHeld(f"another run is active ({held.get('owner')}, pid {held.get('pid')})")
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "owner": self.owner, "since": time.time()}),
            encoding="utf-8",
        )
        return self

    def __exit__(self, *exc) -> None:
        held = self.holder()
        if held is None or held.get("pid") == os.getpid():
            self.path.unlink(missing_ok=True)


@dataclass
class StageResult:
    name: str
    ok: bool
    seconds: float
    detail: dict = field(default_factory=dict)
    error: str | None = None


@dataclass
class RunRecord:
    started: str
    finished: str = ""
    ok: bool = True
    stages: list[StageResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def run_stages(
    stages: dict[str, Callable[[], dict]], log: Callable[[str], None] = print
) -> RunRecord:
    rec = RunRecord(started=dt.datetime.now().isoformat(timespec="seconds"))
    for name, fn in stages.items():
        t0 = time.monotonic()
        try:
            detail = fn() or {}
            res = StageResult(name, True, round(time.monotonic() - t0, 1), detail)
        except (Exception, SystemExit) as e:  # noqa: BLE001 - isolate stages by design
            msg = str(e) or e.__class__.__name__
            log(f"[{name}] failed: {msg}")
            res = StageResult(
                name,
                False,
                round(time.monotonic() - t0, 1),
                {"trace": traceback.format_exc(limit=3)},
                msg[:2000],
            )
        rec.stages.append(res)
        log(f"[{name}] {'ok' if res.ok else 'FAILED'} in {res.seconds}s")
    rec.ok = all(s.ok for s in rec.stages)
    rec.finished = dt.datetime.now().isoformat(timespec="seconds")
    return rec


def save_run(cfg: Config, rec: RunRecord) -> Path:
    runs = cfg.data_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / (rec.started.replace(":", "-") + ".json")
    path.write_text(json.dumps(rec.to_dict(), indent=1), encoding="utf-8")
    return path


def latest_run(cfg: Config) -> dict | None:
    runs = sorted((cfg.data_dir / "runs").glob("*.json"))
    if not runs:
        return None
    try:
        return json.loads(runs[-1].read_text(encoding="utf-8"))
    except ValueError:
        return None
