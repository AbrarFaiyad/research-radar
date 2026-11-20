"""Leak scan: last gate before a build folder is published. Errs toward blocking.

Findings carry a rule name and a path, never the matched text. A file or folder
name that trips a rule is replaced by a placeholder in every finding.
"""

from __future__ import annotations

import base64
import contextlib
import getpass
import html
import math
import os
import platform
import re
import stat
import struct
import unicodedata
import zlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from radar.config import Config

ARCHIVE_EXT = frozenset(
    {".zip", ".tar", ".gz", ".7z", ".whl", ".egg", ".pkl", ".pt", ".npz", ".h5"}
)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_HARMLESS = frozenset(
    {b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"sBIT", b"pHYs", b"bKGD", b"hIST", b"tIME"}
)
PNG_TEXT = frozenset({b"tEXt", b"iTXt", b"zTXt"})
MIN_MARKER_LEN = 3
MB = 1024 * 1024
WINDOW = 16
MAX_INFLATE = 4 * MB

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
SECRET_PREFIX_RE = re.compile(
    r"^(?:sk-ant-[a-z]+\d*-|sk-ant-|sk-|ghp_|gho_|github_pat_|xox[baprs]-|AIza|AKIA)",
    re.IGNORECASE,
)
TOKEN_RE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
HEX_RE = re.compile(r"[0-9a-fA-F]+")
SRI_RE = re.compile(r"sha(?:256|384|512)-[A-Za-z0-9+/=]+")
DATA_PNG_RE = re.compile(r"data:image/png;base64,([A-Za-z0-9+/=\s]+)")
WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/]{1,2}Users[\\/]", re.IGNORECASE)
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


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold()


def _strip_ws(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _alnum(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s).lower()


def _variants(text: str) -> list[str]:
    """The text as written, URL-decoded, and HTML-unescaped, so encodings do not hide a match."""
    seen: list[str] = []
    for v in (
        text,
        unquote(text),
        html.unescape(text),
        html.unescape(unquote(text)),
        unquote(html.unescape(text)),
    ):
        if v not in seen:
            seen.append(v)
    return seen


def _windows(s: str) -> set[str]:
    if not s:
        return set()
    if len(s) <= WINDOW:
        return {s}
    return {s[i : i + WINDOW] for i in range(len(s) - WINDOW + 1)}


def _secret_forms(secret: str) -> tuple[set[str], set[bytes]]:
    """Search windows (lowercase alphanumeric) and UTF-16-LE byte windows for one secret."""
    rand = SECRET_PREFIX_RE.sub("", secret.strip())
    if not rand:
        return set(), set()
    raw = rand.encode("utf-8")
    forms = {_alnum(rand), raw.hex()}
    for off in range(3):
        enc = base64.b64encode(b"\x00" * off + raw).decode()
        forms.add(_alnum(enc[(0, 2, 3)[off] : -2]))
    wins: set[str] = set()
    for f in forms:
        wins |= _windows(f)
    wins.discard("")
    u16 = rand.encode("utf-16-le")
    step = 2 * WINDOW
    bwins = {u16[i : i + step] for i in range(0, max(len(u16) - step, 0) + 1, 2)}
    return wins, bwins


def _text_findings(
    label: str,
    text: str,
    markers: list[str],
    secrets: Sequence[str] = (),
    *,
    entropy: bool = True,
    hex_ok: bool = False,
) -> list[Finding]:
    found: list[Finding] = []
    variants = _variants(text)
    if any(SECRET_RE.search(v) for v in variants):
        found.append(Finding(label, "secret pattern"))
    elif secrets:
        stripped = [_alnum(v) for v in variants]
        for sec in secrets:
            wins, _ = _secret_forms(sec)
            if any(w in t for w in wins for t in stripped):
                found.append(Finding(label, "secret pattern (known key)"))
                break
    if entropy and _high_entropy(text, hex_ok):
        found.append(Finding(label, "secret pattern (high-entropy token)"))
    marks = [m for m in markers if len(m) >= MIN_MARKER_LEN]
    if marks:
        normed = []
        for v in variants:
            n = _norm(v)
            normed += [n, _strip_ws(n)]
        for m in marks:
            nm = _norm(m)
            keys = (nm, _strip_ws(nm))
            if any(k and k in t for k in keys for t in normed):
                found.append(Finding(label, "personal information"))
                break
    if any(WIN_PATH_RE.search(v) or UNIX_PATH_RE.search(v) for v in variants):
        found.append(Finding(label, "local path"))
    return found


def _high_entropy(text: str, hex_ok: bool) -> bool:
    text = SRI_RE.sub(" ", text)
    for tok in TOKEN_RE.findall(text):
        if hex_ok and HEX_RE.fullmatch(tok):
            continue  # pip --hash digest
        for seg in tok.split("/"):
            if (
                len(seg) >= 20
                and any(c.isdigit() for c in seg)
                and any(c.isupper() for c in seg)
                and any(c.islower() for c in seg)
                and _entropy(seg) >= 4.0
            ):
                return True
    return False


def _png_findings(
    label: str, data: bytes, markers: list[str], secrets: Sequence[str]
) -> list[Finding]:
    bad = [Finding(label, "binary")]
    if not data.startswith(PNG_MAGIC):
        return bad
    found: list[Finding] = []
    pos, first, ended = len(PNG_MAGIC), True, False
    while pos + 12 <= len(data) and not ended:
        (n,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + n]
        if len(body) != n or pos + 12 + n > len(data):
            return bad
        if first and (kind != b"IHDR" or n != 13):
            return bad
        first = False
        pos += 12 + n
        if kind in (b"IHDR", b"PLTE", b"IDAT") or kind in PNG_HARMLESS:
            continue
        if kind == b"IEND":
            ended = True
        elif kind in PNG_TEXT:
            try:
                text = _png_text(kind, body)
            except (zlib.error, ValueError):
                return bad
            found += _text_findings(label, text, markers, secrets)
        elif kind == b"eXIf":
            found += _text_findings(label, body.decode("latin-1"), markers, secrets, entropy=False)
        else:
            return bad  # unknown chunk could carry data
    if not ended or pos != len(data):
        return bad
    return found


def _inflate(blob: bytes) -> bytes:
    d = zlib.decompressobj()
    out = d.decompress(blob, MAX_INFLATE)
    if d.unconsumed_tail:
        raise ValueError("png text too large")
    return out


def _png_text(kind: bytes, body: bytes) -> str:
    key, sep, rest = body.partition(b"\x00")
    if not sep:
        raise ValueError("malformed png text")
    if kind == b"tEXt":
        payload = rest
    elif kind == b"zTXt":
        payload = _inflate(rest[1:])
    else:  # iTXt: flag, method, language\0, translated keyword\0, text
        flag, rest = rest[:1], rest[2:]
        _lang, _, rest = rest.partition(b"\x00")
        _tkey, _, rest = rest.partition(b"\x00")
        payload = _inflate(rest) if flag == b"\x01" else rest
    return (
        key.decode("latin-1")
        + " "
        + payload.decode("utf-8", errors="replace")
        + " "
        + (payload.decode("latin-1"))
    )


def _scan_data(
    label: str, name: str, data: bytes, markers: list[str], secrets: Sequence[str]
) -> list[Finding]:
    ext = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    if ext in ARCHIVE_EXT:
        return [Finding(label, "archive")]
    if secrets:
        bw = set()
        for sec in secrets:
            bw |= _secret_forms(sec)[1]
        if any(w in data for w in bw):
            return [Finding(label, "secret pattern (known key)")]
    if ext == ".png":
        return _png_findings(label, data, markers, secrets)
    if b"\x00" in data:
        return [Finding(label, "binary")]
    try:
        texts = [data.decode("utf-8")]
    except UnicodeDecodeError:
        texts = [data.decode("utf-8", errors="replace"), data.decode("latin-1")]
    hex_ok = PurePosixPath(name.replace("\\", "/")).name.lower().startswith("requirements")
    found: list[Finding] = []
    for i, text in enumerate(texts):
        # embedded PNGs are scanned as PNGs, not as high-entropy text
        def _blob(m: re.Match[str], found: list[Finding] = found) -> str:
            try:
                raw = base64.b64decode(_strip_ws(m.group(1)), validate=True)
            except ValueError:
                return m.group(0)
            found.extend(_png_findings(label, raw, markers, secrets))
            return " "

        text = DATA_PNG_RE.sub(_blob, text)
        found += _text_findings(label, text, markers, secrets, entropy=i == 0, hex_ok=hex_ok)
    return _unique(found)


def _unique(found: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str]] = set()
    out = []
    for f in found:
        if (f.path, f.reason) not in seen:
            seen.add((f.path, f.reason))
            out.append(f)
    return out


def scan_bytes(
    name: str, data: bytes, markers: list[str], secrets: Sequence[str] = ()
) -> list[Finding]:
    """Apply the file rules (type, binary, text) to one in-memory file."""
    return _scan_data(name, name, data, markers, secrets)


def _is_junction(path: str) -> bool:
    fn = getattr(os.path, "isjunction", None)
    return bool(fn and fn(path))


def scan_tree(
    root: Path,
    markers: list[str],
    max_file_mb: float,
    max_folder_mb: float,
    secrets: Sequence[str] = (),
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

    redacted = 0
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        cur, cur_disp = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            findings.append(Finding(cur_disp or ".", "unreadable"))
            continue
        for e in entries:
            comp = e.name
            bad_name = _text_findings(comp, comp, marks, secrets)
            if bad_name:
                redacted += 1
                comp = f"<redacted-name-{redacted}>"
            rel = f"{cur_disp}/{comp}" if cur_disp else comp
            for f in bad_name:
                findings.append(Finding(rel, f"{f.reason} (file name)"))
            try:
                st = os.lstat(e.path)  # DirEntry.stat() lacks st_nlink on Windows
            except OSError:
                findings.append(Finding(rel, "unreadable"))
                continue
            if stat.S_ISLNK(st.st_mode) or _is_junction(e.path):
                findings.append(Finding(rel, "link"))
            elif stat.S_ISDIR(st.st_mode):
                stack.append((Path(e.path), rel))
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
                findings.extend(_scan_data(rel, e.name, data, marks, secrets))
    if total > max_folder_mb * MB:
        findings.append(Finding(".", "folder too large"))
    return findings
