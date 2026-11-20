import contextlib
import datetime as dt
import json
import os
import struct
import sys
import zlib
from types import SimpleNamespace

import pytest

from radar.builds import run as run_mod
from radar.builds.run import render_prompt, run_build

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


def fake_docker(
    *,
    ready=(True, ""),
    on_build=good_build,
    on_verify=None,
    verify=(True, True),
    seen=None,
    codes=(0, 0),
    alive=False,
):
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
            if on_verify:
                on_verify(work)
            (work / ".verify.json").write_text(
                json.dumps({"installed": verify[0], "tests_passed": verify[1]}), encoding="utf-8"
            )
        return codes[0 if argv[0] == "build" else 1], ""

    return SimpleNamespace(
        docker_ready=lambda: ready,
        ensure_network_and_proxy=lambda cfg: None,
        ensure_image=lambda cfg: None,
        api_key_env_file=env_file,
        build_argv=lambda cfg, work, name, env_file=None: ["build", name, str(work)],
        verify_argv=lambda cfg, work, name: ["verify", name, str(work)],
        run_container=run_container,
        remove=lambda name: None,
        is_running=lambda name: alive,
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


def git(*args, cwd):
    import subprocess

    return subprocess.run(
        ["git", "-c", "safe.directory=*", "-c", "core.autocrlf=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


PROFILE = "# Field profile\nCOMMITTED-PROFILE-LINE\n"


@pytest.fixture
def git_root(tmp_path):
    """A throwaway repo root with a committed field profile and the real build prompt."""
    import shutil

    from radar.config import REPO_ROOT

    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "prompts").mkdir()
    shutil.copy(REPO_ROOT / "prompts" / "build_task.md", root / "prompts" / "build_task.md")
    (root / "config" / "field_profile.md").write_bytes(PROFILE.encode("utf-8"))
    git("init", "-q", "-b", "main", cwd=root)
    git("add", "config/field_profile.md", cwd=root)
    git("-c", "user.name=t", "-c", "user.email=t@x.y", "commit", "-q", "-m", "p", cwd=root)
    return root


@pytest.fixture
def on_cfg(cfg, git_root):
    cfg.builds.enabled = True
    cfg.builds.repo = "owner/daily"
    cfg.root = git_root
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
    kept = json.loads((on_cfg.builds_dir / "records" / "2026-11-21.json").read_text("utf-8"))
    assert kept["status"] == "published" and kept["url"]
    assert json.loads((second / "build.json").read_text("utf-8"))["status"] == "skipped"


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
    on_cfg.builds_dir.mkdir(parents=True, exist_ok=True)
    (on_cfg.builds_dir / "history.json").write_text("{nope", encoding="utf-8")
    rec = go(on_cfg, digest, result)
    assert (rec.status, rec.reason) == ("skipped", "build history file is corrupt")


def _tree_has(root, needle):
    return any(
        needle in p.read_bytes() for p in root.rglob("*") if p.is_file() and not p.is_symlink()
    )


def test_api_key_only_in_env_file(on_cfg, digest, result, tmp_path):
    seen = {}
    go(on_cfg, digest, result, docker=fake_docker(seen=seen))
    assert seen["key"] == KEY

    def noisy(work):
        good_build(work)
        (work / ".logs" / "claude.log").write_text(f"env dump {KEY} end", encoding="utf-8")

    def leak(work):
        noisy(work)
        (work / "out.txt").write_text(KEY, encoding="utf-8")

    def broken(work):
        (work / ".logs").mkdir()
        (work / ".logs" / "claude.log").write_text(KEY, encoding="utf-8")

    for i, fn in enumerate((noisy, leak, broken)):
        cfg_dir = on_cfg.builds_dir
        (cfg_dir / "history.json").unlink(missing_ok=True)
        d = tmp_path / f"d{i}"
        d.mkdir()
        go(on_cfg, d, result, docker=fake_docker(on_build=fn))
        assert not _tree_has(cfg_dir, KEY.encode()), fn.__name__
        assert not _tree_has(d, KEY.encode())


def test_default_publisher_is_lazy(on_cfg, digest, result, monkeypatch):
    calls = []

    def fake(cfg, work, record, markers, secrets):
        calls.append(record.status)
        return "https://example.test/x"

    fake_mod = SimpleNamespace(publish=fake, PublishBlocked=RuntimeError)
    monkeypatch.setitem(sys.modules, "radar.builds.publish", fake_mod)
    rec = run_build(
        on_cfg, digest, result, api_key=KEY, docker=fake_docker(), today=DAY, log=lambda *_: None
    )
    assert calls == ["published"]
    assert rec.url == "https://example.test/x"


def test_build_timeout_fails_without_publish(on_cfg, digest, result):
    pub = FakePublisher()
    rec = go(on_cfg, digest, result, docker=fake_docker(codes=(124, 0)), publisher=pub)
    assert (rec.status, rec.reason) == ("failed", "build timed out")
    assert pub.calls == []


def test_verify_timeout_is_not_verified(on_cfg, digest, result):
    rec = go(on_cfg, digest, result, docker=fake_docker(codes=(0, 124)))
    assert rec.status == "did_not_run"


def test_container_still_alive_fails(on_cfg, digest, result):
    pub = FakePublisher()
    rec = go(on_cfg, digest, result, docker=fake_docker(alive=True), publisher=pub)
    assert (rec.status, rec.reason) == ("failed", "build container did not stop")
    assert pub.calls == []


def test_field_profile_comes_from_committed_file(on_cfg, digest, result, tmp_path):
    other = tmp_path / "private_profile.md"
    other.write_text("PRIVATE-PROFILE-TEXT", encoding="utf-8")
    on_cfg.field_profile = other
    seen = {}
    go(on_cfg, digest, result, docker=fake_docker(seen=seen))
    assert "PRIVATE-PROFILE-TEXT" not in seen["blob"]
    committed = (on_cfg.root / "config" / "field_profile.md").read_text(encoding="utf-8")
    assert committed.strip().splitlines()[0] in seen["blob"]


def _symlink(src, dst):
    try:
        os.symlink(src, dst)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")


def test_link_made_by_verify_container_blocks(on_cfg, digest, result, tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")

    def swap(work):
        (work / "README.md").unlink()
        _symlink(target, work / "README.md")

    pub = FakePublisher()
    rec = go(on_cfg, digest, result, docker=fake_docker(on_verify=swap), publisher=pub)
    assert (rec.status, rec.reason) == ("blocked", "link or special file in the build folder")
    assert pub.calls == [] and target.read_text(encoding="utf-8") == "outside"


def test_readme_deleted_by_verify_container_fails(on_cfg, digest, result):
    pub = FakePublisher()
    rec = go(
        on_cfg,
        digest,
        result,
        docker=fake_docker(on_verify=lambda w: (w / "README.md").unlink()),
        publisher=pub,
    )
    assert rec.status == "failed" and pub.calls == []


def test_stale_folder_that_cannot_be_removed_fails(on_cfg, digest, result, monkeypatch):
    stale = on_cfg.builds_dir / "work" / "2026-11-21_mace_mace-lora-finetune"
    stale.mkdir(parents=True)
    (stale / "old.txt").write_text("old", encoding="utf-8")
    monkeypatch.setattr(run_mod.shutil, "rmtree", lambda *a, **k: None)
    pub = FakePublisher()
    rec = go(on_cfg, digest, result, publisher=pub)
    assert (rec.status, rec.reason) == ("failed", "could not prepare the build folder")
    assert pub.calls == []


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


LINK_REASON = "link or special file in the build folder"


def _outside(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    return outside


def _hard_link(src, dst):
    try:
        os.link(src, dst)
    except (OSError, NotImplementedError):
        pytest.skip("hard links not available")


def test_hard_link_from_build_container_blocks(on_cfg, digest, result, tmp_path):
    outside = _outside(tmp_path)

    def link(work):
        good_build(work)
        _hard_link(outside, work / "linked.txt")

    pub = FakePublisher()
    rec = go(on_cfg, digest, result, docker=fake_docker(on_build=link), publisher=pub)
    assert (rec.status, rec.reason) == ("blocked", LINK_REASON)
    assert pub.calls == [] and outside.read_text(encoding="utf-8") == "outside"


def test_hard_link_from_verify_container_blocks(on_cfg, digest, result, tmp_path):
    outside = _outside(tmp_path)
    pub = FakePublisher()
    rec = go(
        on_cfg,
        digest,
        result,
        docker=fake_docker(on_verify=lambda w: _hard_link(outside, w / "linked.txt")),
        publisher=pub,
    )
    assert (rec.status, rec.reason) == ("blocked", LINK_REASON)
    assert pub.calls == [] and outside.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows only")
def test_junction_from_build_container_blocks(on_cfg, digest, result, tmp_path):
    import _winapi

    target = tmp_path / "target_dir"
    target.mkdir()
    (target / "secret.txt").write_text("outside", encoding="utf-8")

    def junction(work):
        good_build(work)
        _winapi.CreateJunction(str(target), str(work / "jn"))

    pub = FakePublisher()
    rec = go(on_cfg, digest, result, docker=fake_docker(on_build=junction), publisher=pub)
    assert (rec.status, rec.reason) == ("blocked", LINK_REASON)
    assert pub.calls == [] and (target / "secret.txt").read_text(encoding="utf-8") == "outside"


def test_docker_timeout_checking_container_fails_cleanly(on_cfg, digest, result):
    import subprocess

    def hung(name):
        raise subprocess.TimeoutExpired(["docker"], 30)

    def noisy(work):
        good_build(work)
        (work / ".logs" / "claude.log").write_text(KEY, encoding="utf-8")

    docker = fake_docker(on_build=noisy)
    docker.is_running = hung
    pub = FakePublisher()
    rec = go(on_cfg, digest, result, docker=docker, publisher=pub)
    assert (rec.status, rec.reason) == ("failed", "build container did not stop")
    assert pub.calls == []
    assert not (on_cfg.builds_dir / "work" / rec.folder).exists()
    assert not _tree_has(on_cfg.builds_dir, KEY.encode())


def test_committed_profile_reaches_prompt_even_with_crlf_checkout(on_cfg, digest, result):
    profile = on_cfg.root / "config" / "field_profile.md"
    profile.write_bytes(PROFILE.replace("\n", "\r\n").encode("utf-8"))
    seen = {}
    rec = go(on_cfg, digest, result, docker=fake_docker(seen=seen))
    assert rec.status == "published"
    assert "COMMITTED-PROFILE-LINE" in seen["blob"]


def test_uncommitted_profile_skips_build(on_cfg, digest, result):
    profile = on_cfg.root / "config" / "field_profile.md"
    profile.write_text(PROFILE + "From the CV: PRIVATE-CV-TEXT\n", encoding="utf-8")
    seen = {}
    rec = go(on_cfg, digest, result, docker=fake_docker(seen=seen))
    assert (rec.status, rec.reason) == ("skipped", "field profile has uncommitted changes")
    assert "blob" not in seen


def test_profile_not_committed_skips_build(on_cfg, digest, result, tmp_path):
    bare = tmp_path / "not-a-repo"
    (bare / "config").mkdir(parents=True)
    (bare / "config" / "field_profile.md").write_text(PROFILE, encoding="utf-8")
    on_cfg.root = bare
    rec = go(on_cfg, digest, result)
    assert (rec.status, rec.reason) == ("skipped", "field profile not committed")
    on_cfg.root = tmp_path / "repo"
    git("rm", "-q", "--cached", "config/field_profile.md", cwd=on_cfg.root)
    git("-c", "user.name=t", "-c", "user.email=t@x.y", "commit", "-q", "-m", "x", cwd=on_cfg.root)
    rec = go(on_cfg, digest, result)
    assert (rec.status, rec.reason) == ("skipped", "field profile not committed")


def test_tool_leftovers_are_removed_before_the_scan(on_cfg, digest, result):
    def messy(work):
        good_build(work)
        for rel in (
            ".venv/lib/x.so",
            "venv/bin/python",
            "clone/.git/objects/ab",
            "node_modules/pkg/index.js",
            ".mypy_cache/x",
            ".ruff_cache/x",
            "nb/.ipynb_checkpoints/x",
            "sub/.pytest_cache/x",
            "sub/deep/__pycache__/x.pyc",
        ):
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_bytes(b"\x00\x01binary")

    pub = FakePublisher()
    rec = go(on_cfg, digest, result, docker=fake_docker(on_build=messy), publisher=pub)
    assert rec.status == "published"
    names = {p.name for p in pub.calls[0][0].rglob("*")}
    for gone in (".venv", "venv", ".git", "node_modules", ".mypy_cache", ".ruff_cache"):
        assert gone not in names
    assert not names & {".ipynb_checkpoints", ".pytest_cache", "__pycache__"}
    assert "clone" in names and "sub" in names


def test_prompt_sends_venvs_and_clones_to_tmp():
    from radar.config import REPO_ROOT

    text = (REPO_ROOT / "prompts" / "build_task.md").read_text(encoding="utf-8")
    assert "virtual environments" in text and "/tmp" in text


def test_os_error_after_container_exit_fails_and_deletes_folder(
    on_cfg, digest, result, monkeypatch
):
    def broken(*a, **k):
        raise OSError("disk gone")

    for target in ("_move_logs", "_clean_work", "_sanitize_images"):
        monkeypatch.undo()
        monkeypatch.setattr(run_mod, target, broken)
        (on_cfg.builds_dir / "history.json").unlink(missing_ok=True)
        pub = FakePublisher()
        rec = go(on_cfg, digest, result, publisher=pub)
        assert (rec.status, rec.reason) == ("failed", "could not check the build folder"), target
        assert pub.calls == [] and not (on_cfg.builds_dir / "work" / rec.folder).exists()
        assert json.loads((digest / "build.json").read_text("utf-8"))["status"] == "failed"


def test_os_error_writing_banner_fails(on_cfg, digest, result, monkeypatch):
    real = run_mod.Path.write_text

    def flaky(self, text, *a, **k):
        if self.name == "README.md" and text.startswith("> **DID NOT RUN.**"):
            raise OSError("read-only")
        return real(self, text, *a, **k)

    monkeypatch.setattr(run_mod.Path, "write_text", flaky)
    rec = go(on_cfg, digest, result, docker=fake_docker(verify=(False, False)))
    assert (rec.status, rec.reason) == ("failed", "could not check the build folder")


def test_missing_files_record_claude_exit_code(on_cfg, digest, result):
    rec = go(on_cfg, digest, result, docker=fake_docker(on_build=lambda w: None, codes=(3, 0)))
    assert (rec.status, rec.reason) == ("failed", "claude exited 3; no README.md or no Python file")
    rec = go(on_cfg, digest, result, docker=fake_docker(on_build=lambda w: None))
    assert (rec.status, rec.reason) == ("failed", "no README.md or no Python file")
