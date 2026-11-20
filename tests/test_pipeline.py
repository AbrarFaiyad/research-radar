import json
import os
import time

import pytest

from radar.pipeline import LockHeld, RunLock, latest_run, run_stages, save_run


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
        other = {"pid": os.getpid() + 1, "owner": "scheduler", "since": time.time()}
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
