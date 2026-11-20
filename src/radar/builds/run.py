"""Run one daily build: pick an item, start it on GitHub Actions, wait for it, record the result.

Privacy: only the public item fields, the committed field profile and the folder name are sent.
Reasons in records are fixed phrases, never workflow output or file contents.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from radar.builds import actions as actions_module
from radar.builds import scan
from radar.builds.actions import ActionsError
from radar.builds.select import History, HistoryCorrupt, folder_name, pick

DONE = ("published", "did_not_run")
SYNC_LIMIT_S = 0  # sync polls each building record once
PROFILE_REL = "config/field_profile.md"
GIT_ENV_ALLOW = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
GIT_TIMEOUT_S = 30
NOT_COMMITTED = "field profile not committed"
UNCOMMITTED = "field profile has uncommitted changes"
GONE = actions_module.GONE
STILL_RUNNING = "a build is still running"
NOT_FOUND = "workflow started but run not found"
UNREADABLE = actions_module.UNREADABLE
PROMPT_MAX_CHARS = 60_000
PROMPT_BLOCKING = ("personal information", "local path", "secret pattern")
TEMPLATE_RE = re.compile(r"\{\{(DAY|FIELD_PROFILE|ITEM_JSON)\}\}")


@dataclass
class BuildRecord:
    day: str
    status: str  # published | did_not_run | blocked | failed | skipped | building
    package: str = ""
    folder: str = ""
    url: str = ""
    reason: str = ""
    seconds: float = 0
    run_id: int | None = None


def render_prompt(template: str, day: str, item: dict, profile: str) -> str:
    """Fill the template in one pass, so text inside a value is never treated as a placeholder."""
    values = {
        "DAY": day,
        "FIELD_PROFILE": profile.strip(),
        "ITEM_JSON": json.dumps(item, indent=2, ensure_ascii=False),
    }
    return TEMPLATE_RE.sub(lambda m: values[m.group(1)], template)


def _committed_profile(root: Path) -> tuple[str | None, str]:
    """The field profile as committed at HEAD, or (None, skip reason).

    The setup wizard and the Field profile tab rewrite the working copy, possibly with
    CV-derived text, so only the committed version may reach the build.
    """
    env = {k: v for k, v in os.environ.items() if k.upper() in GIT_ENV_ALLOW}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    argv = ["git", "-c", f"safe.directory={Path(root).as_posix()}", "show", f"HEAD:{PROFILE_REL}"]
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            argv, cwd=root, env=env, capture_output=True, timeout=GIT_TIMEOUT_S, check=False
        )
        if proc.returncode != 0:
            return None, NOT_COMMITTED
        committed = proc.stdout.decode("utf-8").replace("\r\n", "\n")
        working = (Path(root) / PROFILE_REL).read_bytes().decode("utf-8").replace("\r\n", "\n")
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None, NOT_COMMITTED
    if committed != working:
        return None, UNCOMMITTED
    return committed, ""


def _prompt_leaks(cfg, prompt: str) -> bool:
    """True when the outgoing prompt carries personal markers, local paths or key patterns.

    High-entropy findings are ignored: public item text legitimately holds hashes and URLs.
    """
    findings = scan.scan_bytes("prompt", prompt.encode("utf-8"), scan.personal_markers(cfg))
    return any(
        f.reason.startswith(PROMPT_BLOCKING) and "(high-entropy" not in f.reason for f in findings
    )


def _status_of(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("status") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_record(record: BuildRecord, digest_folder: Path | None, records_dir: Path) -> None:
    data = asdict(record)
    if record.status == "published":
        data.pop("reason")
    text = json.dumps(data, indent=2)
    records_dir.mkdir(parents=True, exist_ok=True)
    paths = [records_dir / f"{record.day}.json"]
    if digest_folder is not None:
        digest_folder.mkdir(parents=True, exist_ok=True)
        paths.insert(0, digest_folder / "build.json")
    for path in paths:
        current = _status_of(path)
        if record.status not in DONE and current in DONE:
            continue  # a later skip or failure must not hide the day's published build
        if record.status == "skipped" and current == "building":
            continue  # a skip must not hide a build that is still running
        path.write_text(text, encoding="utf-8")


def record_skip(
    cfg, day: str, reason: str, digest_folder: Path | None = None, log: Callable = print
) -> BuildRecord:
    """Write a skipped record for a build that never started (no digest, lock)."""
    record = BuildRecord(day=day, status="skipped", reason=reason)
    try:
        _write_record(record, digest_folder, cfg.builds_dir / "records")
    except OSError:
        log("builds: could not write the build record")
    return record


def read_record(cfg, day: str) -> BuildRecord | None:
    """The day's record, or None."""
    try:
        data = json.loads((cfg.builds_dir / "records" / f"{day}.json").read_text("utf-8"))
        fields = BuildRecord.__dataclass_fields__
        return BuildRecord(**{k: v for k, v in data.items() if k in fields})
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _save(
    cfg, record: BuildRecord, digest_folder: Path | None, log: Callable, track: bool = True
) -> None:
    """Write the record; add finished builds to the history (not `building`)."""
    try:
        _write_record(record, digest_folder, cfg.builds_dir / "records")
    except OSError:
        log("builds: could not write the build record")
    if track and record.package and record.status not in ("building", "skipped"):
        try:
            history = History(cfg.builds_dir / "history.json")
            if record.url:
                history.add_published(record.package, record.folder, record.url)
            else:
                history.add_attempt(record.package, record.status, record.reason)
        except (HistoryCorrupt, OSError):
            log("builds: could not update the build history")
    log(f"builds: {record.status}" + (f" ({record.reason})" if record.reason else ""))


def run_build(
    cfg,
    digest_folder: Path,
    result: dict,
    *,
    actions=actions_module,
    today: dt.date | None = None,
    log: Callable = print,
    retry: bool = False,
) -> BuildRecord:
    start = time.monotonic()
    day = (today or dt.date.today()).isoformat()
    cand = None
    folder = ""
    run_id: int | None = None

    def finish(status: str, reason: str = "", url: str = "") -> BuildRecord:
        record = BuildRecord(
            day=day,
            status=status,
            package=cand.package if cand else "",
            folder=folder,
            url=url,
            reason=reason,
            seconds=round(time.monotonic() - start, 1),
            run_id=run_id,
        )
        _save(cfg, record, Path(digest_folder), log)
        return record

    b = cfg.builds
    if not b.enabled:
        return finish("skipped", "builds disabled")
    if not b.repo:
        return finish("skipped", "no target repo")
    profile, why = _committed_profile(cfg.root)
    if profile is None:
        return finish("skipped", why)
    history = History(cfg.builds_dir / "history.json")
    try:
        if history.published_on(day):
            return finish("skipped", "already published today")
        if not retry and _status_of(cfg.builds_dir / "records" / f"{day}.json") == "building":
            return finish("skipped", STILL_RUNNING)
        cand, reason = pick(result, history.built())
    except HistoryCorrupt:
        return finish("skipped", "build history file is corrupt")
    if cand is None:
        return finish("skipped", reason)

    folder = folder_name(day, cand.package, cand.title)
    try:
        template = (cfg.root / "prompts" / "build_task.md").read_text(encoding="utf-8")
        prompt = render_prompt(template, day, cand.item, profile)
    except OSError:
        return finish("failed", "could not prepare the build prompt")
    if len(prompt) > PROMPT_MAX_CHARS:
        return finish("skipped", "build prompt too long")
    if _prompt_leaks(cfg, prompt):
        return finish("skipped", "build prompt contains personal information")
    names = actions.secret_names(b.repo)
    if names is None:
        return finish("skipped", "could not check the repo secrets")
    if actions_module.SECRET_NAME not in names:
        return finish("skipped", "subscription token secret not set in the repo")

    since = dt.datetime.now(dt.UTC)
    try:
        log(f"builds: starting {cand.package}")
        actions.dispatch(cfg, folder, prompt)
    except ActionsError as exc:
        return finish("failed", str(exc))
    try:
        run_id = actions.find_run(cfg, folder, since)
    except ActionsError:
        # The workflow may still publish, so this is not a history attempt.
        record = BuildRecord(
            day,
            "failed",
            cand.package,
            folder,
            reason=NOT_FOUND,
            seconds=round(time.monotonic() - start, 1),
        )
        _save(cfg, record, Path(digest_folder), log, track=False)
        return record
    # Written now, so a crash or a restart while waiting never starts a second build.
    _save(
        cfg,
        BuildRecord(day, "building", cand.package, folder, run_id=run_id),
        Path(digest_folder),
        log,
    )

    try:
        state = actions.wait_run(cfg, run_id, b.poll_min * 60)
    except ActionsError as exc:
        if str(exc) == actions_module.GONE:
            return finish("failed", GONE)
        return finish("building", str(exc))
    if state == "building":
        return finish("building")
    status, reason = actions.outcome(cfg, run_id, folder)
    return finish(status, reason, actions.url(cfg, folder) if status in DONE else "")


def _digest_folders(cfg, record: BuildRecord) -> list[Path]:
    """Digest folders of the record's day whose build.json is the same still-running build."""
    out = []
    for path in sorted(cfg.digests_dir.glob(f"{record.day}*")):
        try:
            marker = json.loads((path / "build.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(marker, dict)
            and marker.get("status") == "building"
            and marker.get("run_id") == record.run_id
        ):
            out.append(path)
    return out


def sync_builds(cfg, actions=actions_module, log: Callable = print) -> list[BuildRecord]:
    """Finish every `building` record whose workflow run has ended. Returns the finished ones."""
    done: list[BuildRecord] = []
    for path in sorted((cfg.builds_dir / "records").glob("*.json")):
        rec = read_record(cfg, path.stem)
        if rec is None or rec.status != "building" or not isinstance(rec.run_id, int):
            continue
        try:
            state = actions.wait_run(cfg, rec.run_id, SYNC_LIMIT_S)
            if state == "building":
                continue
            status, reason = actions.outcome(cfg, rec.run_id, rec.folder)
            if reason == UNREADABLE:
                continue  # keep it building; the next sync tries again
        except ActionsError as exc:
            if str(exc) != GONE:
                log(f"builds: could not check the run for {rec.day}")
                continue
            status, reason = "failed", GONE
        final = BuildRecord(
            day=rec.day,
            status=status,
            package=rec.package,
            folder=rec.folder,
            url=actions.url(cfg, rec.folder) if status in DONE else "",
            reason=reason,
            seconds=rec.seconds,
            run_id=rec.run_id,
        )
        folders = _digest_folders(cfg, rec)
        _save(cfg, final, folders[0] if folders else None, log)
        for extra in folders[1:]:
            try:
                _write_record(final, extra, cfg.builds_dir / "records")
            except OSError:
                log("builds: could not write the build record")
        done.append(final)
    return done
