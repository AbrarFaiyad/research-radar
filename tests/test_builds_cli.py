import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from radar import cli, settings
from radar.builds import run as run_mod
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
    monkeypatch.setattr(run_mod, "run_build", build_fn)
    monkeypatch.setattr(run_mod, "sync_builds", lambda cfg, **k: [])
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
        assert result == RESULT and "api_key" not in kw
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
    monkeypatch.setattr(cli, "BUILD_SETUP_MIN", -cfg.builds.poll_min + 0.01)  # 0.6 s
    cli.cmd_daily(cfg, SimpleNamespace())
    stage = latest_run(cfg)["stages"][2]
    assert stage["ok"] and stage["detail"]["status"] == "published"


def test_build_that_never_finishes_without_a_run_is_failed(cfg, monkeypatch):
    release = threading.Event()

    def build(cfg_, folder, result, **kw):
        release.wait(10)
        return BuildRecord(day="d", status="published")

    daily_setup(cfg, monkeypatch, build)
    monkeypatch.setattr(cli, "BUILD_SETUP_MIN", -cfg.builds.poll_min + 0.001)
    try:
        cli.cmd_daily(cfg, SimpleNamespace())
    finally:
        release.set()
    stage = latest_run(cfg)["stages"][2]
    assert not stage["ok"] and "build did not finish" in stage["error"]


def test_build_that_never_finishes_with_a_run_is_building(cfg, monkeypatch):
    release = threading.Event()

    def build(cfg_, folder, result, **kw):
        run_mod._write_record(
            BuildRecord(cli.dt.date.today().isoformat(), "building", "p", "f", run_id=5),
            folder,
            cfg_.builds_dir / "records",
        )
        release.wait(10)
        return BuildRecord(day="d", status="published")

    daily_setup(cfg, monkeypatch, build)
    monkeypatch.setattr(cli, "BUILD_SETUP_MIN", -cfg.builds.poll_min + 0.001)
    try:
        cli.cmd_daily(cfg, SimpleNamespace())
    finally:
        release.set()
    stage = latest_run(cfg)["stages"][2]
    assert (
        stage["ok"] and stage["detail"]["status"] == "building" and stage["detail"]["run_id"] == 5
    )


def test_building_record_keeps_daily_ok(cfg, monkeypatch):
    daily_setup(cfg, monkeypatch, lambda *a, **k: BuildRecord(day="d", status="building", run_id=9))
    assert cli.cmd_daily(cfg, SimpleNamespace()) == 0
    stage = latest_run(cfg)["stages"][2]
    assert stage["ok"] and not stage["skipped"] and stage["detail"]["status"] == "building"


def test_daily_syncs_before_the_new_build(cfg, monkeypatch):
    order = []

    def build(*a, **kw):
        order.append("build")
        return BuildRecord(day="d", status="published", package="p")

    daily_setup(cfg, monkeypatch, build)
    monkeypatch.setattr(run_mod, "sync_builds", lambda cfg_, **k: order.append("sync") or [])
    assert cli.cmd_daily(cfg, SimpleNamespace()) == 0
    assert order == ["sync", "build"]


def test_sync_crash_does_not_stop_the_build(cfg, monkeypatch):
    def broken(cfg_, **k):
        raise RuntimeError("gh exploded")

    daily_setup(cfg, monkeypatch, lambda *a, **k: BuildRecord(day="d", status="published"))
    monkeypatch.setattr(run_mod, "sync_builds", broken)
    assert cli.cmd_daily(cfg, SimpleNamespace()) == 0
    assert latest_run(cfg)["stages"][2]["detail"]["status"] == "published"


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


def build_args(folder=None, retry=False, sync=False, init_repo=False):
    return SimpleNamespace(folder=folder, retry=retry, sync=sync, init_repo=init_repo)


def fake_build(record, seen):
    def build(cfg, folder, result, **kw):
        seen["folder"], seen["result"], seen["kw"] = folder, result, kw
        seen["locked"] = RunLock(cfg).holder()
        return record

    return build


def test_build_command_uses_latest_digest(cfg, monkeypatch, capsys):
    make_digest(cfg, "2026-11-18")
    latest = make_digest(cfg, "2026-11-19")
    seen = {}
    rec = BuildRecord(day="d", status="published", package="pkg", url="https://x/y")
    monkeypatch.setattr(run_mod, "run_build", fake_build(rec, seen))
    assert cli.cmd_build(cfg, build_args()) == 0
    assert seen["folder"] == latest and seen["result"] == RESULT and seen["kw"] == {}
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
    monkeypatch.setattr(run_mod, "run_build", fake_build(rec, {}))
    assert cli.cmd_build(cfg, build_args()) == code
    out = capsys.readouterr().out
    assert status in out and (status == "published" or "why" in out)


def test_build_named_folder_and_missing_digest(cfg, monkeypatch):
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
    monkeypatch.setattr(run_mod, "run_build", fake_build(BuildRecord("d", "published"), seen))
    assert cli.cmd_build(cfg, build_args()) == 0
    assert "folder" not in seen and "--retry" in capsys.readouterr().out
    assert cli.cmd_build(cfg, build_args(retry=True)) == 0
    assert seen["folder"] == folder


def test_build_sync_finishes_records_and_holds_the_lock(cfg, monkeypatch, capsys):
    seen = {}

    def fake_sync(cfg_, **kw):
        seen["locked"] = RunLock(cfg_).holder()
        return [BuildRecord(day="d", status="published", package="pkg", url="https://x/y")]

    monkeypatch.setattr(run_mod, "sync_builds", fake_sync)
    assert cli.cmd_build(cfg, build_args(sync=True)) == 0
    assert seen["locked"]["owner"] == "build" and RunLock(cfg).holder() is None
    assert "published" in capsys.readouterr().out


def test_build_sync_exit_codes(cfg, monkeypatch, capsys):
    monkeypatch.setattr(run_mod, "sync_builds", lambda cfg_, **k: [])
    assert cli.cmd_build(cfg, build_args(sync=True)) == 0
    assert "nothing to sync" in capsys.readouterr().out
    bad = [BuildRecord(day="d", status="failed", package="p", reason="build job failed")]
    monkeypatch.setattr(run_mod, "sync_builds", lambda cfg_, **k: bad)
    assert cli.cmd_build(cfg, build_args(sync=True)) == 1
    assert "build job failed" in capsys.readouterr().out


def test_build_init_repo(cfg, monkeypatch, capsys):
    from radar.builds import repo_init

    seen = {}

    def fake_init(cfg_, **kw):
        seen["locked"] = RunLock(cfg_).holder()
        return "https://github.com/o/r"

    monkeypatch.setattr(repo_init, "init_repo", fake_init)
    assert cli.cmd_build(cfg, build_args(init_repo=True)) == 0
    assert seen["locked"]["owner"] == "build" and RunLock(cfg).holder() is None
    assert "https://github.com/o/r" in capsys.readouterr().out


def test_build_init_repo_error_exits_2(cfg, monkeypatch, capsys):
    from radar.builds import repo_init
    from radar.builds.actions import ActionsError

    def refuse(cfg_, **kw):
        raise ActionsError("gh needs the workflow scope: run gh auth refresh -s workflow")

    monkeypatch.setattr(repo_init, "init_repo", refuse)
    monkeypatch.setattr(cli, "load_config", lambda path=None, **k: cfg)
    code = cli.main(["build", "--init-repo"])
    assert code == 2
    assert "workflow scope" in capsys.readouterr().err
    assert RunLock(cfg).holder() is None


def test_parser_has_sync_and_init_repo_but_not_prepare():
    args = cli.build_parser().parse_args(["build", "--sync"])
    assert args.sync and not args.init_repo
    assert cli.build_parser().parse_args(["build", "--init-repo"]).init_repo
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["build", "--prepare"])


def test_builds_key_goes_to_the_repo_secret(cfg, monkeypatch):
    from radar.builds import repo_init

    stored = []
    monkeypatch.setattr(repo_init, "set_secret", lambda cfg_, key: stored.append(key))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": f"  {KEY}" + chr(10) + " ")
    assert cli.cmd_builds_key(cfg, SimpleNamespace()) == 0
    assert stored == [KEY]
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "   ")
    with pytest.raises(cli.UserError):
        cli.cmd_builds_key(cfg, SimpleNamespace())
    assert stored == [KEY]


def test_builds_key_error_is_a_user_error_without_the_key(cfg, monkeypatch):
    from radar.builds import repo_init
    from radar.builds.actions import ActionsError

    def refuse(cfg_, key):
        raise ActionsError("could not store the secret")

    monkeypatch.setattr(repo_init, "set_secret", refuse)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": KEY)
    with pytest.raises(cli.UserError, match="could not store the secret") as err:
        cli.cmd_builds_key(cfg, SimpleNamespace())
    assert KEY not in str(err.value)


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
    assert jobs.ACTIONS["build-sync"] == ["build", "--sync"]
    assert jobs.ACTIONS["build-init-repo"] == ["build", "--init-repo"]
    assert "build-image" not in jobs.ACTIONS


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


# ---- separate mode, lock waits, key store errors ----------------------------------------------
TODAY = __import__("datetime").date.today().isoformat()


class FakeTime:
    """Fake clock for the build-lock wait: sleep advances time and runs a hook."""

    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.sleeps = []
        self.on_sleep = on_sleep

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s
        if self.on_sleep:
            self.on_sleep(len(self.sleeps))


@pytest.fixture
def daily_lock(cfg):
    """The run lock held by a live 'daily' process."""
    import sys
    import time

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.data_dir / "run.lock"
    path.write_text(
        json.dumps({"pid": proc.pid, "owner": "daily", "since": time.time()}), encoding="utf-8"
    )
    yield path
    proc.kill()
    proc.wait()


def separate_cfg(cfg, monkeypatch, seen):
    cfg.builds.mode = "separate"
    rec = BuildRecord(day=TODAY, status="published", package="pkg", url="https://x/y")
    monkeypatch.setattr(run_mod, "run_build", fake_build(rec, seen))


def test_separate_mode_builds_only_todays_digest(cfg, monkeypatch, capsys):
    seen = {}
    separate_cfg(cfg, monkeypatch, seen)
    make_digest(cfg, "2026-01-01")
    assert cli.cmd_build(cfg, build_args()) == 0
    assert "folder" not in seen and "no digest today" in capsys.readouterr().out
    record = json.loads((cfg.builds_dir / "records" / f"{TODAY}.json").read_text("utf-8"))
    assert record["status"] == "skipped" and record["reason"] == "no digest today"
    today = make_digest(cfg, TODAY)
    assert cli.cmd_build(cfg, build_args()) == 0
    assert seen["folder"] == today


def test_separate_mode_waits_for_daily_then_builds(cfg, monkeypatch, daily_lock):
    seen = {}
    separate_cfg(cfg, monkeypatch, seen)
    make_digest(cfg, TODAY)
    clock = FakeTime(on_sleep=lambda n: n == 3 and daily_lock.unlink())
    monkeypatch.setattr(cli, "time", clock)
    assert cli.cmd_build(cfg, build_args()) == 0
    assert clock.sleeps == [60, 60, 60]
    assert seen["locked"]["owner"] == "build"


def test_separate_mode_gives_up_after_two_hours(cfg, monkeypatch, daily_lock, capsys):
    seen = {}
    separate_cfg(cfg, monkeypatch, seen)
    folder = make_digest(cfg, TODAY)
    clock = FakeTime()
    monkeypatch.setattr(cli, "time", clock)
    assert cli.cmd_build(cfg, build_args()) == 0
    assert "folder" not in seen and len(clock.sleeps) == 120 and clock.now == 7200
    assert "daily run still in progress" in capsys.readouterr().out
    for path in (folder / "build.json", cfg.builds_dir / "records" / f"{TODAY}.json"):
        data = json.loads(path.read_text("utf-8"))
        assert (data["status"], data["reason"]) == ("skipped", "daily run still in progress")
    assert daily_lock.exists()


def test_manual_build_does_not_wait_for_daily(cfg, monkeypatch, daily_lock):
    from radar.pipeline import LockHeld

    seen = {}
    separate_cfg(cfg, monkeypatch, seen)
    cfg.builds.mode = "with_digest"
    make_digest(cfg, TODAY)
    clock = FakeTime()
    monkeypatch.setattr(cli, "time", clock)
    with pytest.raises(LockHeld):
        cli.cmd_build(cfg, build_args())
    assert clock.sleeps == [] and "folder" not in seen


def test_separate_mode_does_not_wait_for_other_jobs(cfg, monkeypatch, daily_lock):
    from radar.pipeline import LockHeld

    info = json.loads(daily_lock.read_text("utf-8"))
    daily_lock.write_text(json.dumps({**info, "owner": "scan"}), encoding="utf-8")
    separate_cfg(cfg, monkeypatch, {})
    make_digest(cfg, TODAY)
    clock = FakeTime()
    monkeypatch.setattr(cli, "time", clock)
    with pytest.raises(LockHeld):
        cli.cmd_build(cfg, build_args())
    assert clock.sleeps == []


def test_build_command_syncs_inside_the_lock_before_building(cfg, monkeypatch):
    order = []
    make_digest(cfg)

    def sync(cfg_, **kw):
        order.append(("sync", RunLock(cfg_).holder()["owner"]))
        return []

    def build(cfg_, folder, result, **kw):
        order.append(("build", None))
        return BuildRecord(day="d", status="published", package="p")

    monkeypatch.setattr(run_mod, "sync_builds", sync)
    monkeypatch.setattr(run_mod, "run_build", build)
    assert cli.cmd_build(cfg, build_args()) == 0
    assert order == [("sync", "build"), ("build", None)]


def test_build_command_survives_a_sync_crash(cfg, monkeypatch):
    make_digest(cfg)

    def boom(cfg_, **kw):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(run_mod, "sync_builds", boom)
    monkeypatch.setattr(run_mod, "run_build", fake_build(BuildRecord("d", "published"), {}))
    assert cli.cmd_build(cfg, build_args()) == 0


def test_daily_without_new_digest_still_syncs(cfg, monkeypatch):
    synced = []
    daily_setup(cfg, monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "sync_builds", lambda cfg_, **k: synced.append(1) or [])
    import radar.digest

    monkeypatch.setattr(radar.digest, "run_digest", lambda *a, **k: None)
    cli.cmd_daily(cfg, SimpleNamespace())
    assert synced == [1]
    assert latest_run(cfg)["stages"][2]["skipped"]


def test_daily_in_separate_mode_syncs(cfg, monkeypatch):
    synced = []
    daily_setup(cfg, monkeypatch, lambda *a, **k: None, mode="separate")
    monkeypatch.setattr(run_mod, "sync_builds", lambda cfg_, **k: synced.append(1) or [])
    assert cli.cmd_daily(cfg, SimpleNamespace()) == 0
    assert synced == [1]
