"""Run one daily build: pick an item, build it in the sandbox, check it, hand it to the publisher.

Privacy: the container sees only the public item fields, the committed field profile, and the
prompt. Reasons in records are fixed phrases plus scan finding reasons, never file contents.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from radar.builds import sandbox
from radar.builds.scan import personal_markers, sanitize_png, scan_tree
from radar.builds.select import History, HistoryCorrupt, folder_name, pick

VERIFY_TIMEOUT_S = 15 * 60
BANNER = (
    "> **DID NOT RUN.** The demo's requirements did not install or its smoke test did not pass "
    "in a clean container. Treat everything below as unverified.\n\n"
)
DID_NOT_RUN = "requirements did not install or the demo test did not pass"
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
    (digest_folder / "build.json").write_text(text, encoding="utf-8")
    records_dir.mkdir(parents=True, exist_ok=True)
    (records_dir / f"{record.day}.json").write_text(text, encoding="utf-8")


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


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
) -> BuildRecord:
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
        template = (cfg.root / "prompts" / "build_task.md").read_text(encoding="utf-8")
        prompt = render_prompt(template, day, cand.item, cfg.field_profile_text())
        _remove(work)
        (work / ".task").mkdir(parents=True)
        (work / ".task" / "prompt.md").write_text(prompt, encoding="utf-8")
        (work / ".task" / "item.json").write_text(
            json.dumps(cand.item, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        return finish("failed", "could not prepare the build folder")

    name = f"radar-build-{folder}"
    try:
        docker.ensure_network_and_proxy(cfg)
        docker.ensure_image(cfg)
        with docker.api_key_env_file(api_key) as env_file:
            argv = docker.build_argv(cfg, work, name, env_file=env_file)
            log(f"builds: building {cand.package}")
            code, _output = docker.run_container(argv, b.timeout_min * 60)
    except (RuntimeError, OSError, ValueError):
        return finish("failed", "docker setup failed")

    # The container has exited; from here the host owns the folder.
    _move_logs(work, cfg.builds_dir / "logs" / folder)
    _clean_work(work)
    readme = work / "README.md"
    has_py = any(p.is_file() and not p.is_symlink() for p in work.rglob("*.py"))
    if not readme.is_file() or readme.is_symlink() or not has_py:
        return finish(
            "failed", "build timed out" if code == 124 else "no README.md or no Python file"
        )

    try:
        docker.run_container(docker.verify_argv(cfg, work, name + "-verify"), VERIFY_TIMEOUT_S)
        verified = _read_verify(work)
    except (RuntimeError, OSError, ValueError):
        verified = False
    _clean_work(work)
    if not verified:
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
