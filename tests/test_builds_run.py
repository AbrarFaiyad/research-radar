import datetime as dt
import json
import subprocess

import pytest

from radar.builds import actions as actions_module
from radar.builds import run as run_mod
from radar.builds.actions import ActionsError
from radar.builds.run import render_prompt, run_build, sync_builds

DAY = dt.date(2026, 11, 21)
URL_PREFIX = "https://github.com/owner/daily/tree/main/"


class FakeActions:
    """Stands in for radar.builds.actions: records calls, returns what the test sets."""

    url = staticmethod(actions_module.url)

    def __init__(
        self,
        *,
        state="success",
        outcome=("published", ""),
        run_id=77,
        dispatch_exc=None,
        find_exc=None,
        wait_exc=None,
    ):
        self.state, self.result, self.run_id = state, outcome, run_id
        self.dispatch_exc, self.find_exc, self.wait_exc = dispatch_exc, find_exc, wait_exc
        self.calls = []

    def dispatch(self, cfg, folder, prompt):
        self.calls.append(("dispatch", folder, prompt))
        if self.dispatch_exc:
            raise self.dispatch_exc

    def find_run(self, cfg, folder, since):
        self.calls.append(("find_run", folder))
        if self.find_exc:
            raise self.find_exc
        return self.run_id

    def wait_run(self, cfg, run_id, limit_s):
        self.calls.append(("wait_run", run_id, limit_s))
        if self.wait_exc:
            raise self.wait_exc
        return self.state

    def outcome(self, cfg, run_id, folder):
        self.calls.append(("outcome", run_id, folder))
        return self.result

    def names(self):
        return [c[0] for c in self.calls]


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
    kw.setdefault("actions", FakeActions())
    kw.setdefault("today", DAY)
    kw.setdefault("log", lambda *_: None)
    return run_build(cfg, digest, result, **kw)


def records(cfg, day="2026-11-21"):
    return json.loads((cfg.builds_dir / "records" / f"{day}.json").read_text("utf-8"))


def history(cfg):
    path = cfg.builds_dir / "history.json"
    return (
        json.loads(path.read_text("utf-8")) if path.exists() else {"published": [], "attempts": []}
    )


def test_only_the_prompt_and_folder_are_sent(on_cfg, digest, result, monkeypatch):
    monkeypatch.setenv("RADAR_PRIVATE_MARKERS", "zed-private-marker")
    fake = FakeActions()
    rec = go(on_cfg, digest, result, actions=fake)
    _, folder, prompt = fake.calls[0]
    assert folder == rec.folder and folder.startswith("2026-11-21_mace_")
    assert "MACE LoRA finetune" in prompt and "COMMITTED-PROFILE-LINE" in prompt
    for word in ("zed-private-marker", "owner@fakemail", "relevance", "post_ids", "rejected"):
        assert word not in prompt
    for word in ("podcast_focus", "cpu_demo"):
        assert word not in prompt


def test_published(on_cfg, digest, result):
    fake = FakeActions()
    rec = go(on_cfg, digest, result, actions=fake)
    assert rec.status == "published" and rec.url == URL_PREFIX + rec.folder
    assert rec.package == "mace" and rec.run_id == 77
    assert fake.names() == ["dispatch", "find_run", "wait_run", "outcome"]
    assert fake.calls[2] == ("wait_run", 77, on_cfg.builds.poll_min * 60)
    assert history(on_cfg)["published"] == [
        {"package": "mace", "folder": rec.folder, "url": rec.url}
    ]
    data = json.loads((digest / "build.json").read_text(encoding="utf-8"))
    assert set(data) == {"day", "status", "package", "folder", "url", "seconds", "run_id"}
    assert records(on_cfg) == data


def test_did_not_run_is_published_to_history_with_reason(on_cfg, digest, result):
    fake = FakeActions(outcome=("did_not_run", "build did not run"))
    rec = go(on_cfg, digest, result, actions=fake)
    assert (rec.status, rec.reason) == ("did_not_run", "build did not run")
    assert rec.url == URL_PREFIX + rec.folder
    assert history(on_cfg)["published"][0]["package"] == "mace"


def test_blocked_has_no_url_and_is_an_attempt(on_cfg, digest, result):
    fake = FakeActions(outcome=("blocked", "leak scan blocked the build"))
    rec = go(on_cfg, digest, result, actions=fake)
    assert (rec.status, rec.url) == ("blocked", "")
    data = json.loads((digest / "build.json").read_text(encoding="utf-8"))
    assert data["reason"] == "leak scan blocked the build"
    assert history(on_cfg)["published"] == []
    assert history(on_cfg)["attempts"] == [
        {"package": "mace", "status": "blocked", "reason": "leak scan blocked the build"}
    ]


def test_failed_outcome(on_cfg, digest, result):
    rec = go(on_cfg, digest, result, actions=FakeActions(outcome=("failed", "build job failed")))
    assert (rec.status, rec.reason) == ("failed", "build job failed")
    assert history(on_cfg)["attempts"][0]["status"] == "failed"


def test_dispatch_error_fails_with_the_fixed_phrase(on_cfg, digest, result):
    fake = FakeActions(dispatch_exc=ActionsError("could not start the workflow"))
    rec = go(on_cfg, digest, result, actions=fake)
    assert (rec.status, rec.reason) == ("failed", "could not start the workflow")
    assert rec.run_id is None
    assert "outcome" not in fake.names()
    assert records(on_cfg)["status"] == "failed"


def test_building_when_the_run_outlasts_the_limit(on_cfg, digest, result):
    fake = FakeActions(state="building")
    rec = go(on_cfg, digest, result, actions=fake)
    assert rec.status == "building" and rec.run_id == 77 and rec.folder and rec.url == ""
    assert "outcome" not in fake.names()
    assert records(on_cfg)["status"] == "building" and records(on_cfg)["run_id"] == 77
    assert json.loads((digest / "build.json").read_text("utf-8"))["run_id"] == 77
    assert history(on_cfg) == {"published": [], "attempts": []}


def test_building_when_github_cannot_be_reached(on_cfg, digest, result):
    fake = FakeActions(wait_exc=ActionsError("could not reach GitHub"))
    rec = go(on_cfg, digest, result, actions=fake)
    assert (rec.status, rec.reason, rec.run_id) == ("building", "could not reach GitHub", 77)
    assert history(on_cfg) == {"published": [], "attempts": []}


def test_building_record_is_written_before_waiting(on_cfg, digest, result):
    seen = {}

    class Spy(FakeActions):
        def wait_run(self, cfg, run_id, limit_s):
            seen["record"] = records(cfg)
            return super().wait_run(cfg, run_id, limit_s)

    go(on_cfg, digest, result, actions=Spy())
    assert seen["record"]["status"] == "building" and seen["record"]["run_id"] == 77


def test_building_does_not_count_as_published_today(on_cfg, digest, result):
    go(on_cfg, digest, result, actions=FakeActions(state="building"))
    assert history(on_cfg)["published"] == []
    on_cfg.builds_dir.joinpath("history.json").write_text(
        json.dumps(
            {
                "published": [{"package": "x", "folder": "2026-11-21_x_y", "url": "u"}],
                "attempts": [],
            }
        ),
        encoding="utf-8",
    )
    rec = go(on_cfg, digest, result)
    assert (rec.status, rec.reason) == ("skipped", "already published today")


def test_a_running_build_blocks_a_second_one(on_cfg, digest, result, tmp_path):
    go(on_cfg, digest, result, actions=FakeActions(state="building"))
    second = tmp_path / "digests" / "2026-11-21-2"
    second.mkdir()
    fake = FakeActions()
    rec = go(on_cfg, second, {"items": [item(code_url="https://github.com/o/other")]}, actions=fake)
    assert (rec.status, rec.reason) == ("skipped", "a build is still running")
    assert fake.calls == []
    assert records(on_cfg)["status"] == "building"  # the skip did not hide the running build
    assert json.loads((digest / "build.json").read_text("utf-8"))["status"] == "building"


def test_skip_disabled_and_no_repo(cfg, digest, result):
    assert go(cfg, digest, result).reason == "builds disabled"
    cfg.builds.enabled = True
    assert go(cfg, digest, result).reason == "no target repo"


def test_no_candidate(on_cfg, digest):
    fake = FakeActions()
    rec = go(on_cfg, digest, {"items": []}, actions=fake)
    assert rec.status == "skipped" and fake.calls == []


def test_one_published_build_per_day(on_cfg, digest, result, tmp_path):
    assert go(on_cfg, digest, result).status == "published"
    second = tmp_path / "digests" / "2026-11-21-2"
    second.mkdir()
    rec = go(on_cfg, second, {"items": [item(code_url="https://github.com/o/other")]})
    assert (rec.status, rec.reason) == ("skipped", "already published today")
    kept = records(on_cfg)
    assert kept["status"] == "published" and kept["url"]
    assert json.loads((second / "build.json").read_text("utf-8"))["status"] == "skipped"


def test_corrupt_history_skips(on_cfg, digest, result):
    on_cfg.builds_dir.mkdir(parents=True, exist_ok=True)
    (on_cfg.builds_dir / "history.json").write_text("{nope", encoding="utf-8")
    fake = FakeActions()
    rec = go(on_cfg, digest, result, actions=fake)
    assert (rec.status, rec.reason) == ("skipped", "build history file is corrupt")
    assert fake.calls == []


def test_missing_prompt_template_fails(on_cfg, digest, result):
    (on_cfg.root / "prompts" / "build_task.md").unlink()
    fake = FakeActions()
    rec = go(on_cfg, digest, result, actions=fake)
    assert (rec.status, rec.reason) == ("failed", "could not prepare the build prompt")
    assert fake.calls == []


def test_field_profile_comes_from_committed_file(on_cfg, digest, result, tmp_path):
    other = tmp_path / "private_profile.md"
    other.write_text("PRIVATE-PROFILE-TEXT", encoding="utf-8")
    on_cfg.field_profile = other
    fake = FakeActions()
    go(on_cfg, digest, result, actions=fake)
    assert "PRIVATE-PROFILE-TEXT" not in fake.calls[0][2]
    assert "COMMITTED-PROFILE-LINE" in fake.calls[0][2]


def test_committed_profile_reaches_prompt_even_with_crlf_checkout(on_cfg, digest, result):
    profile = on_cfg.root / "config" / "field_profile.md"
    profile.write_bytes(PROFILE.replace("\n", "\r\n").encode("utf-8"))
    fake = FakeActions()
    assert go(on_cfg, digest, result, actions=fake).status == "published"
    assert "COMMITTED-PROFILE-LINE" in fake.calls[0][2]


def test_uncommitted_profile_skips_build(on_cfg, digest, result):
    profile = on_cfg.root / "config" / "field_profile.md"
    profile.write_text(PROFILE + "From the CV: PRIVATE-CV-TEXT\n", encoding="utf-8")
    fake = FakeActions()
    rec = go(on_cfg, digest, result, actions=fake)
    assert (rec.status, rec.reason) == ("skipped", "field profile has uncommitted changes")
    assert fake.calls == []


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


# ---- sync_builds -------------------------------------------------------------------------------
def start_building(cfg, digest, result, run_id=77):
    return go(cfg, digest, result, actions=FakeActions(state="building", run_id=run_id))


def test_sync_finishes_a_building_record_and_history(on_cfg, result):
    digest = on_cfg.digests_dir / "2026-11-21"
    digest.mkdir(parents=True)
    built = start_building(on_cfg, digest, result)
    fake = FakeActions(state="success")
    done = sync_builds(on_cfg, actions=fake, log=lambda *_: None)
    assert [r.status for r in done] == ["published"]
    assert fake.calls[0] == ("wait_run", 77, 0)  # one poll, no waiting
    assert fake.calls[1] == ("outcome", 77, built.folder)
    rec = records(on_cfg)
    assert rec["status"] == "published" and rec["url"] == URL_PREFIX + built.folder
    assert json.loads((digest / "build.json").read_text("utf-8")) == rec
    assert history(on_cfg)["published"] == [
        {"package": "mace", "folder": built.folder, "url": rec["url"]}
    ]


def test_sync_records_a_failed_run_as_an_attempt(on_cfg, digest, result):
    start_building(on_cfg, digest, result)
    fake = FakeActions(outcome=("failed", "build job failed"))
    sync_builds(on_cfg, actions=fake, log=lambda *_: None)
    assert records(on_cfg)["status"] == "failed" and records(on_cfg)["reason"] == "build job failed"
    assert history(on_cfg)["attempts"][0]["status"] == "failed"


def test_sync_leaves_a_run_that_is_still_going(on_cfg, digest, result):
    start_building(on_cfg, digest, result)
    fake = FakeActions(state="building")
    assert sync_builds(on_cfg, actions=fake, log=lambda *_: None) == []
    assert "outcome" not in fake.names() and records(on_cfg)["status"] == "building"


def test_sync_leaves_the_record_when_github_is_unreachable(on_cfg, digest, result):
    start_building(on_cfg, digest, result)
    fake = FakeActions(wait_exc=ActionsError("could not reach GitHub"))
    assert sync_builds(on_cfg, actions=fake, log=lambda *_: None) == []
    assert records(on_cfg)["status"] == "building"


def test_sync_ignores_finished_and_foreign_records(on_cfg, digest, result):
    go(on_cfg, digest, result)  # published
    rdir = on_cfg.builds_dir / "records"
    (rdir / "2026-11-20.json").write_text(json.dumps({"day": "d", "status": "failed"}), "utf-8")
    (rdir / "2026-11-19.json").write_text("{nope", encoding="utf-8")
    (rdir / "2026-11-18.json").write_text(json.dumps({"status": "building"}), "utf-8")  # no run_id
    fake = FakeActions()
    assert sync_builds(on_cfg, actions=fake, log=lambda *_: None) == []
    assert fake.calls == []


def test_sync_with_no_records_dir(cfg):
    assert sync_builds(cfg, actions=FakeActions(), log=lambda *_: None) == []


# ---- prompt ------------------------------------------------------------------------------------
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
    for needle in ("CPU only", "output/", "BUILD_LOG.md", "real output", "requirements.txt"):
        assert needle in text
    for gone in ("/work", "test_demo.py", "Docker", "proxy"):
        assert gone not in text
    assert run_mod.TEMPLATE_RE.findall(text) == ["DAY", "FIELD_PROFILE", "ITEM_JSON"]


def test_prompt_asks_for_notebook():
    from radar.config import REPO_ROOT

    text = (REPO_ROOT / "prompts" / "build_task.md").read_text(encoding="utf-8")
    for needle in (
        "demo.ipynb",
        "Executive summary",
        "papermill",
        "ipykernel",
        "under 10 minutes",
        "1 MB",
        "python3",
        "relative",
        "%pip",
    ):
        assert needle in text
    assert "/tmp" in text
    assert "relevance" not in text and "post_ids" not in text


def test_prompt_sends_venvs_and_clones_to_tmp():
    from radar.config import REPO_ROOT

    text = (REPO_ROOT / "prompts" / "build_task.md").read_text(encoding="utf-8")
    assert "virtual environments" in text and "/tmp" in text


def test_run_not_found_is_failed_without_a_history_attempt(on_cfg, digest, result):
    fake = FakeActions(find_exc=ActionsError("could not find the workflow run"))
    rec = go(on_cfg, digest, result, actions=fake)
    assert (rec.status, rec.reason) == ("failed", "workflow started but run not found")
    assert records(on_cfg)["reason"] == "workflow started but run not found"
    assert history(on_cfg) == {"published": [], "attempts": []}


def test_sync_keeps_building_when_the_run_cannot_be_read(on_cfg, digest, result):
    start_building(on_cfg, digest, result)
    fake = FakeActions(outcome=("failed", "could not read the workflow run"))
    assert sync_builds(on_cfg, actions=fake, log=lambda *_: None) == []
    assert records(on_cfg)["status"] == "building"
    assert history(on_cfg) == {"published": [], "attempts": []}


def test_building_record_is_written_for_a_string_digest_folder(on_cfg, digest, result):
    rec = go(on_cfg, str(digest), result, actions=FakeActions(state="building"))
    assert rec.status == "building"
    assert json.loads((digest / "build.json").read_text("utf-8"))["status"] == "building"
