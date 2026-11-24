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

STALE_LOCK_S = 6 * 3600


class LockHeld(RuntimeError):
    pass


class StageSkipped(Exception):  # noqa: N818 - a signal, not an error
    """Raised by a stage that chose not to run; recorded as ok and skipped, with the reason."""


def pid_alive(pid: int) -> bool:
    """True if a process with this id is running. Never signals the process: on Windows,
    os.kill(pid, 0) would terminate it, so query it through the Win32 API instead."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    """Cross-process lock so the scheduled run, the panel, and the CLI never run jobs at once.

    The lock file is created atomically (O_CREAT | O_EXCL), so two processes cannot both take
    it. A lock is stale when its process has exited (crash, cancel) or it is older than six
    hours (a guard against process-id reuse); a stale lock is removed and acquisition retried.
    """

    def __init__(self, cfg: Config, owner: str = "cli"):
        self.path = cfg.data_dir / "run.lock"
        self.owner = owner

    def holder(self) -> dict | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            info = json.loads(raw)
        except ValueError:
            # Another process may be mid-write; trust a fresh file, discard an old broken one.
            try:
                fresh = time.time() - self.path.stat().st_mtime < 10
            except FileNotFoundError:
                return None
            return {"owner": "starting", "pid": 0, "since": time.time()} if fresh else None
        if time.time() - info.get("since", 0) > STALE_LOCK_S:
            return None
        if not pid_alive(int(info.get("pid", 0))):
            return None
        return info

    def _create(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "owner": self.owner, "since": time.time()}, f)
        return True

    def __enter__(self) -> RunLock:  # noqa: PYI034
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            if self._create():
                return self
            held = self.holder()
            if held and held.get("pid") == os.getpid():
                return self  # re-entrant within one process
            if held:
                raise LockHeld(
                    f"another run is active ({held.get('owner')}, pid {held.get('pid')})"
                )
            self.path.unlink(missing_ok=True)  # stale: owner gone
        raise LockHeld("could not take the run lock")

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
    optional: bool = False
    skipped: bool = False


@dataclass
class RunRecord:
    started: str
    finished: str = ""
    ok: bool = True
    stages: list[StageResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def run_stages(
    stages: dict[str, Callable[[], dict]],
    log: Callable[[str], None] = print,
    optional: set[str] | frozenset[str] = frozenset(),
) -> RunRecord:
    """Run every stage; the run is ok when all non-optional stages are ok."""
    rec = RunRecord(started=dt.datetime.now().isoformat(timespec="seconds"))
    for name, fn in stages.items():
        t0 = time.monotonic()
        opt = name in optional
        try:
            detail = fn() or {}
            res = StageResult(name, True, round(time.monotonic() - t0, 1), detail, optional=opt)
        except StageSkipped as e:
            res = StageResult(
                name,
                True,
                round(time.monotonic() - t0, 1),
                error=(str(e) or "skipped")[:2000],
                optional=opt,
                skipped=True,
            )
        except (Exception, SystemExit) as e:  # noqa: BLE001 - isolate stages by design
            msg = str(e) or e.__class__.__name__
            log(f"[{name}] failed: {msg}")
            res = StageResult(
                name,
                False,
                round(time.monotonic() - t0, 1),
                {"trace": traceback.format_exc(limit=3)},
                msg[:2000],
                optional=opt,
            )
        rec.stages.append(res)
        if res.skipped:
            log(f"[{name}] skipped: {res.error}")
        else:
            log(f"[{name}] {'ok' if res.ok else 'FAILED'} in {res.seconds}s")
    rec.ok = all(s.ok for s in rec.stages if not s.optional)
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
