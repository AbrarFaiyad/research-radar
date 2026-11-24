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
import re
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
SECRET_NAME = "CLAUDE_CODE_OAUTH_TOKEN"
GONE = "workflow run no longer exists"
UNREADABLE = "could not read the workflow run"
GONE_RE = re.compile(r"\b404\b|not found", re.IGNORECASE)
# Step names of the publish job in template/build.yml, read to tell the failures apart.
NO_OUTPUT_STEPS = ("Download artifact", "Check artifact")
SCAN_BLOCK_STEP = "Leak scan blocked the build"
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


def _real_gh() -> GhFn:
    """The module's gh, looked up when called, so tests can replace it."""
    return globals()["gh"]


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


def secret_names(repo: str, gh_fn: GhFn | None = None) -> set[str] | None:
    """Names of the repo's Actions secrets, or None when they could not be listed."""
    data = _json(gh_fn or gh, ["secret", "list", "-R", repo, "--json", "name"])
    if not isinstance(data, list):
        return None
    return {str(x.get("name")) for x in data if isinstance(x, dict)}


def dispatch(cfg: Config, folder: str, prompt: str, gh: GhFn | None = None) -> None:
    gh = gh or _real_gh()
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
    gh: GhFn | None = None,
    attempts: int = 10,
    wait: float = 6,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """The oldest run of this folder at or after `since`, else the newest within the skew."""
    gh = gh or _real_gh()
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


def _view(gh_fn: GhFn, args: list[str]) -> Any:
    """Like _json, but a run that GitHub no longer has raises GONE."""
    try:
        proc = gh_fn(args)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        if GONE_RE.search(proc.stderr or ""):
            raise ActionsError(GONE)
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def wait_run(
    cfg: Config,
    run_id: int,
    limit_s: float,
    gh: GhFn | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    gh = gh or _real_gh()
    args = ["run", "view", str(run_id), "-R", cfg.builds.repo, "--json", "status,conclusion"]
    start = clock()
    reached = False
    while True:
        data = _view(gh, args)
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
) -> tuple[str | None, bool]:
    """(status, retry). retry is True when the file could not be fetched at all."""
    path = f"repos/{cfg.builds.repo}/contents/{folder}/build-result.json"
    data = _retry_json(gh_fn, ["api", path], sleep)
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        return None, True
    try:
        result = json.loads(base64.b64decode(data["content"], validate=False).decode("utf-8"))
    except (ValueError, binascii.Error):
        return None, False
    if not isinstance(result, dict) or result.get("folder") != folder:
        return None, False
    got = result.get("run_id")
    if isinstance(got, bool) or got != run_id:
        return None, False
    status = result.get("status")
    return (status if status in RESULT_STATUSES else None), False


def _publish_failure(job: dict) -> tuple[str, str]:
    steps = {
        s.get("name"): s.get("conclusion") for s in job.get("steps") or [] if isinstance(s, dict)
    }
    if any(steps.get(name) == "failure" for name in NO_OUTPUT_STEPS):
        return "failed", "build produced no output"
    if steps.get("Scan and commit") != "failure" and steps.get(SCAN_BLOCK_STEP) == "failure":
        return "blocked", "leak scan blocked the build"
    return "failed", "publish step failed"


def outcome(
    cfg: Config,
    run_id: int,
    folder: str,
    gh: GhFn | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    """Map a finished run to (status, reason). Reasons are fixed phrases.

    ("building", UNREADABLE) means the run is done but its result cannot be read yet: keep the
    record and ask again later.
    """
    gh = gh or _real_gh()
    data = _retry_json(
        gh,
        ["run", "view", str(run_id), "-R", cfg.builds.repo, "--json", "status,conclusion,jobs"],
        sleep,
    )
    if not isinstance(data, dict):
        return "failed", UNREADABLE
    jobs = {j.get("name"): j for j in data.get("jobs") or [] if isinstance(j, dict)}
    build = (jobs.get("build") or {}).get("conclusion")
    publish_job = jobs.get("publish") or {}
    publish = publish_job.get("conclusion")
    if publish == "success":
        status, retry = _read_result(cfg, folder, run_id, gh, sleep)
        if status is None:
            if retry:
                return "building", UNREADABLE
            return "failed", "unexpected build result"
        return status, "build did not run" if status == "did_not_run" else ""
    if "cancelled" in (data.get("conclusion"), build, publish):
        return "failed", "workflow cancelled"
    if publish not in (None, "skipped"):
        return _publish_failure(publish_job)
    if build not in (None, "success"):
        return "failed", "build job failed"
    return "failed", "unexpected build result"
