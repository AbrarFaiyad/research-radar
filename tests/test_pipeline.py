import json
import os
import time

import pytest

from radar.pipeline import LockHeld, RunLock, StageSkipped, latest_run, run_stages, save_run


def test_failed_stage_does_not_stop_later_stages(cfg):
    calls = []

    def scan():
        calls.append("scan")
        raise RuntimeError("LinkedIn feed not visible")

    def digest():
        calls.append("digest")
        return {"kept": 6}

    def email():
        calls.append("email")
        raise SystemExit("No SMTP password stored")

    rec = run_stages({"scan": scan, "digest": digest, "email": email}, log=lambda s: None)
    assert calls == ["scan", "digest", "email"]
    assert rec.ok is False
    assert [s.ok for s in rec.stages] == [False, True, False]
    assert rec.stages[0].error == "LinkedIn feed not visible"
    assert rec.stages[1].detail == {"kept": 6}
    path = save_run(cfg, rec)
    assert json.loads(path.read_text(encoding="utf-8"))["ok"] is False
    assert latest_run(cfg)["stages"][2]["error"] == "No SMTP password stored"


def test_lock_refuses_second_holder_and_releases(cfg):
    lock = RunLock(cfg, owner="panel")
    with lock:
        other = {"pid": os.getppid(), "owner": "scheduler", "since": time.time()}
        lock.path.write_text(json.dumps(other), encoding="utf-8")
        with pytest.raises(LockHeld, match="scheduler"):
            RunLock(cfg).__enter__()
        lock.path.write_text(
            json.dumps({"pid": os.getpid(), "since": time.time()}), encoding="utf-8"
        )
    assert not lock.path.exists()


def test_stale_lock_is_taken_over(cfg):
    lock = RunLock(cfg)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(
        json.dumps({"pid": 999999, "owner": "old", "since": time.time() - 4 * 3600}),
        encoding="utf-8",
    )
    with lock:
        assert json.loads(lock.path.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_lock_of_dead_process_is_stale(cfg):
    import subprocess
    import sys

    from radar.pipeline import pid_alive

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert pid_alive(os.getpid()) and not pid_alive(proc.pid)
    lock = RunLock(cfg)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(
        json.dumps({"pid": proc.pid, "owner": "cancelled", "since": time.time()}), encoding="utf-8"
    )
    assert lock.holder() is None
    with lock:
        pass


def test_lock_held_by_another_live_process(cfg, tmp_path):
    import subprocess
    import sys
    import textwrap

    ready = tmp_path / "ready"
    child = textwrap.dedent(f"""
        import time, pathlib
        from radar.config import load_config
        from radar.pipeline import RunLock
        cfg = load_config()
        cfg.data_dir = pathlib.Path(r"{cfg.data_dir}")
        with RunLock(cfg, owner="scheduler"):
            pathlib.Path(r"{ready}").write_text("x")
            time.sleep(20)
    """)
    proc = subprocess.Popen([sys.executable, "-c", child])
    try:
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists(), "child never took the lock"
        with pytest.raises(LockHeld, match="scheduler"):
            RunLock(cfg).__enter__()
    finally:
        proc.kill()
        proc.wait()
    # The killed child's lock is now stale and can be taken over.
    with RunLock(cfg):
        pass


def _boom():
    raise RuntimeError("boom")


def test_optional_stage_failure_keeps_run_ok():
    rec = run_stages({"a": lambda: {}, "b": _boom}, log=lambda _: None, optional={"b"})
    assert rec.ok and not rec.stages[1].ok and rec.stages[1].optional


def test_required_stage_failure_still_fails_run():
    rec = run_stages({"a": _boom, "b": lambda: {}}, log=lambda _: None, optional={"b"})
    assert not rec.ok and not rec.stages[0].optional


def test_stage_skipped_is_recorded():
    def skip():
        raise StageSkipped("docker not running")

    logs = []
    rec = run_stages({"build": skip}, log=logs.append, optional={"build"})
    assert rec.ok and rec.stages[0].skipped and rec.stages[0].error == "docker not running"
    assert rec.stages[0].ok
    assert any("skipped: docker not running" in line for line in logs)
    assert not any("FAILED" in line for line in logs)
