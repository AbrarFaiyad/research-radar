import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from radar import cli, settings
from radar.builds import keys
from radar.builds import run as run_mod
from radar.builds import sandbox as sandbox_mod
from radar.builds.run import BuildRecord
from radar.pipeline import RunLock, latest_run
from radar.ui import jobs, schedule

KEY = "sk-ant-api03-" + "q" * 40
RESULT = {"items": [{"title": "T", "url": "https://example.org/x"}]}


def make_digest(cfg, name="2026-11-19", with_result=True):
    folder = cfg.digests_dir / name
    folder.mkdir(parents=True)
    (folder / "summary.md").write_text("# digest\n", encoding="utf-8")
    if with_result:
        (folder / "codex_result.json").write_text(json.dumps(RESULT), encoding="utf-8")
    return folder


def daily_setup(cfg, monkeypatch, build_fn, *, mode="with_digest", audio_fn=None):
    """Fake every stage except the daily wiring; returns the list of stage calls."""
    cfg.builds.enabled = True
    cfg.builds.mode = mode
    cfg.email.enabled = True
    cfg.notebooklm.enabled = audio_fn is not None
    calls = []
    monkeypatch.setattr(keys, "get_key", lambda: KEY)
    monkeypatch.setattr(run_mod, "run_build", build_fn)
    monkeypatch.setattr(cli, "stage_scan", lambda cfg, *a, **k: calls.append("scan") or {})
    monkeypatch.setattr(cli, "stage_email", lambda cfg, *a, **k: calls.append("email") or {})

    def fake_run_digest(cfg_, store, make_audio=None, after_codex=None, **_):
        folder = make_digest(cfg_)
        if after_codex:
            after_codex(folder, RESULT)
        audio = make_audio(folder, RESULT) if make_audio else None
        calls.append("digest")
        return SimpleNamespace(
            kept=1, reviewed=1, summary_md=folder / "summary.md", audio=audio, folder=folder
        )

    import radar.digest

    monkeypatch.setattr(radar.digest, "run_digest", fake_run_digest)
    monkeypatch.setattr(cli, "_audio_maker", lambda cfg: audio_fn)
    return calls


def stage_names(cfg):
    return [s["name"] for s in latest_run(cfg)["stages"]]


def test_daily_runs_build_in_parallel_with_audio(cfg, monkeypatch):
    build_started = threading.Event()
    audio_saw_build = []

    def build(cfg_, folder, result, **kw):
        assert result == RESULT and kw["api_key"] == KEY
        build_started.set()
        return BuildRecord(day="2026-11-19", status="published", package="p", url="u")

    def audio(folder, result):
        audio_saw_build.append(build_started.wait(5))
        return folder / "audio.m4a"

    calls = daily_setup(cfg, monkeypatch, build, audio_fn=audio)
    assert cli.cmd_daily(cfg, SimpleNamespace()) == 0
    assert audio_saw_build == [True]
    assert calls == ["scan", "digest", "email"]
    assert stage_names(cfg) == ["scan", "digest", "build", "email"]
    stage = latest_run(cfg)["stages"][2]
    assert stage["ok"] and stage["optional"] and stage["detail"]["status"] == "published"


def test_build_failure_keeps_daily_ok(cfg, monkeypatch):
    def build(*a, **kw):
        raise RuntimeError("boom")

    daily_setup(cfg, monkeypatch, build)
    assert cli.cmd_daily(cfg, SimpleNamespace()) == 0
    run = latest_run(cfg)
    assert run["ok"]
    stage = run["stages"][2]
    assert stage["name"] == "build" and not stage["ok"] and stage["optional"]
    assert "build crashed" in stage["error"]
    assert run["stages"][3]["name"] == "email" and run["stages"][3]["ok"]


def test_build_gets_a_copy_of_the_result(cfg, monkeypatch):
    seen = {}

    def build(cfg_, folder, result, **kw):
        result["items"].append("mutated")
        seen["done"] = True
        return BuildRecord(day="d", status="skipped", reason="no API key")

    daily_setup(cfg, monkeypatch, build)
    cli.cmd_daily(cfg, SimpleNamespace())
    assert seen["done"] and RESULT["items"] == [{"title": "T", "url": "https://example.org/x"}]
    stage = latest_run(cfg)["stages"][2]
    assert stage["ok"] and stage["skipped"] and stage["error"] == "no API key"


def test_slow_build_inside_budget_keeps_its_status(cfg, monkeypatch):
    def build(*a, **kw):
        threading.Event().wait(0.3)
        return BuildRecord(day="d", status="published", package="p")

    daily_setup(cfg, monkeypatch, build)
    monkeypatch.setattr(cli, "BUILD_SETUP_MIN", -cfg.builds.timeout_min - 15 + 0.01)  # 0.6 s
    cli.cmd_daily(cfg, SimpleNamespace())
    stage = latest_run(cfg)["stages"][2]
    assert stage["ok"] and stage["detail"]["status"] == "published"


def test_build_that_never_finishes_is_removed_and_recorded(cfg, monkeypatch):
    release = threading.Event()
    removed = []

    def build(cfg_, folder, result, **kw):
        kw["containers"].extend(["radar-build-x", "radar-build-x-verify"])
        release.wait(10)
        return BuildRecord(day="d", status="published")

    daily_setup(cfg, monkeypatch, build)
    monkeypatch.setattr(cli, "BUILD_SETUP_MIN", -cfg.builds.timeout_min - 15 + 0.001)
    monkeypatch.setattr(cli, "BUILD_KILL_GRACE_MIN", 0.002)
    monkeypatch.setattr(sandbox_mod, "remove", removed.append)
    try:
        cli.cmd_daily(cfg, SimpleNamespace())
    finally:
        release.set()
    stage = latest_run(cfg)["stages"][2]
    assert not stage["ok"] and "build did not finish" in stage["error"]
    assert removed == ["radar-build-x", "radar-build-x-verify"]


def test_separate_mode_skips_build_in_daily(cfg, monkeypatch):
    def build(*a, **kw):
        raise AssertionError("must not run")

    daily_setup(cfg, monkeypatch, build, mode="separate")
    assert cli.cmd_daily(cfg, SimpleNamespace()) == 0
    assert stage_names(cfg) == ["scan", "digest", "email"]


def test_builds_off_adds_no_stage(cfg, monkeypatch):
    daily_setup(cfg, monkeypatch, lambda *a, **k: None)
    cfg.builds.enabled = False
    cli.cmd_daily(cfg, SimpleNamespace())
    assert stage_names(cfg) == ["scan", "digest", "email"]


def test_daily_without_new_digest_skips_build_stage(cfg, monkeypatch):
    daily_setup(cfg, monkeypatch, lambda *a, **k: None)
    import radar.digest

    monkeypatch.setattr(radar.digest, "run_digest", lambda *a, **k: None)
    cli.cmd_daily(cfg, SimpleNamespace())
    stage = latest_run(cfg)["stages"][2]
    assert stage["name"] == "build" and stage["skipped"]


def build_args(folder=None, retry=False, prepare=False):
    return SimpleNamespace(folder=folder, retry=retry, prepare=prepare)


def fake_build(record, seen):
    def build(cfg, folder, result, **kw):
        seen["folder"], seen["result"], seen["key"] = folder, result, kw["api_key"]
        seen["locked"] = RunLock(cfg).holder()
        return record

    return build


def test_build_command_uses_latest_digest(cfg, monkeypatch, capsys):
    make_digest(cfg, "2026-11-18")
    latest = make_digest(cfg, "2026-11-19")
    seen = {}
    rec = BuildRecord(day="d", status="published", package="pkg", url="https://x/y")
    monkeypatch.setattr(keys, "get_key", lambda: KEY)
    monkeypatch.setattr(run_mod, "run_build", fake_build(rec, seen))
    assert cli.cmd_build(cfg, build_args()) == 0
    assert seen["folder"] == latest and seen["result"] == RESULT and seen["key"] == KEY
    assert seen["locked"]["owner"] == "build"
    assert RunLock(cfg).holder() is None
    out = capsys.readouterr().out
    assert "published" in out and "pkg" in out and "https://x/y" in out


@pytest.mark.parametrize(
    ("status", "code"),
    [("published", 0), ("did_not_run", 0), ("skipped", 0), ("failed", 1), ("blocked", 1)],
)
def test_build_exit_codes(cfg, monkeypatch, capsys, status, code):
    make_digest(cfg)
    rec = BuildRecord(day="d", status=status, package="pkg", reason="why")
    monkeypatch.setattr(keys, "get_key", lambda: KEY)
    monkeypatch.setattr(run_mod, "run_build", fake_build(rec, {}))
    assert cli.cmd_build(cfg, build_args()) == code
    out = capsys.readouterr().out
    assert status in out and (status == "published" or "why" in out)


def test_build_named_folder_and_missing_digest(cfg, monkeypatch):
    monkeypatch.setattr(keys, "get_key", lambda: KEY)
    monkeypatch.setattr(run_mod, "run_build", fake_build(BuildRecord("d", "skipped"), {}))
    with pytest.raises(cli.UserError):
        cli.cmd_build(cfg, build_args())
    make_digest(cfg, "2026-11-19", with_result=False)
    with pytest.raises(cli.UserError):
        cli.cmd_build(cfg, build_args("2026-11-19"))


def test_build_needs_retry_when_day_already_has_a_record(cfg, monkeypatch, capsys):
    folder = make_digest(cfg)
    (folder / "build.json").write_text(json.dumps({"status": "failed"}), encoding="utf-8")
    seen = {}
    monkeypatch.setattr(keys, "get_key", lambda: KEY)
    monkeypatch.setattr(run_mod, "run_build", fake_build(BuildRecord("d", "published"), seen))
    assert cli.cmd_build(cfg, build_args()) == 0
    assert "folder" not in seen and "--retry" in capsys.readouterr().out
    assert cli.cmd_build(cfg, build_args(retry=True)) == 0
    assert seen["folder"] == folder


def test_build_prepare_builds_network_and_image(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox_mod, "docker_ready", lambda: (True, ""))
    monkeypatch.setattr(sandbox_mod, "ensure_network_and_proxy", lambda c: calls.append("net"))
    monkeypatch.setattr(sandbox_mod, "ensure_image", lambda c: calls.append("image"))
    assert cli.cmd_build(cfg, build_args(prepare=True)) == 0
    assert calls == ["net", "image"]
    monkeypatch.setattr(sandbox_mod, "docker_ready", lambda: (False, "Docker is not running"))
    with pytest.raises(cli.UserError, match="Docker is not running"):
        cli.cmd_build(cfg, build_args(prepare=True))


def test_builds_key_stores_stripped_key(cfg, monkeypatch):
    stored = []
    monkeypatch.setattr(keys, "set_key", stored.append)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": f"  {KEY}\n ")
    assert cli.cmd_builds_key(cfg, SimpleNamespace()) == 0
    assert stored == [KEY]
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "   ")
    with pytest.raises(cli.UserError):
        cli.cmd_builds_key(cfg, SimpleNamespace())
    assert stored == [KEY]


def test_keys_use_the_credential_store(monkeypatch):
    import keyring

    store = {}
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: store.update({(s, u): p}))
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))
    assert keys.get_key() is None
    keys.set_key(KEY)
    assert store == {("research-radar-anthropic", "default"): KEY}
    assert keys.get_key() == KEY


def test_status_shows_skipped_and_optional_failures(cfg, capsys):
    from radar.pipeline import RunRecord, StageResult, save_run

    rec = RunRecord(
        started="2026-11-19T07:00:00",
        stages=[
            StageResult("scan", True, 1.0),
            StageResult("build", True, 1.0, error="no API key", optional=True, skipped=True),
            StageResult("podcast", False, 1.0, error="x", optional=True),
            StageResult("email", False, 1.0, error="x"),
        ],
    )
    save_run(cfg, rec)
    cli.cmd_status(cfg, SimpleNamespace())
    out = capsys.readouterr().out
    assert "scan ok" in out
    assert "build skipped: no API key" in out
    assert "podcast failed (optional)" in out
    assert "email FAILED" in out


def test_job_actions():
    assert jobs.ACTIONS["build"] == ["build"]
    assert jobs.ACTIONS["build-retry"] == ["build", "--retry"]
    assert jobs.ACTIONS["build-image"] == ["build", "--prepare"]


def test_separate_time_format(tmp_path):
    f = settings.BY_ID["builds.separate_time"]
    assert settings.coerce(f, "08:30") == "08:30"
    for bad in ("25:61", "8:30", "24:00", "08:60"):
        with pytest.raises(ValueError, match="24-hour time like 08:30"):
            settings.coerce(f, bad)
    with pytest.raises(ValueError, match="owner/name"):
        settings.coerce(settings.BY_ID["builds.repo"], "not a repo")


def test_set_schedule_task_parameter(monkeypatch, tmp_path):
    ran = []

    def fake_run(argv, **kw):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(schedule.os, "name", "nt")
    monkeypatch.setattr(schedule.subprocess, "run", fake_run)
    monkeypatch.setattr(schedule, "get", lambda task=schedule.TASK: {"task": task})
    out = schedule.set_schedule(Path(tmp_path), "08:30", True, task="ResearchRadarBuild")
    assert out == {"task": "ResearchRadarBuild"}
    install = ran[0]
    assert install[install.index("-TaskName") + 1] == "ResearchRadarBuild"
    assert install[install.index("-Command") + 1] == "build"
    assert "ResearchRadarBuild" in ran[1][-1]
    ran.clear()
    schedule.set_schedule(Path(tmp_path), "07:00")
    assert ran[0][ran[0].index("-TaskName") + 1] == "ResearchRadarDaily"
    with pytest.raises(ValueError, match="unknown task"):
        schedule.set_schedule(Path(tmp_path), "07:00", task="Other")
