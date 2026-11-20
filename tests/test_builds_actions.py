import base64
import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from radar.builds import actions
from radar.builds.actions import ActionsError

REPO = "owner/builds"
FOLDER = "2026-11-20_numpy_fft-demo"
CFG = SimpleNamespace(builds=SimpleNamespace(repo=REPO))
SINCE = dt.datetime(2026, 11, 20, 12, 0, 0, tzinfo=dt.UTC)


def done(out="", code=0):
    return subprocess.CompletedProcess([], code, stdout=out, stderr="SECRET-stderr")


class FakeGh:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, args, *, input=None, timeout=60):
        self.calls.append(list(args))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def runs(*items):
    return done(json.dumps(list(items)))


def run_item(folder=FOLDER, created="2026-11-20T12:00:05Z", rid=1):
    return {"databaseId": rid, "displayTitle": f"build {folder}", "createdAt": created}


# dispatch


def test_dispatch_uses_prompt_file_and_deletes_it():
    seen = {}

    def gh(args, *, input=None, timeout=60):
        arg = next(a for a in args if a.startswith("prompt=@"))
        path = Path(arg[len("prompt=@") :])
        seen["path"] = path
        seen["text"] = path.read_text(encoding="utf-8")
        seen["args"] = list(args)
        return done()

    actions.dispatch(CFG, FOLDER, "secret-ish prompt é", gh=gh)
    assert seen["text"] == "secret-ish prompt é"
    assert not seen["path"].exists()
    assert seen["path"].name.startswith("radar-prompt-")
    assert seen["args"][:6] == ["workflow", "run", "build.yml", "-R", REPO, "-f"]
    assert f"folder={FOLDER}" in seen["args"]
    assert "-F" in seen["args"]
    assert not any("secret-ish" in a for a in seen["args"])


def test_dispatch_failure_is_fixed_phrase_and_cleans_up():
    seen = {}

    def gh(args, *, input=None, timeout=60):
        seen["path"] = Path(next(a for a in args if a.startswith("prompt=@"))[8:])
        return done("x", code=1)

    with pytest.raises(ActionsError) as e:
        actions.dispatch(CFG, FOLDER, "p", gh=gh)
    assert str(e.value) == "could not start the workflow"
    assert not seen["path"].exists()


def test_dispatch_os_error_and_timeout():
    for exc in (OSError("boom SECRET"), subprocess.TimeoutExpired("gh", 1)):
        with pytest.raises(ActionsError) as e:
            actions.dispatch(CFG, FOLDER, "p", gh=FakeGh(exc))
        assert str(e.value) == "could not start the workflow"


# find_run


def test_find_run_picks_newest_matching():
    gh = FakeGh(
        runs(
            run_item("other-folder", rid=9),
            run_item(created="2026-11-20T11:30:00Z", rid=2),
            run_item(created="2026-11-20T12:00:05Z", rid=3),
            run_item(created="2026-11-20T12:05:00Z", rid=4),
            run_item(created="2026-11-20T11:59:30Z", rid=5),  # within skew
        )
    )
    rid = actions.find_run(CFG, FOLDER, SINCE, gh=gh, sleep=lambda s: None)
    assert rid == 4
    assert gh.calls[0][:6] == ["run", "list", "-R", REPO, "--workflow", "build.yml"]


def test_find_run_retries_then_succeeds():
    gh = FakeGh(runs(), runs(run_item(rid=7)))
    sleeps = []
    assert actions.find_run(CFG, FOLDER, SINCE, gh=gh, sleep=sleeps.append, wait=6) == 7
    assert sleeps == [6]


def test_find_run_raises_after_attempts():
    gh = FakeGh(runs(run_item("other"), run_item(created="2026-11-20T10:00:00Z")))
    sleeps = []
    with pytest.raises(ActionsError) as e:
        actions.find_run(CFG, FOLDER, SINCE, gh=gh, attempts=3, sleep=sleeps.append)
    assert str(e.value) == "could not find the workflow run"
    assert len(gh.calls) == 3
    assert len(sleeps) == 2


def test_find_run_survives_bad_gh_output():
    gh = FakeGh(done("not json"), done("", code=1), OSError("x"), runs(run_item(rid=8)))
    assert actions.find_run(CFG, FOLDER, SINCE, gh=gh, attempts=4, sleep=lambda s: None) == 8


# wait_run


def test_wait_run_returns_conclusion():
    gh = FakeGh(
        done(json.dumps({"status": "queued", "conclusion": ""})),
        done(json.dumps({"status": "in_progress", "conclusion": ""})),
        done(json.dumps({"status": "completed", "conclusion": "success"})),
    )
    t = {"now": 0.0}
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t["now"] += s

    assert actions.wait_run(CFG, 5, 600, gh=gh, sleep=sleep, clock=lambda: t["now"]) == "success"
    assert sleeps == [30, 30]
    assert gh.calls[0] == ["run", "view", "5", "-R", REPO, "--json", "status,conclusion"]


def test_wait_run_building_at_limit():
    gh = FakeGh(done(json.dumps({"status": "in_progress", "conclusion": ""})))
    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s

    assert actions.wait_run(CFG, 5, 100, gh=gh, sleep=sleep, clock=lambda: t["now"]) == "building"
    assert t["now"] <= 120


def test_wait_run_tolerates_gh_errors():
    done_json = json.dumps({"status": "completed", "conclusion": "failure"})
    gh = FakeGh(done("", code=1), done(done_json))
    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s

    assert actions.wait_run(CFG, 5, 600, gh=gh, sleep=sleep, clock=lambda: t["now"]) == "failure"


# outcome


def view(run_conclusion, build, publish):
    jobs = []
    if build is not None:
        jobs.append({"name": "build", "conclusion": build, "steps": []})
    if publish is not None:
        jobs.append({"name": "publish", "conclusion": publish, "steps": []})
    return done(json.dumps({"status": "completed", "conclusion": run_conclusion, "jobs": jobs}))


def content(payload):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    b64 = base64.encodebytes(raw.encode()).decode()
    return done(json.dumps({"content": b64, "encoding": "base64"}))


def test_outcome_published():
    gh = FakeGh(
        view("success", "success", "success"),
        content({"status": "published", "folder": FOLDER}),
    )
    assert actions.outcome(CFG, 5, FOLDER, gh=gh)[0] == "published"
    assert gh.calls[0] == ["run", "view", "5", "-R", REPO, "--json", "status,conclusion,jobs"]
    assert gh.calls[1] == ["api", f"repos/{REPO}/contents/{FOLDER}/build-result.json"]


def test_outcome_did_not_run():
    gh = FakeGh(
        view("success", "success", "success"),
        content({"status": "did_not_run", "folder": FOLDER}),
    )
    assert actions.outcome(CFG, 5, FOLDER, gh=gh)[0] == "did_not_run"


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "weird", "folder": FOLDER},
        {"status": "published", "folder": "other"},
        {"status": "published"},
        ["list"],
        "not json {",
    ],
)
def test_outcome_bad_result_file(payload):
    gh = FakeGh(view("success", "success", "success"), content(payload))
    assert actions.outcome(CFG, 5, FOLDER, gh=gh) == ("failed", "unexpected build result")


def test_outcome_result_fetch_error():
    gh = FakeGh(view("success", "success", "success"), done("", code=1))
    assert actions.outcome(CFG, 5, FOLDER, gh=gh) == ("failed", "unexpected build result")
    gh = FakeGh(view("success", "success", "success"), done(json.dumps({"content": "!!!"})))
    assert actions.outcome(CFG, 5, FOLDER, gh=gh) == ("failed", "unexpected build result")


def test_outcome_publish_failure_is_blocked():
    gh = FakeGh(view("failure", "success", "failure"))
    assert actions.outcome(CFG, 5, FOLDER, gh=gh) == ("blocked", "leak scan blocked the build")


@pytest.mark.parametrize("build,publish", [("failure", "skipped"), ("failure", None)])
def test_outcome_build_failure(build, publish):
    gh = FakeGh(view("failure", build, publish))
    assert actions.outcome(CFG, 5, FOLDER, gh=gh) == ("failed", "build job failed")


def test_outcome_timed_out_build():
    gh = FakeGh(view("failure", "timed_out", "skipped"))
    assert actions.outcome(CFG, 5, FOLDER, gh=gh) == ("failed", "build job failed")


def test_outcome_cancelled():
    gh = FakeGh(view("cancelled", "cancelled", "skipped"))
    assert actions.outcome(CFG, 5, FOLDER, gh=gh) == ("failed", "workflow cancelled")


def test_outcome_gh_failure_and_garbage():
    for reply in (done("", code=1), done("nope"), OSError("x")):
        assert actions.outcome(CFG, 5, FOLDER, gh=FakeGh(reply)) == (
            "failed",
            "could not read the workflow run",
        )


def test_reasons_never_carry_gh_output():
    gh = FakeGh(view("failure", "failure", "skipped"))
    status, reason = actions.outcome(CFG, 5, FOLDER, gh=gh)
    assert "SECRET" not in reason


def test_url():
    assert actions.url(CFG, FOLDER) == f"https://github.com/{REPO}/tree/main/{FOLDER}"


# gh helper


def test_gh_env_is_allowlisted(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_TOKEN", "tok2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    env = actions.gh_env()
    for bad in ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY"):
        assert bad not in env
    assert env["GH_PROMPT_DISABLED"] == "1"
    assert env["GH_NO_UPDATE_NOTIFIER"] == "1"
    assert env["NO_COLOR"] == "1"
    assert "PATH" in env


def test_gh_env_passes_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))
    assert actions.gh_env()["GH_CONFIG_DIR"] == str(tmp_path)


def test_gh_runs_subprocess_with_argv_and_env(monkeypatch):
    got = {}

    def fake_run(argv, **kw):
        got["argv"] = argv
        got["kw"] = kw
        return done("ok")

    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    monkeypatch.setenv("GH_TOKEN", "tok")
    out = actions.gh(["run", "list"], input="in", timeout=5)
    assert out.stdout == "ok"
    assert got["argv"][1:] == ["run", "list"]
    assert got["kw"]["input"] == "in"
    assert got["kw"]["timeout"] == 5
    assert got["kw"]["encoding"] == "utf-8"
    assert got["kw"]["errors"] == "replace"
    assert not got["kw"].get("shell")
    assert "GH_TOKEN" not in got["kw"]["env"]
