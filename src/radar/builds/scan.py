"""Leak scan: last gate before a build folder is published. Errs toward blocking.

Findings carry a rule name and a path, never the matched text.
"""

from __future__ import annotations

import contextlib
import getpass
import math
import os
import platform
import re
import stat
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from radar.config import Config

ARCHIVE_EXT = frozenset(
    {".zip", ".tar", ".gz", ".7z", ".whl", ".egg", ".pkl", ".pt", ".npz", ".h5"}
)
ALLOWED_BINARY_EXT = frozenset({".png", ".svg"})
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MIN_MARKER_LEN = 3
MB = 1024 * 1024

SECRET_RE = re.compile(
    r"sk-ant-"
    r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}"
    r"|ghp_[A-Za-z0-9]{8,}"
    r"|github_pat_[A-Za-z0-9_]{8,}"
    r"|gho_[A-Za-z0-9]{8,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|xox[baprs]-[A-Za-z0-9-]{6,}"
    r"|AIza[0-9A-Za-z_-]{35}"
)
TOKEN_RE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
HEX_RE = re.compile(r"[0-9a-fA-F]+")
WIN_PATH_RE = re.compile(r"[A-Za-z]:\\{1,2}Users\\", re.IGNORECASE)
WIN_PATH_FWD_RE = re.compile(r"[A-Za-z]:/Users/", re.IGNORECASE)
UNIX_PATH_RE = re.compile(r"/Users/|/home/")


@dataclass
class Finding:
    path: str
    reason: str


def personal_markers(cfg: Config) -> list[str]:
    """Strings that must never appear in published files."""
    home = str(Path.home())
    raw = [
        cfg.email.to,
        cfg.email.sender,
        home,
        home.replace("\\", "/"),
        home.replace("\\", "\\\\"),
    ]
    with contextlib.suppress(OSError, KeyError, ImportError):  # host without a user name
        raw.append(getpass.getuser())
    raw.append(platform.node())
    raw.extend(os.environ.get("RADAR_PRIVATE_MARKERS", "").split(","))
    out: list[str] = []
    for m in raw:
        m = (m or "").strip()
        if len(m) >= MIN_MARKER_LEN and m not in out:
            out.append(m)
    return out


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _text_findings(
    name: str, text: str, markers: list[str], *, entropy: bool = True
) -> list[Finding]:
    found: list[Finding] = []
    if SECRET_RE.search(text):
        found.append(Finding(name, "secret pattern"))
    if entropy:
        is_req = PurePosixPath(name.replace("\\", "/")).name.lower().startswith("requirements")
        for tok in TOKEN_RE.findall(text):
            if is_req and HEX_RE.fullmatch(tok):
                continue  # pip --hash digest
            if _entropy(tok) >= 4.0:
                found.append(Finding(name, "secret pattern (high-entropy token)"))
                break
    low = text.lower()
    if any(m.lower() in low for m in markers if len(m) >= MIN_MARKER_LEN):
        found.append(Finding(name, "personal information"))
    if WIN_PATH_RE.search(text) or WIN_PATH_FWD_RE.search(text) or UNIX_PATH_RE.search(text):
        found.append(Finding(name, "local path"))
    return found


def scan_bytes(name: str, data: bytes, markers: list[str]) -> list[Finding]:
    """Apply the file rules (type, binary, text) to one in-memory file."""
    ext = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    if ext in ARCHIVE_EXT:
        return [Finding(name, "archive")]
    if ext == ".png":
        if not data.startswith(PNG_MAGIC):
            return [Finding(name, "binary")]
        # compressed pixels hide nothing greppable; metadata chunks can
        return _text_findings(name, data.decode("latin-1"), markers, entropy=False)
    if b"\x00" in data[:8192]:
        return [Finding(name, "binary")]
    return _text_findings(name, data.decode("utf-8", errors="replace"), markers)


def _is_junction(path: str) -> bool:
    fn = getattr(os.path, "isjunction", None)
    return bool(fn and fn(path))


def scan_tree(
    root: Path, markers: list[str], max_file_mb: float, max_folder_mb: float
) -> list[Finding]:
    root = Path(root)
    findings: list[Finding] = []
    total = 0
    marks = [m for m in markers if len(m) >= MIN_MARKER_LEN]
    try:
        rst = os.lstat(root)
    except OSError:
        return [Finding(".", "unreadable")]
    if stat.S_ISLNK(rst.st_mode) or _is_junction(str(root)):
        return [Finding(".", "link")]

    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            findings.append(Finding(_rel(root, cur), "unreadable"))
            continue
        for e in entries:
            rel = _rel(root, Path(e.path))
            if any(m.lower() in rel.lower() for m in marks):
                findings.append(Finding(rel, "personal information (file name)"))
            try:
                st = os.lstat(e.path)  # DirEntry.stat() lacks st_nlink on Windows
            except OSError:
                findings.append(Finding(rel, "unreadable"))
                continue
            if stat.S_ISLNK(st.st_mode) or _is_junction(e.path):
                findings.append(Finding(rel, "link"))
            elif stat.S_ISDIR(st.st_mode):
                stack.append(Path(e.path))
            elif not stat.S_ISREG(st.st_mode):
                findings.append(Finding(rel, "not a regular file"))
            elif st.st_nlink > 1:
                findings.append(Finding(rel, "hard link"))
            else:
                total += st.st_size
                if st.st_size > max_file_mb * MB:
                    findings.append(Finding(rel, "file too large"))
                    continue
                try:
                    data = Path(e.path).read_bytes()
                except OSError:
                    findings.append(Finding(rel, "unreadable"))
                    continue
                findings.extend(scan_bytes(rel, data, marks))
    if total > max_folder_mb * MB:
        findings.append(Finding(".", "folder too large"))
    return findings


def _rel(root: Path, p: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()
