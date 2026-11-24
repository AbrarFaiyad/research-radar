"""Publish one build artifact into this repo after a leak scan.

Runs in the trusted publish job. Everything in the artifact is hostile: it is
walked with lstat, size-capped, parsed as data only (never imported or run),
sanitized, scanned with the vendored tools/scan.py, then copied and committed.

The artifact root holds status.json and work/. Exit codes: 0 published, 2 blocked by the scan, a hostile
artifact or a token hit, 1 error (including output that could not be checked).
Output carries fixed reasons and relative paths only, never matched text.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import html
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan  # noqa: E402  (vendored copy of radar/builds/scan.py)

FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_[a-z0-9-]+_[a-z0-9-]+$")
MAX_FILE_MB = 5
MAX_FOLDER_MB = 20
MAX_FILES = 2000
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
BANNER = "This notebook did not finish running on the build machine; outputs may be partial."
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._/ -]+$")
KEEP_MIMES = frozenset(
    {"text/plain", "text/markdown", "text/latex", "application/json", "image/png", "image/svg+xml"}
)
UNSAFE_SVG_RE = re.compile(
    r"<\s*script|<\s*foreignObject|\bon[a-z]+\s*=|javascript:|@import"
    r"|href\s*=\s*[\"']?\s*(?:https?:)?//|url\(\s*[\"']?\s*(?:https?:)?//",
    re.IGNORECASE,
)
WHY = ("key", "unscannable")
INDEX_HEADER = "| Date | Package | What it shows | Ran |\n| --- | --- | --- | --- |\n"


class Unchecked(Exception):
    """The build machine could not check its own output; nothing is published."""


class Blocked(Exception):
    """Artifact refused; reason is a fixed phrase, path is relative."""

    def __init__(self, reason: str, path: str = ".") -> None:
        super().__init__(reason)
        self.reason = reason
        self.path = path


def _shown(path: str) -> str:
    return path if SAFE_NAME_RE.fullmatch(path) else "<unprintable-name>"


def _check_name(name: str) -> None:
    if name in ("", ".", "..") or any(c in name for c in "/\\:\x00"):
        raise Blocked("bad file name", _shown(name))


def _is_link(path: str, st: os.stat_result) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return stat.S_ISLNK(st.st_mode) or bool(is_junction and is_junction(path))


def walk_artifact(src: Path) -> list[tuple[str, Path]]:
    """Return (relative posix path, absolute path) for every visible regular file."""
    try:
        rst = os.lstat(src)
    except OSError as exc:
        raise Blocked("artifact unreadable") from exc
    if _is_link(str(src), rst) or not stat.S_ISDIR(rst.st_mode):
        raise Blocked("artifact is not a plain folder")
    out: list[tuple[str, Path]] = []
    total = 0
    stack: list[tuple[Path, str]] = [(src, "")]
    while stack:
        cur, disp = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise Blocked("unreadable", disp or ".") from exc
        for e in entries:
            if e.name.startswith("."):
                continue  # hidden paths are dropped, not read
            _check_name(e.name)
            rel = f"{disp}/{e.name}" if disp else e.name
            try:
                st = os.lstat(e.path)
            except OSError as exc:
                raise Blocked("unreadable", _shown(rel)) from exc
            if _is_link(e.path, st):
                raise Blocked("link", _shown(rel))
            if stat.S_ISDIR(st.st_mode):
                stack.append((Path(e.path), rel))
            elif not stat.S_ISREG(st.st_mode):
                raise Blocked("not a regular file", _shown(rel))
            elif st.st_nlink > 1:
                raise Blocked("hard link", _shown(rel))
            else:
                if len(out) >= MAX_FILES:
                    raise Blocked("too many files")
                if st.st_size > MAX_FILE_MB * scan.MB:
                    raise Blocked("file too large", _shown(rel))
                total += st.st_size
                if total > MAX_FOLDER_MB * scan.MB:
                    raise Blocked("folder too large")
                out.append((rel, Path(e.path)))
    return out


def read_status(src: Path) -> tuple[bool, bool, str]:
    """Return (ran, blocked, why) from status.json; missing or odd means did not run.

    why is "key" or "unscannable" when the tripwire blocked the output, else "".
    """
    p = src / "status.json"
    try:
        st = os.lstat(p)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 4096:
            return False, False, ""
        data = json.loads(p.read_text(encoding="utf-8"))
        why = data.get("why")
        return data.get("ran") is True, data.get("blocked") is True, why if why in WHY else ""
    except (OSError, ValueError, AttributeError):
        return False, False, ""


def _png_b64(value: object, rel: str) -> str:
    text = "".join(value) if isinstance(value, list) else value
    if not isinstance(text, str):
        raise Blocked("invalid image", rel)
    try:
        raw = base64.b64decode("".join(text.split()), validate=True)
        return base64.b64encode(scan.sanitize_png(raw)).decode("ascii")
    except (binascii.Error, ValueError, zlib.error) as exc:
        raise Blocked("invalid image", rel) from exc


def _unsafe_svg(text: str) -> bool:
    """Active content, also when it is hidden behind character references."""
    return bool(UNSAFE_SVG_RE.search(text) or UNSAFE_SVG_RE.search(html.unescape(text)))


def _svg_text(value: object) -> str | None:
    text = "".join(value) if isinstance(value, list) else value
    return text if isinstance(text, str) else None


def _clean_mime_bundle(data: dict, rel: str, pngs: list[tuple[dict, str]]) -> None:
    for mime in list(data):
        if mime not in KEEP_MIMES:
            del data[mime]
        elif mime == "image/png":
            data[mime] = _png_b64(data[mime], rel)
            pngs.append((data, mime))
        elif mime == "image/svg+xml":
            svg = _svg_text(data[mime])
            if svg is None or _unsafe_svg(svg):
                del data[mime]


def process_notebook(raw: bytes, rel: str, ran: bool) -> tuple[bytes, bytes]:
    """Return (notebook to publish, notebook text to scan).

    The scan copy writes each image as a data URI so the scanner checks it as a
    strict PNG instead of flagging its base64 as a high-entropy token.
    """
    try:
        nb = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise Blocked("invalid notebook", rel) from exc
    cells = nb.get("cells") if isinstance(nb, dict) else None
    if not isinstance(cells, list):
        raise Blocked("invalid notebook", rel)
    pngs: list[tuple[dict, str]] = []
    for cell in cells:
        if not isinstance(cell, dict):
            raise Blocked("invalid notebook", rel)
        bundles = [o.get("data") for o in cell.get("outputs", []) if isinstance(o, dict)]
        attach = cell.get("attachments")
        if isinstance(attach, dict):
            bundles += list(attach.values())
        for b in bundles:
            if isinstance(b, dict):
                _clean_mime_bundle(b, rel, pngs)
    if not ran:
        banner = {"cell_type": "markdown", "metadata": {}, "source": [f"**{BANNER}**"]}
        if isinstance(nb.get("nbformat_minor"), int) and nb["nbformat_minor"] >= 5:
            banner["id"] = "build-banner"
        cells.insert(0, banner)
    dump = {"indent": 1, "ensure_ascii": False}
    try:
        real = (json.dumps(nb, **dump) + "\n").encode("utf-8")
        for data, mime in pngs:
            data[mime] = "data:image/png;base64," + data[mime]
        return real, (json.dumps(nb, **dump) + "\n").encode("utf-8")
    except UnicodeEncodeError as exc:  # a lone surrogate cannot be written as UTF-8
        raise Blocked("invalid notebook", rel) from exc


def stage(files: list[tuple[str, Path]], ran: bool, stage_dir: Path) -> dict[str, bytes]:
    """Write scan copies into stage_dir; return publish bytes for files that differ."""
    real: dict[str, bytes] = {}
    for rel, path in files:
        raw = path.read_bytes()
        ext = Path(rel).suffix.lower()
        scan_raw = raw
        if ext == ".ipynb":
            real[rel], scan_raw = process_notebook(raw, _shown(rel), ran)
        elif ext == ".png":
            try:
                raw = scan.sanitize_png(raw)
            except (ValueError, zlib.error) as exc:
                raise Blocked("invalid image", _shown(rel)) from exc
            real[rel] = scan_raw = raw
        elif ext == ".svg":
            if _unsafe_svg(raw.decode("utf-8", errors="replace")):
                raise Blocked("active svg", _shown(rel))
        dest = stage_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(scan_raw)
    return real


def markers_from_env() -> list[str]:
    raw = os.environ.get("RADAR_PRIVATE_MARKERS", "").split(",")
    return [m.strip() for m in raw if len(m.strip()) >= scan.MIN_MARKER_LEN]


def update_index(repo: Path, folder: str, ran: bool) -> None:
    date, package, task = folder.split("_", 2)
    row = f"| {date} | {package} | {task} | {'yes' if ran else 'no'} |\n"
    path = repo / "README.md"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("| --- |"):
            lines.insert(i + 1, row)
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["\n", INDEX_HEADER, row]
    path.write_text("".join(lines), encoding="utf-8", newline="\n")


GIT_ENV_KEEP = ("PATH", "HOME", "LANG", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP")


def _git_env() -> dict[str, str]:
    stamp = f"@{int(time.time())} +0000"
    env = {k: os.environ[k] for k in GIT_ENV_KEEP if k in os.environ}
    env.update(
        TZ="UTC",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME=BOT_NAME,
        GIT_AUTHOR_EMAIL=BOT_EMAIL,
        GIT_COMMITTER_NAME=BOT_NAME,
        GIT_COMMITTER_EMAIL=BOT_EMAIL,
        GIT_AUTHOR_DATE=stamp,
        GIT_COMMITTER_DATE=stamp,
    )
    return env


def commit(repo: Path, folder: str) -> None:
    cfg = [
        "-c",
        f"user.name={BOT_NAME}",
        "-c",
        f"user.email={BOT_EMAIL}",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
    ]
    env = _git_env()

    def git(*args: str) -> None:
        subprocess.run(["git", *cfg, *args], cwd=repo, env=env, check=True, capture_output=True)

    git("add", "--", folder, "README.md")
    git("commit", "--no-verify", "-m", f"Build {folder}")


def publish(folder: str, src: Path, repo: Path) -> None:
    if not FOLDER_RE.fullmatch(folder):
        raise Blocked("bad folder name")
    ran, blocked, why = read_status(src)
    if blocked:
        if why == "unscannable":
            raise Unchecked
        raise Blocked("build output contained the subscription token")
    files = walk_artifact(src / "work")
    if not files:
        raise Blocked("empty artifact")
    dest = repo / folder
    if os.path.lexists(dest):
        raise FileExistsError
    with tempfile.TemporaryDirectory() as tmp:
        stage_dir = Path(tmp) / "stage"
        stage_dir.mkdir()
        real = stage(files, ran, stage_dir)
        for rel, data in real.items():
            if len(data) > MAX_FILE_MB * scan.MB:
                raise Blocked("file too large", _shown(rel))
        findings = scan.scan_tree(stage_dir, markers_from_env(), MAX_FILE_MB, MAX_FOLDER_MB)
        if findings:
            raise Blocked("scan: " + "; ".join(f"{f.reason}: {f.path}" for f in findings))
        shutil.copytree(stage_dir, dest)
        for rel, data in real.items():
            (dest / rel).write_bytes(data)
    status = "published" if ran else "did_not_run"
    result: dict[str, object] = {"status": status, "folder": folder}
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if run_id.isdigit():
        result["run_id"] = int(run_id)
    (dest / "build-result.json").write_text(
        json.dumps(result) + "\n", encoding="utf-8", newline="\n"
    )
    update_index(repo, folder, ran)
    commit(repo, folder)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--folder", required=True)
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--repo", type=Path, default=Path("."))
    args = ap.parse_args(argv)
    try:
        publish(args.folder, args.src, args.repo.resolve())
    except Blocked as b:
        print(f"blocked: {b.reason}" + (f" ({b.path})" if b.path != "." else ""))
        return 2
    except Unchecked:
        print("error: build output could not be checked")
        return 1
    except FileExistsError:
        print("error: build folder already exists")
        return 1
    except Exception as exc:  # noqa: BLE001  fixed text only: a message could carry file content
        print(f"error: {type(exc).__name__}")
        return 1
    print(f"published {args.folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
