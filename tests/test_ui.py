import shutil

import pytest
from fastapi.testclient import TestClient

from radar import settings
from radar.ui.app import create_app
from radar.ui.jobs import JobRunner

TOKEN = "t0ken"
H = {"X-Radar-Token": TOKEN, "Origin": "http://testserver"}


@pytest.fixture
def root(tmp_path):
    """A throwaway repo root: config, field profile, prompts, static files come from the real repo."""
    from radar.config import REPO_ROOT

    (tmp_path / "config").mkdir()
    shutil.copy(
        REPO_ROOT / "config" / "config.example.toml", tmp_path / "config" / "config.example.toml"
    )
    shutil.copy(REPO_ROOT / "config" / "field_profile.md", tmp_path / "config" / "field_profile.md")
    return tmp_path


class FakeRunner(JobRunner):
    def __init__(self):
        super().__init__(root=None, data_dir=None)
        self.started = []

    def start(self, action, extra=None):
        from radar.ui.jobs import Job, JobBusy

        if self.current and self.current.running:
            raise JobBusy(f"{self.current.action} is still running")
        self.started.append((action, extra))
        self.current = Job(action, ["radar", action])
        return self.current


@pytest.fixture
def client(root):
    from radar.config import load_config

    cfg = load_config(root=root)
    cfg.data_dir = root / "data"
    runner = FakeRunner()
    app = create_app(cfg, token=TOKEN, runner=runner)
    c = TestClient(app)
    c.runner = runner
    c.cfg = cfg
    return c


def test_index_injects_token(client):
    r = client.get("/")
    assert r.status_code == 200 and TOKEN in r.text and "__RADAR_TOKEN__" not in r.text


def test_posts_need_token_and_origin(client):
    assert client.post("/api/jobs", json={"action": "scan"}).status_code == 403
    assert (
        client.post(
            "/api/jobs",
            json={"action": "scan"},
            headers={"X-Radar-Token": TOKEN, "Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert client.post("/api/jobs", json={"action": "scan"}, headers=H).status_code == 200


def test_foreign_host_header_rejected(client):
    r = client.get("/api/status", headers={"Host": "attacker.example:8765"})
    assert r.status_code == 403


def test_one_job_at_a_time(client):
    assert (
        client.post("/api/jobs", json={"action": "scan", "max_posts": 15}, headers=H).status_code
        == 200
    )
    assert client.runner.started == [("scan", ["--max-posts", "15"])]
    r = client.post("/api/jobs", json={"action": "digest"}, headers=H)
    assert r.status_code == 409


def test_unknown_action_and_bad_folder(client):
    assert client.post("/api/jobs", json={"action": "rm -rf"}, headers=H).status_code == 400
    r = client.post("/api/jobs", json={"action": "podcast", "folder": "../../etc"}, headers=H)
    assert r.status_code == 400


def test_status_and_empty_lists(client):
    s = client.get("/api/status").json()
    assert s["posts"] == {} and s["queue"] == 0 and s["last_run"] is None
    assert client.get("/api/digests").json() == []
    assert client.get("/api/queue").json() == []


def test_settings_roundtrip_keeps_comments(client, root):
    r = client.post(
        "/api/settings", json={"scan.max_posts": 40, "notebooklm.length": "longer"}, headers=H
    )
    assert r.status_code == 200
    assert r.json()["values"]["scan.max_posts"] == 40
    text = (root / "config" / "config.toml").read_text(encoding="utf-8")
    assert "max_posts = 40" in text and "# Human-paced" in text


def test_invalid_settings_rejected_file_untouched(client, root):
    client.get("/api/settings")  # creates config.toml from the example
    before = (root / "config" / "config.toml").read_text(encoding="utf-8")
    r = client.post(
        "/api/settings",
        json={
            "scan.max_posts": -5,
            "digest.min_items": 20,
            "digest.max_items": 10,
            "email.to": "nope",
        },
        headers=H,
    )
    assert r.status_code == 400
    errors = r.json()["errors"]
    assert set(errors) >= {"scan.max_posts", "digest.max_items", "email.to"}
    assert (root / "config" / "config.toml").read_text(encoding="utf-8") == before


def test_profile_save(client, root):
    r = client.post("/api/profile", json={"text": "# My field\n\n- batteries\n"}, headers=H)
    assert r.status_code == 200
    assert client.get("/api/profile").json()["text"].startswith("# My field")
    assert client.post("/api/profile", json={"text": "  "}, headers=H).status_code == 400


def test_queue_and_digest_views(client, root):
    from radar.classify import Classifier
    from radar.db import Store
    from radar.scanner import scan_file
    from tests.conftest import FIXTURES, FakeModel

    store = Store(client.cfg.db_path)
    scan_file(
        FIXTURES / "feed_sample.json", store, Classifier(FakeModel(), "p"), log=lambda s: None
    )
    store.close()
    rows = client.get("/api/queue").json()
    assert [r["name"] for r in rows] == ["Dana Example", "Lee Placeholder"]
    r = client.post(
        "/api/queue/mark", json={"profile_url": rows[0]["profile_url"], "status": "sent"}, headers=H
    )
    assert r.json() == {"updated": 1}
    assert [r["name"] for r in client.get("/api/queue").json()] == ["Lee Placeholder"]

    d = client.cfg.digests_dir / "2026-11-19"
    d.mkdir(parents=True)
    (d / "summary.md").write_text("# Digest\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8")
    (d / "episode.m4a").write_bytes(b"audio")
    listing = client.get("/api/digests").json()
    assert listing[0]["name"] == "2026-11-19" and listing[0]["audio"] == "episode.m4a"
    assert "<table>" in client.get("/api/digests/2026-11-19").json()["html"]
    assert client.get("/api/digests/2026-11-19/audio").content == b"audio"
    assert client.get("/api/digests/..%2F..%2Fconfig/audio").status_code in (400, 404)


def test_schema_covers_every_config_section():
    sections = {f.section for f in settings.SCHEMA}
    assert sections == {
        "profile",
        "scan",
        "browser",
        "llm",
        "digest",
        "notebooklm",
        "email",
        "builds",
    }


def test_digest_html_is_sanitized():
    from radar.ui.app import safe_markdown

    md = (
        "# T\n\n<script>alert(1)</script> <img src=x onerror=alert(2)>\n\n"
        "<https://arxiv.org/abs/2611.1>\n\n[click](javascript:alert(3)) **bold** & co\n"
    )
    html = safe_markdown(md)
    assert "<script" not in html and "<img" not in html
    assert (
        '<a rel="noopener noreferrer" target="_blank" href="https://arxiv.org/abs/2611.1">' in html
    )
    assert "javascript:" not in html and "<strong>bold</strong>" in html


def test_nan_and_infinity_rejected(client):
    client.get("/api/settings")
    r = client.post(
        "/api/settings", json={"scan.max_minutes": "nan", "llm.timeout_s": "inf"}, headers=H
    )
    assert r.status_code == 400
    assert set(r.json()["errors"]) == {"scan.max_minutes", "llm.timeout_s"}


def test_jobs_pass_custom_config(tmp_path):
    from radar.ui.jobs import JobRunner

    r = JobRunner(tmp_path, tmp_path / "data", python="py", config=tmp_path / "alt.toml")
    started = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            started["argv"] = argv
            self.stdout = iter(())
            self.pid = 1

        def wait(self):
            return 0

    import subprocess

    orig = subprocess.Popen
    subprocess.Popen = FakePopen
    try:
        r.start("scan", ["--max-posts", "5"])
    finally:
        subprocess.Popen = orig
    argv = started["argv"]
    assert argv[argv.index("--config") + 1] == str(tmp_path / "alt.toml")
    assert argv[-3:] == ["scan", "--max-posts", "5"]


# ---- daily builds -----------------------------------------------------------------------------
def _make_build(
    client, day="2026-11-19", status="published", folder="2026-11-19_pkg_task", **extra
):
    import json

    builds = client.cfg.builds_dir
    (builds / "records").mkdir(parents=True, exist_ok=True)
    rec = {"day": day, "status": status, "package": "pkg", "folder": folder, "url": "", **extra}
    if status not in ("published", "building"):
        rec["reason"] = "scan found a secret"
    (builds / "records" / f"{day}.json").write_text(json.dumps(rec), encoding="utf-8")
    return builds


def test_builds_list_newest_first_with_running_job(client):
    _make_build(client, day="2026-11-18", folder="2026-11-18_a_b")
    _make_build(client, day="2026-11-19")
    (client.cfg.builds_dir / "records" / "bad.json").write_text("{", encoding="utf-8")
    d = client.get("/api/builds").json()
    assert [b["day"] for b in d["builds"]] == ["2026-11-19", "2026-11-18"] and d["job"] is None
    client.post("/api/jobs", json={"action": "build"}, headers=H)
    assert client.get("/api/builds").json()["job"]["action"] == "build"


def test_builds_key_goes_to_the_repo_secret_and_is_never_echoed(client, monkeypatch):
    from radar.builds import repo_init

    saved = []
    monkeypatch.setattr(repo_init, "set_secret", lambda cfg, key: saved.append(key))
    key = "sk-ant-" + "a" * 30
    r = client.post("/api/builds/key", json={"key": f" {key}" + chr(10)}, headers=H)
    assert r.status_code == 200 and r.json() == {"saved": True} and key not in r.text
    assert saved == [key]
    assert client.post("/api/builds/key", json={"key": "tooshort"}, headers=H).status_code == 400
    assert len(saved) == 1  # the rejected key was not stored


def test_builds_key_failure_is_a_fixed_phrase(client, monkeypatch):
    from radar.builds import repo_init
    from radar.builds.actions import ActionsError

    def boom(cfg, key):
        raise ActionsError("could not store the secret")

    monkeypatch.setattr(repo_init, "set_secret", boom)
    key = "sk-ant-" + "a" * 30
    r = client.post("/api/builds/key", json={"key": key}, headers=H)
    assert r.status_code == 400 and "could not store the secret" in r.text and key not in r.text


def test_builds_posts_need_token(client):
    assert client.post("/api/builds/key", json={"key": "x" * 30}).status_code == 403
    assert client.post("/api/schedule", json={"task": "ResearchRadarBuild"}).status_code == 403


def test_schedule_task_validated(client, monkeypatch):
    from radar.ui import schedule

    seen = []
    monkeypatch.setattr(
        schedule,
        "set_schedule",
        lambda root, time, enabled, task=schedule.TASK: seen.append(task) or {},
    )
    assert client.get("/api/schedule?task=Evil").status_code == 400
    body = {"time": "08:00", "task": "ResearchRadarBuild"}
    assert client.post("/api/schedule", json=body, headers=H).status_code == 200
    assert client.post("/api/schedule", json={"time": "08:00"}, headers=H).status_code == 200
    assert seen == ["ResearchRadarBuild", "ResearchRadarDaily"]
    bad = {"time": "08:00", "task": "Evil"}
    monkeypatch.undo()
    assert client.post("/api/schedule", json=bad, headers=H).status_code == 400


def test_page_has_builds_tab_and_key_field(client):
    html = client.get("/").text
    for needle in (
        'data-tab="builds"',
        'id="b-key"',
        "ResearchRadarBuild",
        "builds-key",
        "build-init-repo",
        "build-sync",
    ):
        assert needle in html


def test_cancel_kills_the_job_without_docker(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from radar.ui import jobs
    from radar.ui.jobs import Job

    killed = []
    monkeypatch.setattr(jobs.subprocess, "run", lambda argv, **kw: killed.append(argv))
    r = JobRunner(tmp_path, tmp_path / "data", python="py")
    r.current = Job("build", ["radar", "build"])
    r.current.proc = SimpleNamespace(pid=4242, terminate=lambda: killed.append("term"))
    assert r.cancel() is True
    assert killed


def test_build_detail_links_to_github_only(client):
    _make_build(client, url="https://github.com/own/builds/tree/main/2026-11-19_pkg_task")
    d = client.get("/api/builds/2026-11-19").json()
    assert d["record"]["package"] == "pkg" and d["record"]["run_url"] == ""
    assert d["record"]["url"].startswith("https://github.com/own/builds/tree/main/")
    assert "readme_html" not in d and "images" not in d and "has_folder" not in d
    _make_build(client, url="https://evil.example/x")
    assert client.get("/api/builds/2026-11-19").json()["record"]["url"] == ""
    _make_build(client, url="javascript:alert(1)")
    assert client.get("/api/builds/2026-11-19").json()["record"]["url"] == ""


def _set_repo(client, repo):
    cfg_file = client.cfg.root / "config" / "config.toml"
    cfg_file.write_text("[builds]\n" + f'repo = "{repo}"\n', encoding="utf-8")


def test_building_record_gets_the_run_link(client):
    _set_repo(client, "own/builds")
    _make_build(client, status="building", run_id=4242)
    rec = client.get("/api/builds").json()["builds"][0]
    assert rec["run_url"] == "https://github.com/own/builds/actions/runs/4242"
    assert client.get("/api/builds/2026-11-19").json()["record"]["run_url"] == rec["run_url"]


def test_run_link_needs_an_integer_run_id_and_building_status(client):
    _set_repo(client, "own/builds")
    for extra in ({"run_id": "4242/../x"}, {"run_id": True}, {"run_id": None}, {}):
        _make_build(client, status="building", **extra)
        assert client.get("/api/builds").json()["builds"][0]["run_url"] == ""
    _make_build(client, status="published", run_id=4242)
    assert client.get("/api/builds").json()["builds"][0]["run_url"] == ""
    _set_repo(client, "bad repo")
    _make_build(client, status="building", run_id=4242)
    assert client.get("/api/builds").json()["builds"][0]["run_url"] == ""


def test_local_work_folder_endpoints_are_gone(client):
    _make_build(client)
    assert client.get("/api/builds/2026-11-19/file/plot.png").status_code == 404


def test_page_has_no_dead_build_image_action_and_shows_run_link(client):
    html = client.get("/").text
    assert "build-image" not in html and "work folder" not in html
    for needle in ("Set up repo", ">Sync<", "run_url", "build-init-repo", "build-sync"):
        assert needle in html
