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
from typing import TYPE_CHECKING
from urllib.parse import unquote

if TYPE_CHECKING:  # scan.py is vendored standalone into the workflow repo
    from radar.config import Config

ARCHIVE_EXT = frozenset(
    {".zip", ".tar", ".gz", ".7z", ".whl", ".egg", ".pkl", ".pt", ".npz", ".h5"}
)
MB = 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_KEEP = frozenset(
    {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND", b"gAMA", b"cHRM", b"sRGB", b"pHYs"}
)
PNG_SIZES = {b"IHDR": 13, b"IEND": 0, b"gAMA": 4, b"cHRM": 32, b"sRGB": 1, b"pHYs": 9}
PNG_BUDGET = 64 * MB  # total inflate allowance per file
PNG_MARKER_LEN = 8  # shorter markers would match chunk bytes by chance
MIN_MARKER_LEN = 3
MAX_FILES = 2000  # a learning kit has a handful of files; thousands means a venv or a clone
WINDOW = 16

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


def _png_chunks(data: bytes) -> list[tuple[bytes, bytes, bytes]]:
    """Parse and validate chunk framing and CRCs; return (kind, body, raw) per chunk up to IEND."""
    if not data.startswith(PNG_MAGIC):
        raise ValueError("not a png")
    out: list[tuple[bytes, bytes, bytes]] = []
    pos = len(PNG_MAGIC)
    while True:
        if pos + 12 > len(data):
            raise ValueError("truncated png")
        (n,) = struct.unpack(">I", data[pos : pos + 4])
        end = pos + 12 + n
        if end > len(data):
            raise ValueError("truncated png")
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + n]
        (crc,) = struct.unpack(">I", data[end - 4 : end])
        if zlib.crc32(kind + body) != crc:
            raise ValueError("bad crc")
        if not out and (kind != b"IHDR" or n != 13):
            raise ValueError("IHDR must come first")
        out.append((kind, body, data[pos:end]))
        pos = end
        if kind == b"IEND":
            return out


def sanitize_png(data: bytes) -> bytes:
    """Rebuild a PNG keeping only the pixel-bearing and harmless chunks (drops text, EXIF, time)."""
    chunks = _png_chunks(data)
    return PNG_MAGIC + b"".join(raw for kind, _body, raw in chunks if kind in PNG_KEEP)


def _png_pixels(chunks: list[tuple[bytes, bytes, bytes]]) -> bytes:
    """Strict structure check (counts, order, colour-type rules); return the inflated pixels."""
    if len(chunks) < 3 or chunks[-1][0] != b"IEND":
        raise ValueError("bad chunk order")
    colour = chunks[0][1][9]
    if colour not in (0, 2, 3, 4, 6):
        raise ValueError("bad colour type")
    seen: set[bytes] = set()
    idat: list[bytes] = []
    palette = 0
    for kind, body, _raw in chunks:
        if kind not in PNG_KEEP:
            raise ValueError("chunk not allowed")
        if kind == b"IDAT":
            idat.append(body)
            continue
        if kind in seen:
            raise ValueError("repeated chunk")
        seen.add(kind)
        if idat and kind != b"IEND":
            raise ValueError("chunk after pixel data")
        size = PNG_SIZES.get(kind)
        if size is not None and len(body) != size:
            raise ValueError("bad chunk length")
        if kind == b"PLTE":
            if (
                b"tRNS" in seen
                or colour not in (2, 3, 6)
                or len(body) % 3
                or not 3 <= len(body) <= 768
            ):
                raise ValueError("bad palette")
            palette = len(body) // 3
        elif kind == b"tRNS":
            want = {0: 2, 2: 6}.get(colour)
            if colour == 3:
                if not 1 <= len(body) <= palette:
                    raise ValueError("bad transparency")
            elif want is None or len(body) != want:
                raise ValueError("bad transparency")
    if not idat:
        raise ValueError("no pixels")
    if colour == 3 and b"PLTE" not in seen:
        raise ValueError("missing palette")
    d = zlib.decompressobj()
    pixels = d.decompress(b"".join(idat), PNG_BUDGET)
    if d.unconsumed_tail or not d.eof or d.unused_data:
        raise ValueError("bad pixel stream")
    return pixels


def _known_in_bytes(blob: bytes, secrets: Sequence[str]) -> bool:
    text = _alnum(blob.decode("latin-1"))
    for sec in secrets:
        wins, bwins = _secret_forms(sec)
        if any(w in text for w in wins) or any(w in blob for w in bwins):
            return True
    return False


def _png_findings(
    label: str, data: bytes, markers: list[str], secrets: Sequence[str]
) -> list[Finding]:
    """A PNG passes only if it is exactly pixels plus harmless chunks; nothing else may ride along."""
    try:
        chunks = _png_chunks(data)
        if len(PNG_MAGIC) + sum(len(raw) for _, _, raw in chunks) != len(data):
            raise ValueError("data after IEND")
        pixels = _png_pixels(chunks)
    except (ValueError, zlib.error):
        return [Finding(label, "binary")]
    found: list[Finding] = []
    meta = [body for kind, body, _ in chunks if kind != b"IDAT"]
    if secrets and any(_known_in_bytes(b, secrets) for b in [data, pixels, b"".join(meta), *meta]):
        found.append(Finding(label, "secret pattern (known key)"))
    text = "".join(b.decode("latin-1") for b in meta)
    long_marks = [m for m in markers if len(m) >= PNG_MARKER_LEN]
    found += _text_findings(label, text, long_marks, (), entropy=False)
    return found


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
    max_files: int = MAX_FILES,
) -> list[Finding]:
    root = Path(root)
    findings: list[Finding] = []
    total = 0
    files = 0
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
                files += 1
                if files > max_files:
                    findings.append(Finding(".", "too many files"))
                    return findings
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
