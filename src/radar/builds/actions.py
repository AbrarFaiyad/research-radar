"""Start a build on GitHub Actions, find its run, wait for it and map its result.

Only the rendered prompt (through a temp file, never argv) and the folder name are sent. Every
`gh` call uses an argv list and an allowlisted environment without host tokens. Messages raised
in ActionsError and returned as reasons are fixed phrases: never gh output, stderr or JSON.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from radar.config import Config

GH_TIMEOUT_S = 60
WORKFLOW = "build.yml"
POLL_S = 30
RETRIES = 4
RETRY_WAIT_S = 5
CLOCK_SKEW_S = 60
RESULT_STATUSES = ("published", "did_not_run")
ENV_ALLOW = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
)
GH_FALLBACK = "C:/Program Files/GitHub CLI/gh.exe"

GhFn = Callable[..., "subprocess.CompletedProcess[str]"]


class ActionsError(Exception):
    """A step failed; the message is a fixed phrase."""


def url(cfg: Config, folder: str) -> str:
    return f"https://github.com/{cfg.builds.repo}/tree/main/{folder}"


def _gh_config_dir() -> str | None:
    """Where the host's gh keeps its login. Only this path is handed on, never a token."""
    host = os.environ
    if host.get("GH_CONFIG_DIR"):
        cand = Path(host["GH_CONFIG_DIR"])
    elif host.get("XDG_CONFIG_HOME"):
        cand = Path(host["XDG_CONFIG_HOME"]) / "gh"
    elif os.name == "nt":
        cand = Path(host.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "GitHub CLI"
    else:
        cand = Path.home() / ".config" / "gh"
    return str(cand) if cand.is_dir() else None


def gh_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_ALLOW}
    gh_dir = _gh_config_dir()
    if gh_dir:
        env["GH_CONFIG_DIR"] = gh_dir
    env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
    return env


def gh(
    args: list[str], *, input: str | None = None, timeout: float = GH_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("gh") or GH_FALLBACK
    return subprocess.run(  # noqa: S603 - argv list, no shell
        [exe, *args],
        input=input,
        env=gh_env(),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _call(gh_fn: GhFn, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        proc = gh_fn(args)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc if proc.returncode == 0 else None


def _json(gh_fn: GhFn, args: list[str]) -> Any:
    proc = _call(gh_fn, args)
    if proc is None:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def dispatch(cfg: Config, folder: str, prompt: str, gh: GhFn = gh) -> None:
    fail = ActionsError("could not start the workflow")
    path: str | None = None
    try:
        fd, path = tempfile.mkstemp(prefix="radar-prompt-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(prompt)
        args = [
            "workflow",
            "run",
            WORKFLOW,
            "-R",
            cfg.builds.repo,
            "-f",
            f"folder={folder}",
            "-F",
            f"prompt=@{path}",
        ]
        if _call(gh, args) is None:
            raise fail
    except OSError:
        raise fail from None
    finally:
        if path:
            with contextlib.suppress(OSError):
                os.unlink(path)


def _parse_time(text: object) -> dt.datetime | None:
    try:
        when = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


def find_run(
    cfg: Config,
    folder: str,
    since: dt.datetime,
    gh: GhFn = gh,
    attempts: int = 10,
    wait: float = 6,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """The oldest run of this folder at or after `since`, else the newest within the skew."""
    oldest = since - dt.timedelta(seconds=CLOCK_SKEW_S)
    args = [
        "run",
        "list",
        "-R",
        cfg.builds.repo,
        "--workflow",
        WORKFLOW,
        "--json",
        "databaseId,displayTitle,createdAt",
        "--limit",
        "20",
    ]
    for attempt in range(attempts):
        data = _json(gh, args)
        after: list[tuple[dt.datetime, int]] = []
        skew: list[tuple[dt.datetime, int]] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or item.get("displayTitle") != f"build {folder}":
                continue
            created = _parse_time(item.get("createdAt"))
            run_id = item.get("databaseId")
            if created is None or created < oldest or not isinstance(run_id, int):
                continue
            (after if created >= since else skew).append((created, run_id))
        if after:
            return min(after)[1]
        if skew:
            return max(skew)[1]
        if attempt < attempts - 1:
            sleep(wait)
    raise ActionsError("could not find the workflow run")


def wait_run(
    cfg: Config,
    run_id: int,
    limit_s: float,
    gh: GhFn = gh,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    args = ["run", "view", str(run_id), "-R", cfg.builds.repo, "--json", "status,conclusion"]
    start = clock()
    reached = False
    while True:
        data = _json(gh, args)
        if isinstance(data, dict):
            reached = True
            if data.get("status") == "completed":
                return str(data.get("conclusion") or "failure")
        if clock() - start >= limit_s:
            if not reached:
                raise ActionsError("could not reach GitHub")
            return "building"
        sleep(POLL_S)


def _retry_json(
    gh_fn: GhFn, args: list[str], sleep: Callable[[float], None], attempts: int = RETRIES
) -> Any:
    for attempt in range(attempts):
        data = _json(gh_fn, args)
        if data is not None:
            return data
        if attempt < attempts - 1:
            sleep(RETRY_WAIT_S)
    return None


def _read_result(
    cfg: Config, folder: str, run_id: int, gh_fn: GhFn, sleep: Callable[[float], None]
) -> str | None:
    path = f"repos/{cfg.builds.repo}/contents/{folder}/build-result.json"
    data = _retry_json(gh_fn, ["api", path], sleep)
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        return None
    try:
        result = json.loads(base64.b64decode(data["content"], validate=False).decode("utf-8"))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(result, dict) or result.get("folder") != folder:
        return None
    got = result.get("run_id")
    if isinstance(got, bool) or got != run_id:
        return None
    status = result.get("status")
    return status if status in RESULT_STATUSES else None


def outcome(
    cfg: Config,
    run_id: int,
    folder: str,
    gh: GhFn = gh,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    """Map a finished run to (status, reason). Reasons are fixed phrases."""
    data = _retry_json(
        gh,
        ["run", "view", str(run_id), "-R", cfg.builds.repo, "--json", "status,conclusion,jobs"],
        sleep,
    )
    if not isinstance(data, dict):
        return "failed", "could not read the workflow run"
    jobs = {
        j.get("name"): j.get("conclusion") for j in data.get("jobs") or [] if isinstance(j, dict)
    }
    build, publish = jobs.get("build"), jobs.get("publish")
    if publish == "success":
        status = _read_result(cfg, folder, run_id, gh, sleep)
        if status is None:
            return "failed", "unexpected build result"
        return status, "build did not run" if status == "did_not_run" else ""
    if "cancelled" in (data.get("conclusion"), build, publish):
        return "failed", "workflow cancelled"
    if publish not in (None, "skipped"):
        return "blocked", "leak scan blocked the build"
    if build not in (None, "success"):
        return "failed", "build job failed"
    return "failed", "unexpected build result"
