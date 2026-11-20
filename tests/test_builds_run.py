import contextlib
import datetime as dt
import json
import struct
import zlib
from types import SimpleNamespace

import pytest

from radar.builds import run as run_mod
from radar.builds.run import BuildRecord, render_prompt, run_build

DAY = dt.date(2026, 11, 21)
KEY = "sk-ant-api03-" + "q" * 40
MARKERS = ["zed-private-marker", "owner@fakemail.example", "Fakeowner Person"]


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


def good_build(work):
    (work / "README.md").write_text("# Demo\nHello.\n", encoding="utf-8")
    (work / "demo.py").write_text("print('hi')\n", encoding="utf-8")
    (work / "test_demo.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (work / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
    (work / "BUILD_LOG.md").write_text("log\n", encoding="utf-8")
    (work / ".logs").mkdir()
    (work / ".logs" / "claude.log").write_text("log text", encoding="utf-8")
    (work / "__pycache__").mkdir()
    (work / "__pycache__" / "x.pyc").write_bytes(b"\x00")


def fake_docker(*, ready=(True, ""), on_build=good_build, verify=(True, True), seen=None):
    seen = seen if seen is not None else {}

    @contextlib.contextmanager
    def env_file(key):
        seen["key"] = key
        yield "ENVFILE"

    def run_container(argv, timeout_s):
        workdir = argv[-1]
        from pathlib import Path

        work = Path(workdir)
        if argv[0] == "build":
            seen["task_files"] = sorted(
                p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file()
            )
            seen["blob"] = "\n".join(
                p.read_text(encoding="utf-8") for p in work.rglob("*") if p.is_file()
            )
            on_build(work)
            (work / ".task").mkdir(exist_ok=True)  # trap removes it in the real entry.sh
            import shutil

            shutil.rmtree(work / ".task")
        else:
            seen["verify_ran"] = True
            (work / ".verify.json").write_text(
                json.dumps({"installed": verify[0], "tests_passed": verify[1]}), encoding="utf-8"
            )
        return 0, ""

    return SimpleNamespace(
        docker_ready=lambda: ready,
        ensure_network_and_proxy=lambda cfg: None,
        ensure_image=lambda cfg: None,
        api_key_env_file=env_file,
        build_argv=lambda cfg, work, name, env_file=None: ["build", name, str(work)],
        verify_argv=lambda cfg, work, name: ["verify", name, str(work)],
        run_container=run_container,
    )


class FakePublisher:
    def __init__(self, url="https://github.com/o/r/tree/main/x", exc=None):
        self.calls = []
        self.url = url
        self.exc = exc

    def __call__(self, cfg, work, record, markers, secrets):
        self.calls.append((work, record, list(secrets)))
        if self.exc:
            raise self.exc
        return self.url


def item(**kw):
    base = {
        "title": "MACE LoRA finetune",
        "kind": "paper",
        "code_url": "https://github.com/ACEsuit/mace",
        "license": "MIT",
        "cpu_demo": True,
        "what": "w",
        "relevance": "zed-private-marker core",
        "post_ids": ["p1"],
    }
    base.update(kw)
    return base


@pytest.fixture
def result():
    return {
        "items": [item()],
        "rejected": [{"post_id": "p2", "reason": "zed-private-marker"}],
        "podcast_focus": "owner@fakemail.example",
    }


@pytest.fixture
def digest(tmp_path):
    d = tmp_path / "digests" / "2026-11-21"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def on_cfg(cfg):
    cfg.builds.enabled = True
    cfg.builds.repo = "owner/daily"
    return cfg


def go(cfg, digest, result, **kw):
    kw.setdefault("api_key", KEY)
    kw.setdefault("docker", fake_docker())
    kw.setdefault("publisher", FakePublisher())
    kw.setdefault("today", DAY)
    kw.setdefault("log", lambda *_: None)
    return run_build(cfg, digest, result, **kw)


@pytest.fixture
def private_world(on_cfg, monkeypatch):
    monkeypatch.setenv("RADAR_PRIVATE_MARKERS", "zed-private-marker")
    on_cfg.email.to = "owner@fakemail.example"
    onboarding = on_cfg.data_dir / "onboarding"
    onboarding.mkdir(parents=True)
    (onboarding / "cv.txt").write_text("CV of Fakeowner Person", encoding="utf-8")
    queue = on_cfg.queue_dir
    queue.mkdir(parents=True)
    (queue / "q.json").write_text('{"author": "Fakeowner Person"}', encoding="utf-8")
    return SimpleNamespace(markers=MARKERS)


def test_container_input_has_no_private_data(on_cfg, digest, result, private_world):
    seen = {}
    rec = go(on_cfg, digest, result, docker=fake_docker(seen=seen))
    blob = seen["blob"]
    assert "MACE LoRA finetune" in blob
    assert not any(m in blob for m in private_world.markers)
    for word in ("relevance", "post_ids", "rejected", "podcast_focus", "cpu_demo"):
        assert word not in blob
    assert seen["task_files"] == [".task/item.json", ".task/prompt.md"]
    assert KEY not in blob
    assert rec.status == "published"


def test_status_paths(on_cfg, digest, result):
    assert go(on_cfg, digest, result, api_key=None).status == "skipped"
    rec = go(on_cfg, digest, result, docker=fake_docker(ready=(False, "docker not running")))
    assert (rec.status, rec.reason) == ("skipped", "docker not running")


def test_skip_disabled_and_no_repo(cfg, digest, result):
    assert go(cfg, digest, result).reason == "builds disabled"
    cfg.builds.enabled = True
    assert go(cfg, digest, result).reason == "no target repo"


def test_no_candidate(on_cfg, digest):
    rec = go(on_cfg, digest, {"items": []})
    assert rec.status == "skipped" and "no item" in rec.reason


def test_verify_fails_marks_did_not_run(on_cfg, digest, result):
    pub = FakePublisher()
    rec = go(on_cfg, digest, result, docker=fake_docker(verify=(True, False)), publisher=pub)
    assert rec.status == "did_not_run"
    work, passed, _ = pub.calls[0]
    assert (work / "README.md").read_text(encoding="utf-8").startswith("> **DID NOT RUN.**")
    assert not (work / ".verify.json").exists()
    assert rec.url


def test_leak_blocks_publish(on_cfg, digest, result, private_world):
    def leak(work):
        good_build(work)
        (work / "README.md").write_text("mail owner@fakemail.example\n", encoding="utf-8")

    pub = FakePublisher()
    rec = go(on_cfg, digest, result, docker=fake_docker(on_build=leak), publisher=pub)
    assert rec.status == "blocked" and pub.calls == []
    assert "personal information" in rec.reason
    assert "fakemail" not in rec.reason and "README" not in rec.reason


def test_key_in_output_blocks(on_cfg, digest, result):
    def leak(work):
        good_build(work)
        (work / "out.txt").write_text(f"k={KEY}", encoding="utf-8")

    pub = FakePublisher()
    rec = go(on_cfg, digest, result, docker=fake_docker(on_build=leak), publisher=pub)
    assert rec.status == "blocked" and pub.calls == []
    assert KEY not in json.dumps(rec.__dict__)


def test_missing_readme_or_py_fails(on_cfg, digest, result):
    def no_readme(work):
        (work / "demo.py").write_text("x=1\n", encoding="utf-8")

    def no_py(work):
        (work / "README.md").write_text("x\n", encoding="utf-8")

    for fn in (no_readme, no_py):
        pub = FakePublisher()
        rec = go(on_cfg, digest, result, docker=fake_docker(on_build=fn), publisher=pub)
        assert rec.status == "failed" and pub.calls == []


def test_logs_moved_and_caches_removed(on_cfg, digest, result):
    pub = FakePublisher()
    rec = go(on_cfg, digest, result, publisher=pub)
    work = pub.calls[0][0]
    names = {p.name for p in work.rglob("*")}
    assert ".logs" not in names and "__pycache__" not in names and ".task" not in names
    log = on_cfg.builds_dir / "logs" / rec.folder / "claude.log"
    assert log.read_text(encoding="utf-8") == "log text"


def test_png_sanitized_and_malformed_removed(on_cfg, digest, result):
    text_chunk = _chunk(b"tEXt", b"Comment\x00hello")

    def pngs(work):
        good_build(work)
        (work / "output").mkdir()
        (work / "output" / "ok.png").write_bytes(make_png(text_chunk))
        (work / "output" / "bad.png").write_bytes(b"not a png")

    pub = FakePublisher()
    rec = go(on_cfg, digest, result, docker=fake_docker(on_build=pngs), publisher=pub)
    work = pub.calls[0][0]
    assert rec.status == "published"
    assert b"tEXt" not in (work / "output" / "ok.png").read_bytes()
    assert not (work / "output" / "bad.png").exists()
    assert "malformed image" in (work / "BUILD_LOG.md").read_text(encoding="utf-8")


def test_one_published_build_per_day(on_cfg, digest, result, tmp_path):
    assert go(on_cfg, digest, result).status == "published"
    second = tmp_path / "digests" / "2026-11-21-2"
    second.mkdir()
    rec = go(on_cfg, second, {"items": [item(code_url="https://github.com/o/other")]})
    assert (rec.status, rec.reason) == ("skipped", "already published today")


def test_build_json_written(on_cfg, digest, result):
    rec = go(on_cfg, digest, result)
    data = json.loads((digest / "build.json").read_text(encoding="utf-8"))
    assert set(data) == {"day", "status", "package", "folder", "url", "seconds"}
    assert data["status"] == "published" and data["package"] == "mace"
    assert data["folder"].startswith("2026-11-21_mace_")
    assert (on_cfg.builds_dir / "records" / "2026-11-21.json").is_file()
    assert rec.url == data["url"]


def test_blocked_build_json_keeps_reason(on_cfg, digest, result):
    go(on_cfg, digest, result, docker=fake_docker(on_build=lambda w: None))
    data = json.loads((digest / "build.json").read_text(encoding="utf-8"))
    assert data["status"] == "failed" and data["reason"]


def test_publish_blocked_and_error(on_cfg, digest, result):
    class PublishBlocked(Exception):
        pass

    rec = go(on_cfg, digest, result, publisher=FakePublisher(exc=PublishBlocked("leak found")))
    assert (rec.status, rec.reason) == ("blocked", "publish: leak found")
    rec = go(on_cfg, digest, result, publisher=FakePublisher(exc=RuntimeError(KEY)))
    assert rec.status == "failed" and KEY not in rec.reason


def test_corrupt_history_skips(on_cfg, digest, result):
    on_cfg.builds_dir.mkdir(parents=True)
    (on_cfg.builds_dir / "history.json").write_text("{nope", encoding="utf-8")
    rec = go(on_cfg, digest, result)
    assert (rec.status, rec.reason) == ("skipped", "build history file is corrupt")


def test_api_key_only_in_env_file(on_cfg, digest, result):
    seen = {}
    go(on_cfg, digest, result, docker=fake_docker(seen=seen))
    assert seen["key"] == KEY


def test_default_publisher_is_lazy(on_cfg, digest, result):
    # publish.py arrives in a later task; with no module the failure is recorded, not raised
    rec = run_build(
        on_cfg, digest, result, api_key=KEY, docker=fake_docker(), today=DAY, log=lambda *_: None
    )
    assert isinstance(rec, BuildRecord)


def test_render_prompt_braces_cannot_inject():
    out = render_prompt(
        "A {{ITEM_JSON}} B {{FIELD_PROFILE}} {x}",
        "2026-11-21",
        {"title": "{{FIELD_PROFILE}} {day} {0}"},
        "prof",
    )
    assert out.count("prof") == 1 and "{{FIELD_PROFILE}} {day} {0}" in out
    assert "{x}" in out


def test_template_requirements():
    from radar.config import REPO_ROOT

    text = (REPO_ROOT / "prompts" / "build_task.md").read_text(encoding="utf-8")
    for needle in ("pytest", "test_demo.py", "CPU only", "output/", "BUILD_LOG.md", "real output"):
        assert needle in text
    assert run_mod.TEMPLATE_RE.findall(text) == ["DAY", "FIELD_PROFILE", "ITEM_JSON"]
