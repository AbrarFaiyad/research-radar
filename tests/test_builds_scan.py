import base64
import os
import struct
import zlib
from pathlib import Path

import pytest

from radar.builds import scan

FAKE_KEY = "sk-ant-api03-" + "x" * 40
MARK = "fakeowner.example"


def _chunk(kind, body=b""):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def make_png(*extra):
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + b"".join(extra)
        + _chunk(b"IDAT", zlib.compress(b"\x00\x00"))
        + _chunk(b"IEND")
    )


PNG = make_png()


def _scan(root, **kw):
    return scan.scan_tree(root, kw.pop("markers", [MARK]), kw.pop("mb", 5), kw.pop("fmb", 20))


def _reasons(findings):
    return " | ".join(f.reason for f in findings)


@pytest.fixture
def root(tmp_path):
    d = tmp_path / "build"
    d.mkdir()
    (d / "README.md").write_text("# Demo\nA clean tool.\n", encoding="utf-8")
    (d / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return d


def test_clean_folder(root):
    assert _scan(root) == []


def test_secret_pattern_and_no_leak(root):
    (root / "cfg.txt").write_text(f"key = {FAKE_KEY}\n", encoding="utf-8")
    f = _scan(root)
    assert len(f) == 1 and "secret pattern" in f[0].reason and f[0].path == "cfg.txt"
    assert "api03" not in str(f) and "xxxx" not in str(f)


@pytest.mark.parametrize(
    "text",
    [
        "ghp_" + "A1b2" * 6,
        "github_pat_" + "A1b2" * 6,
        "AKIA" + "ABCDEFGH12345678",
        "-----BEGIN RSA PRIVATE KEY-----",
        "xoxb-1234567890-abc",
        "AIza" + "A" * 35,
    ],
)
def test_other_secret_patterns(root, text):
    (root / "a.txt").write_text(text, encoding="utf-8")
    assert "secret pattern" in _reasons(_scan(root))


def test_high_entropy_token(root):
    (root / "a.txt").write_text("t = aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5bC7d\n", encoding="utf-8")
    assert "secret pattern" in _reasons(_scan(root))


def test_requirements_hash_not_flagged(root):
    h = "0123456789abcdef" * 4
    (root / "requirements.txt").write_text(
        f"pkg==1.0 \\\n    --hash=sha256:{h}\n", encoding="utf-8"
    )
    assert _scan(root) == []


def test_personal_marker_case_insensitive(root):
    (root / "BUILD_LOG.md").write_text(f"mail {MARK.upper()}\n", encoding="utf-8")
    f = _scan(root)
    assert "personal information" in _reasons(f) and MARK not in str(f).lower()


def test_marker_in_file_name(root):
    (root / f"me@{MARK}.txt").write_text("x", encoding="utf-8")
    f = _scan(root)
    assert "personal information" in _reasons(f) and MARK not in str(f).lower()
    assert f[0].path == "<redacted-name-1>"


def test_marker_name_redacted_below_directory(root):
    (root / MARK).mkdir()
    (root / MARK / "k.txt").write_text(FAKE_KEY, encoding="utf-8")
    f = _scan(root)
    assert MARK not in str(f).lower() and "api03" not in str(f)
    assert any(x.path == "<redacted-name-1>/k.txt" for x in f)


def test_secret_in_file_name(root):
    (root / (FAKE_KEY + ".txt")).write_text("x", encoding="utf-8")
    f = _scan(root)
    assert "secret pattern" in _reasons(f) and "api03" not in str(f)


def test_short_and_empty_markers_ignored(root):
    assert _scan(root, markers=["", "a", "ab"]) == []


LOCAL_PATHS = [
    "C:" + "\\Users\\someone\\x",
    "c:/users/someone",
    "C:" + "\\\\Users\\\\x",
    "/Users/bob/x",
    "/home/bob/x",
]


@pytest.mark.parametrize("p", LOCAL_PATHS)
def test_local_path(root, p):
    (root / "a.md").write_text(f"see {p}\n", encoding="utf-8")
    assert "local path" in _reasons(_scan(root))


def test_symlink(root, tmp_path):
    (tmp_path / "t.txt").write_text("x", encoding="utf-8")
    try:
        os.symlink(tmp_path / "t.txt", root / "ln.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted")
    assert "link" in _reasons(_scan(root))


def test_hard_link(root):
    try:
        os.link(root / "main.py", root / "hl.py")
    except OSError:
        pytest.skip("hard links not permitted")
    assert "hard link" in _reasons(_scan(root))


def test_file_too_large(root):
    (root / "big.txt").write_bytes(b"a " * (3 * 1024 * 1024))
    assert "file too large" in _reasons(_scan(root))


def test_folder_too_large(root):
    for i in range(3):
        (root / f"f{i}.txt").write_bytes(b"a " * (600 * 1024))
    assert "folder too large" in _reasons(_scan(root, mb=5, fmb=3))


@pytest.mark.parametrize("ext", ["zip", "tar", "gz", "7z", "whl", "egg", "pkl", "pt", "npz", "h5"])
def test_archive(root, ext):
    (root / f"a.{ext}").write_bytes(b"data")
    assert "archive" in _reasons(_scan(root))


def test_binary_and_png(root):
    (root / "a.bin").write_bytes(b"ab\x00cd")
    assert "binary" in _reasons(_scan(root))
    (root / "a.bin").unlink()
    (root / "p.png").write_bytes(PNG)
    (root / "s.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    assert _scan(root) == []


def test_fake_png_blocked(root):
    (root / "p.png").write_bytes(b"not a png")
    assert "binary" in _reasons(_scan(root))


def test_scan_bytes_same_rules():
    f = scan.scan_bytes("index.md", f"x {FAKE_KEY} {MARK}".encode(), [MARK])
    assert {"secret pattern", "personal information"} <= {x.reason for x in f}
    assert scan.scan_bytes("index.md", b"clean", [MARK]) == []
    assert "archive" in _reasons(scan.scan_bytes("a.zip", b"x", [MARK]))


def test_personal_markers(cfg, monkeypatch):
    cfg.email.to = "me@fakeowner.example"
    cfg.email.sender = ""
    monkeypatch.setenv("RADAR_PRIVATE_MARKERS", "555-0100, Jane Q Fake ,")
    m = scan.personal_markers(cfg)
    assert "me@fakeowner.example" in m and "555-0100" in m and "Jane Q Fake" in m
    home = str(Path.home())
    assert home in m and home.replace("\\", "/") in m
    assert all(len(x) >= 3 for x in m)


def test_utf16_after_padding(root):
    (root / "a.txt").write_bytes(("\u4e01" * 5000 + FAKE_KEY).encode("utf-16-le"))
    assert "binary" in _reasons(_scan(root))


def test_nul_late_still_binary():
    assert "binary" in _reasons(scan.scan_bytes("a.txt", b"a" * 9000 + b"\x00b", []))


@pytest.mark.parametrize(
    "data",
    [
        b"me%40fakeowner.example",
        b"me&#64;fakeowner.example",
        b"me@fakeowner.\nexample",
        b"C:%5CUsers%5Cbob",
        b"C:\\Users/bob",
    ],
)
def test_encoded_bypasses(data):
    f = scan.scan_bytes("a.txt", data, [MARK])
    assert f and MARK not in str(f)


def test_accented_marker_forms():
    m = ["Jos\u00e9 Fake"]
    assert scan.scan_bytes("a.txt", "Jos\u00e9 Fake".encode("cp1252"), m)
    assert scan.scan_bytes("a.txt", "Jose\u0301 Fake".encode("utf-8"), m)
    assert scan.scan_bytes("a.txt", "JOSE\u0301 fake".encode("utf-8"), m)


def _p(*extra, idat=None):
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + b"".join(extra)
        + (idat if idat is not None else _chunk(b"IDAT", zlib.compress(b"\x00\x00")))
        + _chunk(b"IEND")
    )


KEYB = b"sk-ant-api03-" + b"Ab3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5bC7d"
EMAIL = ("me@" + MARK).encode()


@pytest.mark.parametrize(
    "data",
    [
        _p(_chunk(b"tIME", KEYB)),
        _p(_chunk(b"pHYs", EMAIL)),
        _p(_chunk(b"tRNS", b"\x00" * 257)),
        _p(_chunk(b"PLTE", b"\x00" * 770)),
        _p(_chunk(b"PLTE", b"\x00" * 4)),
        _p(_chunk(b"gAMA", b"\x00" * 5)),
        _p(idat=_chunk(b"IDAT", KEYB)),
        _p(_chunk(b"zTXt", b"k\x00\x00" + zlib.compress(b"x") + KEYB)),
        _p(_chunk(b"zTXt", b"k\x00\x00" + zlib.compress(b"x") + zlib.compress(KEYB))),
        _p(_chunk(b"iTXt", b"k\x00\x00\x00" + EMAIL + b"\x00t\x00hello")),
        _p(_chunk(b"tEXt", b"k\x00" + ("me@" + MARK).encode("utf-16-le"))),
        _p(_chunk(b"eXIf", b"MM\x00*" + KEYB.decode().encode("utf-16-le"))),
        _p(_chunk(b"zzZz", b"x")),
        _p(idat=_chunk(b"IDAT", zlib.compress(b"ab") + b"junk")),
        _p(idat=_chunk(b"IDAT", zlib.compress(b"ab")[:-2])),
        _p(idat=_chunk(b"IDAT", zlib.compress(b"ab")) + _chunk(b"IDAT", zlib.compress(b"cd"))),
        _p() + b"trailing",
        _p()[:-3],
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IEND"),
    ],
)
def test_png_strict_blocks(data):
    f = scan.scan_bytes("p.png", data, [MARK], secrets=[KEYB.decode()])
    assert f and "Ab3dE5" not in str(f)


def test_png_bad_crc_blocked():
    data = bytearray(_p())
    data[20] ^= 0xFF
    assert scan.scan_bytes("p.png", bytes(data), []) == [scan.Finding("p.png", "binary")]


def _pc(colour, *extra, idat=None):
    """Like _p but with a chosen IHDR colour type."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, colour, 0, 0, 0))
        + b"".join(extra)
        + (idat if idat is not None else _chunk(b"IDAT", zlib.compress(b"\x00\x00")))
        + _chunk(b"IEND")
    )


def _split(body, n, kind):
    body = body + b"\x00" * (-len(body) % n)
    return [_chunk(kind, body[i : i + n]) for i in range(0, len(body), n)]


def test_png_key_in_plte_caught():
    pad = b"\x00" * (-len(KEYB) % 3)
    f = scan.scan_bytes("p.png", _pc(2, _chunk(b"PLTE", KEYB + pad)), [], secrets=[KEYB.decode()])
    assert "secret pattern" in _reasons(f)


@pytest.mark.parametrize(
    "data",
    [
        _pc(2, *_split(KEYB, 3, b"PLTE")),
        _pc(3, *_split(KEYB, 3, b"PLTE")),
        _pc(0, *_split(KEYB, 1, b"tRNS")),
        _pc(3, _chunk(b"PLTE", b"\x00" * 30), *_split(KEYB, 1, b"tRNS")),
        _pc(2, *_split(b"C:\\Users\\bob\\x", 3, b"PLTE")),
        _pc(2, *[_chunk(b"PLTE", b"\x01" * 768)] * 10),
        _pc(2, _chunk(b"PLTE", b"abc"), _chunk(b"PLTE", b"def")),
        _pc(3, _chunk(b"PLTE", b"abc"), _chunk(b"tRNS", b"\x01"), _chunk(b"tRNS", b"\x01")),
        _pc(2, idat=_chunk(b"IDAT", zlib.compress(b"\x00\x00")) + _chunk(b"PLTE", b"abc")),
        _pc(2, _chunk(b"tRNS", b"\x00" * 6), _chunk(b"PLTE", b"abc")),
        _pc(3, _chunk(b"PLTE", b"abc"), _chunk(b"pHYs", b"\x00" * 9), _chunk(b"pHYs", b"\x00" * 9)),
        _pc(3),
        _pc(4, _chunk(b"tRNS", b"\x00\x00")),
        _pc(6, _chunk(b"tRNS", b"\x00" * 6)),
        _pc(0, _chunk(b"PLTE", b"abc")),
        _pc(0, _chunk(b"tRNS", b"\x00")),
        _pc(3, _chunk(b"PLTE", b"abc"), _chunk(b"tRNS", b"\x00\x00")),
        _pc(5),
    ],
)
def test_png_structure_blocks(data):
    f = scan.scan_bytes("p.png", data, [], secrets=[KEYB.decode()])
    assert [x.reason for x in f] == ["binary"]


def test_png_structure_blocks_known_key_split():
    f = scan.scan_bytes("p.png", _pc(2, *_split(KEYB, 3, b"PLTE")), [], secrets=[KEYB.decode()])
    assert f


def test_normal_palette_and_rgba_pngs_pass():
    pal = _pc(
        3,
        _chunk(b"gAMA", b"\x00" * 4),
        _chunk(b"PLTE", b"\x01\x02\x03" * 4),
        _chunk(b"tRNS", b"\xff\x80"),
        _chunk(b"pHYs", b"\x00" * 9),
    )
    assert scan.scan_bytes("p.png", pal, [MARK], secrets=[KEYB.decode()]) == []
    assert scan.scan_bytes("p.png", _pc(6), [MARK], secrets=[KEYB.decode()]) == []
    rgb = _pc(
        2, _chunk(b"sRGB", b"\x00"), _chunk(b"PLTE", b"\x01\x02\x03"), _chunk(b"tRNS", b"\x00" * 6)
    )
    assert scan.scan_bytes("p.png", rgb, [MARK]) == []
    assert scan.scan_bytes("p.png", _pc(0, _chunk(b"tRNS", b"\x00\x00")), []) == []


def test_png_key_in_pixels_caught_with_secrets():
    raw = _chunk(b"IDAT", zlib.compress(KEYB))
    f = scan.scan_bytes("p.png", _p(idat=raw), [], secrets=[KEYB.decode()])
    assert "known key" in _reasons(f)
    # without a known key, pixel data is not text-scanned
    assert scan.scan_bytes("p.png", _p(idat=raw), [MARK]) == []


def test_png_inflate_bomb_bounded(monkeypatch):
    monkeypatch.setattr(scan, "PNG_BUDGET", 1024 * 1024)
    bomb = zlib.compress(b"a" * (4 * 1024 * 1024), 9)
    chunks = [_chunk(b"IDAT", bomb[i : i + 200]) for i in range(0, len(bomb), 200)]
    assert "binary" in _reasons(scan.scan_bytes("p.png", _p(idat=b"".join(chunks)), []))


def test_sanitize_png_strips_metadata_keeps_pixels():
    dirty = _p(
        _chunk(b"tEXt", b"Software\x00matplotlib"),
        _chunk(b"tIME", b"\x00" * 7),
        _chunk(b"eXIf", b"MM\x00*"),
        _chunk(b"iTXt", b"k\x00\x00\x00\x00\x00x"),
        _chunk(b"pHYs", b"\x00" * 9),
    )
    assert "binary" in _reasons(scan.scan_bytes("p.png", dirty, []))
    clean = scan.sanitize_png(dirty)
    assert scan.scan_bytes("p.png", clean, []) == []
    assert b"tEXt" not in clean and b"tIME" not in clean and b"pHYs" in clean
    assert clean == _p(_chunk(b"pHYs", b"\x00" * 9))
    assert scan.sanitize_png(dirty + b"tail") == clean


def test_sanitize_png_rejects_malformed():
    corrupt = bytearray(_p())
    corrupt[20] ^= 0xFF
    for bad in (b"nope", _p()[:-3], bytes(corrupt)):
        with pytest.raises(ValueError):
            scan.sanitize_png(bad)


def test_pixel_data_not_marker_scanned():
    assert scan.scan_bytes("p.png", PNG, ["IHD", "IDA", "IEN", "IHDRIDAT"]) == []


@pytest.mark.parametrize(
    "line",
    [
        "See https://github.com/materialsproject/pymatgen/blob/master/pymatgen/core/structure.py",
        "https://github.com/ACEsuit/mace/tree/main/mace/calculators/foundations_models",
        "https://huggingface.co/datasets/fairchem/OMAT24/resolve/main/train",
        "--8<-- docs/MaterialsProjectStructureRelaxationTutorial",
        '"integrity": "sha512-v2kDEe57lecTulaDIuNTPy3Ry4gLGJ6Z1O3vE1krgXZNrsQ+LFTGHVxVjcXPs17LhbZVGedAJv8XZ1tvj5FvSg=="',
    ],
)
def test_ordinary_content_passes(line):
    assert scan.scan_bytes("README.md", line.encode(), []) == []


def test_svg_embedded_png():
    prefix = b'<svg><image href="data:image/png;base64,'
    ok = prefix + base64.b64encode(PNG) + b'"/></svg>'
    assert scan.scan_bytes("a.svg", ok, []) == []
    bad_png = make_png(_chunk(b"tEXt", b"k\x00" + FAKE_KEY.encode()))
    bad = prefix + base64.b64encode(bad_png) + b'"/></svg>'
    assert "binary" in _reasons(scan.scan_bytes("a.svg", bad, []))


def test_base64_encoded_fake_key_caught():
    enc = base64.b64encode(("sk-ant-api03-" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1x").encode())
    assert "secret pattern" in _reasons(scan.scan_bytes("a.txt", enc, []))


REAL = "sk-ant-api03-" + "Ab3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5bC7d"


@pytest.mark.parametrize(
    "make",
    [
        lambda k: k.encode().hex().encode(),
        lambda k: base64.b64encode(k.encode()),
        lambda k: base64.b64encode(b"x" + k.encode()),
        lambda k: base64.b64encode(b"xy" + k.encode()),
        lambda k: base64.b64encode(k.encode()).replace(b"A", b"a"),
        lambda k: ("(" + " ".join(repr(k[i : i + 6]) for i in range(0, len(k), 6)) + ")").encode(),
        lambda k: k.lower().encode(),
        lambda k: k.replace("-", " ").encode(),
        lambda k: k[13:].encode(),
        lambda k: k.encode("utf-16-le"),
    ],
)
def test_known_secret_forms(make):
    f = scan.scan_bytes("a.txt", make(REAL), [], secrets=[REAL])
    assert f and "Ab3dE5" not in str(f)


def test_known_secret_clean_and_tree(root):
    assert scan.scan_bytes("a.txt", b"nothing here", [], secrets=[REAL]) == []
    (root / "x.txt").write_text(REAL.encode().hex(), encoding="utf-8")
    assert scan.scan_tree(root, [], 5, 20, secrets=[REAL])


def test_too_many_files(tmp_path):
    for i in range(6):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    assert scan.scan_tree(tmp_path, [], 5, 20, max_files=6) == []
    found = scan.scan_tree(tmp_path, [], 5, 20, max_files=5)
    assert [(f.path, f.reason) for f in found] == [(".", "too many files")]
    assert scan.MAX_FILES == 2000
