"""Run one daily build: pick an item, build it in the sandbox, check it, hand it to the publisher.

Privacy: the container sees only the public item fields, the committed field profile, and the
prompt. Reasons in records are fixed phrases plus scan finding reasons, never file contents.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from radar.builds import sandbox
from radar.builds.scan import personal_markers, sanitize_png, scan_bytes, scan_tree
from radar.builds.select import History, HistoryCorrupt, folder_name, pick

VERIFY_TIMEOUT_MIN = 15
VERIFY_TIMEOUT_S = VERIFY_TIMEOUT_MIN * 60
BANNER = (
    "> **DID NOT RUN.** The demo's requirements did not install or its smoke test did not pass "
    "in a clean container. Treat everything below as unverified.\n\n"
)
DID_NOT_RUN = "requirements did not install or the demo test did not pass"
LINK_REASON = "link or special file in the build folder"
DONE = ("published", "did_not_run")
CLEAN_NAMES = (".task", "__pycache__", ".pytest_cache")
TEMPLATE_RE = re.compile(r"\{\{(DAY|FIELD_PROFILE|ITEM_JSON)\}\}")


@dataclass
class BuildRecord:
    day: str
    status: str  # published | did_not_run | blocked | failed | skipped
    package: str = ""
    folder: str = ""
    url: str = ""
    reason: str = ""
    seconds: float = 0


def render_prompt(template: str, day: str, item: dict, profile: str) -> str:
    """Fill the template in one pass, so text inside a value is never treated as a placeholder."""
    values = {
        "DAY": day,
        "FIELD_PROFILE": profile.strip(),
        "ITEM_JSON": json.dumps(item, indent=2, ensure_ascii=False),
    }
    return TEMPLATE_RE.sub(lambda m: values[m.group(1)], template)


def _write_record(record: BuildRecord, digest_folder: Path, records_dir: Path) -> None:
    data = asdict(record)
    if record.status == "published":
        data.pop("reason")
    text = json.dumps(data, indent=2)
    digest_folder.mkdir(parents=True, exist_ok=True)
    records_dir.mkdir(parents=True, exist_ok=True)
    for path in (digest_folder / "build.json", records_dir / f"{record.day}.json"):
        if record.status not in DONE and _has_done_record(path):
            continue  # a later skip or failure must not hide the day's published build
        path.write_text(text, encoding="utf-8")


def _has_done_record(path: Path) -> bool:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("status") in DONE
    except (OSError, ValueError, AttributeError):
        return False


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _has_special_entry(root: Path) -> bool:
    """True if the folder holds a link, junction, hard link, or non-regular entry (lstat only)."""
    junction = getattr(os.path, "isjunction", None)
    stack = [str(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                entries = list(it)
        except OSError:
            return True
        for e in entries:
            try:
                st = os.lstat(e.path)
            except OSError:
                return True
            if stat.S_ISLNK(st.st_mode) or (junction and junction(e.path)):
                return True
            if stat.S_ISDIR(st.st_mode):
                stack.append(e.path)
            elif not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                return True
    return False


def _redact_logs(logs: Path, key: str) -> None:
    """Remove the API key from every kept log; a log that still matches the scan is blanked."""
    windows = {key[i : i + 16] for i in range(max(len(key) - 15, 1))} if len(key) >= 16 else set()
    for p in logs.rglob("*"):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            for needle in sorted({key, *windows}, key=len, reverse=True):
                text = text.replace(needle, "[redacted]")
            if scan_bytes(p.name, text.encode("utf-8"), [], [key]):
                text = "[log removed: it matched the secret scan]"
            p.write_text(text, encoding="utf-8")
        except OSError:
            p.unlink(missing_ok=True)


def _container_gone(docker, name: str) -> bool:
    """Force-remove the container and confirm it is not running."""
    try:
        docker.remove(name)
        return not docker.is_running(name)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False


def _clean_work(work: Path) -> None:
    for name in CLEAN_NAMES:
        _remove(work / name)
    for p in [*work.rglob("__pycache__"), *work.rglob(".pytest_cache")]:
        _remove(p)
    _remove(work / ".verify.json")


def _move_logs(work: Path, dest: Path) -> None:
    src = work / ".logs"
    if src.is_symlink() or not src.is_dir():
        _remove(src)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    _remove(dest)
    shutil.move(str(src), str(dest))


def _has_deliverables(work: Path) -> bool:
    readme = work / "README.md"
    has_py = any(p.is_file() and not p.is_symlink() for p in work.rglob("*.py"))
    return readme.is_file() and not readme.is_symlink() and has_py


def _read_verify(work: Path) -> bool:
    path = work / ".verify.json"
    try:
        if path.is_symlink() or not path.is_file():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(data, dict)
        and data.get("installed") is True
        and data.get("tests_passed") is True
    )


def _sanitize_images(work: Path) -> None:
    notes = []
    for png in sorted(work.rglob("*.png")):
        if png.is_symlink() or not png.is_file():
            continue  # the scan reports links
        try:
            png.write_bytes(sanitize_png(png.read_bytes()))
        except (ValueError, OSError):
            png.unlink(missing_ok=True)
            notes.append(f"- removed a malformed image: {png.relative_to(work).as_posix()}\n")
    if notes:
        log = work / "BUILD_LOG.md"
        old = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        log.write_text(old + "\n## Removed files\n" + "".join(notes), encoding="utf-8")


def run_build(
    cfg,
    digest_folder: Path,
    result: dict,
    *,
    api_key: str | None,
    docker=sandbox,
    publisher: Callable | None = None,
    today: dt.date | None = None,
    log: Callable = print,
    containers: list[str] | None = None,
) -> BuildRecord:
    """`containers`, when given, receives the container names as they are chosen, so a caller
    that gives up waiting can force-remove them."""
    start = time.monotonic()
    day = (today or dt.date.today()).isoformat()
    cand = None
    folder = ""

    def finish(status: str, reason: str = "", url: str = "") -> BuildRecord:
        record = BuildRecord(
            day=day,
            status=status,
            package=cand.package if cand else "",
            folder=folder,
            url=url,
            reason=reason,
            seconds=round(time.monotonic() - start, 1),
        )
        try:
            _write_record(record, Path(digest_folder), cfg.builds_dir / "records")
        except OSError:
            log("builds: could not write the build record")
        if status in ("blocked", "failed") and folder:
            _remove(cfg.builds_dir / "work" / folder)  # never leave a rejected build on disk
        if cand:
            try:
                history = History(cfg.builds_dir / "history.json")
                if url:
                    history.add_published(cand.package, folder, url)
                else:
                    history.add_attempt(cand.package, status, reason)
            except (HistoryCorrupt, OSError):
                log("builds: could not update the build history")
        log(f"builds: {status}" + (f" ({reason})" if reason else ""))
        return record

    b = cfg.builds
    if not b.enabled:
        return finish("skipped", "builds disabled")
    if not api_key:
        return finish("skipped", "no API key")
    if not b.repo:
        return finish("skipped", "no target repo")
    ready, why = docker.docker_ready()
    if not ready:
        return finish("skipped", why)
    history = History(cfg.builds_dir / "history.json")
    try:
        if history.published_on(day):
            return finish("skipped", "already published today")
        cand, reason = pick(result, history.built())
    except HistoryCorrupt:
        return finish("skipped", "build history file is corrupt")
    if cand is None:
        return finish("skipped", reason)

    folder = folder_name(day, cand.package, cand.title)
    work = cfg.builds_dir / "work" / folder
    try:
        profile = (cfg.root / "config" / "field_profile.md").read_text(encoding="utf-8")
        template = (cfg.root / "prompts" / "build_task.md").read_text(encoding="utf-8")
        prompt = render_prompt(template, day, cand.item, profile)
        _remove(work)
        if work.exists():
            raise OSError("stale build folder could not be removed")
        (work / ".task").mkdir(parents=True)
        (work / ".task" / "prompt.md").write_text(prompt, encoding="utf-8")
        (work / ".task" / "item.json").write_text(
            json.dumps(cand.item, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        return finish("failed", "could not prepare the build folder")

    name = f"radar-build-{folder}"
    if containers is not None:
        containers.extend([name, name + "-verify"])
    try:
        docker.ensure_network_and_proxy(cfg)
        docker.ensure_image(cfg)
        with docker.api_key_env_file(api_key) as env_file:
            argv = docker.build_argv(cfg, work, name, env_file=env_file)
            log(f"builds: building {cand.package}")
            code, _output = docker.run_container(argv, b.timeout_min * 60)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
        return finish("failed", "docker setup failed")

    # The container has exited (or must be proven gone); from here the host owns the folder.
    if not _container_gone(docker, name):
        return finish("failed", "build container did not stop")
    if _has_special_entry(work):
        return finish("blocked", LINK_REASON)
    _move_logs(work, cfg.builds_dir / "logs" / folder)
    _redact_logs(cfg.builds_dir / "logs" / folder, api_key)
    if code == 124:
        return finish("failed", "build timed out")
    _clean_work(work)
    if not _has_deliverables(work):
        return finish("failed", "no README.md or no Python file")

    verified = False
    try:
        vcode, _out = docker.run_container(
            docker.verify_argv(cfg, work, name + "-verify"), VERIFY_TIMEOUT_S
        )
        if not _container_gone(docker, name + "-verify"):
            return finish("failed", "build container did not stop")
        if _has_special_entry(work):
            return finish("blocked", LINK_REASON)
        verified = vcode != 124 and _read_verify(work)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
        verified = False
    _clean_work(work)
    if not _has_deliverables(work):
        return finish("failed", "no README.md or no Python file")
    if not verified:
        readme = work / "README.md"
        text = readme.read_text(encoding="utf-8", errors="replace")
        readme.write_text(BANNER + text, encoding="utf-8")

    _sanitize_images(work)

    markers = personal_markers(cfg)
    findings = scan_tree(work, markers, b.max_file_mb, b.max_folder_mb, secrets=[api_key])
    if findings:
        return finish("blocked", "leak scan: " + "; ".join(sorted({f.reason for f in findings})))

    status = "published" if verified else "did_not_run"
    record = BuildRecord(day=day, status=status, package=cand.package, folder=folder)
    try:
        if publisher is None:
            from .publish import publish as publisher
        url = publisher(cfg, work, record, markers, [api_key])
    except Exception as exc:  # noqa: BLE001 - any publisher failure must be recorded, not raised
        if type(exc).__name__ == "PublishBlocked":
            return finish("blocked", f"publish: {exc}")
        return finish("failed", "publish failed")
    return finish(status, "" if verified else DID_NOT_RUN, url)
