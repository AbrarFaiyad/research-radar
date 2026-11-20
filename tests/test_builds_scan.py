import os
import struct
import zlib

import pytest

from radar.builds import scan

FAKE_KEY = "sk-ant-api03-" + "x" * 40
MARK = "fakeowner.example"
PNG = (
    b"\x89PNG\r\n\x1a\n"
    + struct.pack(">I", 13)
    + b"IHDR"
    + b"\x00" * 13
    + struct.pack(">I", zlib.crc32(b"IHDR" + b"\x00" * 13))
)


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
    (root / "requirements.txt").write_text(f"pkg==1.0 \\n    --hash=sha256:{h}\n", encoding="utf-8")
    assert _scan(root) == []


def test_personal_marker_case_insensitive(root):
    (root / "BUILD_LOG.md").write_text(f"mail {MARK.upper()}\n", encoding="utf-8")
    f = _scan(root)
    assert "personal information" in _reasons(f) and MARK not in str(f).lower()


def test_marker_in_file_name(root):
    (root / f"{MARK}.txt").write_text("x", encoding="utf-8")
    f = _scan(root)
    assert "personal information" in _reasons(f)


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


def test_hard_link(root, tmp_path):
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
    (root / "p.png").write_bytes(PNG + b"tEXt" + FAKE_KEY.encode())
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
    from pathlib import Path

    home = str(Path.home())
    assert home in m and home.replace("\\", "/") in m
    assert all(len(x) >= 3 for x in m)
