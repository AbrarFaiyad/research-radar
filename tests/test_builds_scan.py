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


def test_png_metadata_scanned(root):
    (root / "p.png").write_bytes(make_png(_chunk(b"tEXt", b"Comment\x00" + FAKE_KEY.encode())))
    assert "secret pattern" in _reasons(_scan(root))


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


def test_png_chunks():
    z = _chunk(b"zTXt", b"Comment\x00\x00" + zlib.compress(FAKE_KEY.encode()))
    assert "secret pattern" in _reasons(scan.scan_bytes("p.png", make_png(z), []))
    i = _chunk(b"iTXt", b"k\x00\x01\x00en\x00k\x00" + zlib.compress(MARK.encode()))
    assert "personal information" in _reasons(scan.scan_bytes("p.png", make_png(i), [MARK]))
    assert "binary" in _reasons(scan.scan_bytes("p.png", make_png(_chunk(b"zzZz", b"x")), []))
    assert "binary" in _reasons(scan.scan_bytes("p.png", PNG + b"trailing", []))
    assert scan.scan_bytes("p.png", make_png(_chunk(b"pHYs", b"\x00" * 9)), [MARK]) == []


def test_pixel_data_not_marker_scanned():
    assert scan.scan_bytes("p.png", PNG, ["IHD", "IDA", "IEN"]) == []


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
    assert "secret pattern" in _reasons(scan.scan_bytes("a.svg", bad, []))


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
