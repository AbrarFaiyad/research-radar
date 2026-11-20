import base64
import json
import subprocess
from types import SimpleNamespace

import pytest

from radar import cli, podcasts, settings
from radar.builds.actions import ActionsError
from radar.config import PodcastsCfg
from radar.pipeline import latest_run
from radar.ui import jobs

REPO = "someone/daily-podcasts"
RESULT = {
    "headline": "Fine-tuning MLIPs gets cheaper",
    "items": [
        {
            "title": "LoRA for MACE",
            "primary_url": "https://arxiv.org/abs/2611.00001",
            "relevance": "SECRET-RELEVANCE core to the thesis",
            "why_it_matters": "SECRET-WHY",
        },
        {"title": "No link item", "primary_url": " ", "relevance": "SECRET-RELEVANCE"},
    ],
    "rejected": [{"title": "REJECTED-POST"}],
}


def done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, stdout=out, stderr=err)


class FakeGh:
    """Stands in for gh: `release view` answers from `release`, everything else succeeds."""

    def __init__(self, release=None, repo_exists=True, fail=(), readme_exists=False):
        self.release = release  # None = no such release; else list of asset names
        self.repo_exists = repo_exists
        self.readme_exists = readme_exists
        self.fail = fail
        self.calls = []

    def __call__(self, args, *, input=None, timeout=60):
        self.calls.append((list(args), input))
        if any(args[: len(f)] == list(f) for f in self.fail):
            return done(1, err="SECRET-stderr C:/Users/someone")
        if args[:2] == ["release", "view"]:
            if self.release is None:
                return done(1, err="release not found")
            return done(0, json.dumps({"assets": [{"name": n} for n in self.release]}))
        if args[:2] == ["repo", "view"]:
            return done(0 if self.repo_exists else 1)
        if args[:2] == ["api", f"repos/{REPO}/contents/README.md"]:
            return done(0 if self.readme_exists else 1)
        return done(0)

    def verbs(self):
        return [tuple(a[:2]) for a, _ in self.calls]


@pytest.fixture
def pcfg(cfg):
    cfg.podcasts = PodcastsCfg(enabled=True, repo=REPO)
    return cfg


def make_folder(cfg, name="2026-11-21", audio="episode.m4a"):
    folder = cfg.digests_dir / name
    folder.mkdir(parents=True)
    (folder / "summary.md").write_text("# digest\n", encoding="utf-8")
    if audio:
        (folder / audio).write_bytes(b"audio-bytes")
    return folder


def record(folder):
    return json.loads((folder / "podcast.json").read_text(encoding="utf-8"))


def test_creates_release_and_uploads_episode(pcfg):
    folder = make_folder(pcfg)
    gh = FakeGh(release=None)
    rec = podcasts.publish(pcfg, folder, RESULT, gh=gh)
    create = next(a for a, _ in gh.calls if a[:2] == ["release", "create"])
    assert "2026-11-21" in create and str(folder / "episode.m4a") in create
    assert create[create.index("-R") + 1] == REPO
    assert create[create.index("--title") + 1] == "2026-11-21: Fine-tuning MLIPs gets cheaper"
    assert rec == record(folder)
    assert rec["status"] == "published" and rec["tag"] == "2026-11-21"
    assert rec["url"] == f"https://github.com/{REPO}/releases/download/2026-11-21/episode.m4a"
    assert rec["page"] == f"https://github.com/{REPO}/releases/tag/2026-11-21"


def test_notes_have_titles_and_urls_only(pcfg):
    folder = make_folder(pcfg)
    gh = FakeGh(release=None)
    podcasts.publish(pcfg, folder, RESULT, gh=gh)
    create, notes = next((a, i) for a, i in gh.calls if a[:2] == ["release", "create"])
    text = notes if notes is not None else " ".join(create)
    assert "LoRA for MACE" in text and "https://arxiv.org/abs/2611.00001" in text
    assert "No link item" in text
    for banned in ("relevance", "SECRET", "REJECTED", "notebooklm.google", "thesis"):
        assert banned.lower() not in text.lower()
    assert "SECRET" not in "\n".join(" ".join(a) for a, _ in gh.calls)


def test_existing_release_with_asset_is_left_alone(pcfg):
    folder = make_folder(pcfg)
    gh = FakeGh(release=["episode.m4a"])
    rec = podcasts.publish(pcfg, folder, RESULT, gh=gh)
    assert rec["status"] == "published"
    assert ("release", "create") not in gh.verbs() and ("release", "upload") not in gh.verbs()


def test_existing_release_without_asset_gets_upload(pcfg):
    folder = make_folder(pcfg)
    gh = FakeGh(release=[])
    rec = podcasts.publish(pcfg, folder, RESULT, gh=gh)
    up = next(a for a, _ in gh.calls if a[:2] == ["release", "upload"])
    assert up[2] == "2026-11-21" and str(folder / "episode.m4a") in up
    assert ("release", "create") not in gh.verbs()
    assert rec["status"] == "published"


def test_second_digest_of_a_day_uses_suffix_tag(pcfg):
    folder = make_folder(pcfg, "2026-11-21-2")
    rec = podcasts.publish(pcfg, folder, RESULT, gh=FakeGh())
    assert rec["tag"] == "2026-11-21-2"
    assert rec["url"].endswith("/2026-11-21-2/episode.m4a")


@pytest.mark.parametrize("name", ["latest", "2026-11-21_build", "x2026-11-21", "2026-1-21"])
def test_bad_tag_is_skipped(pcfg, name):
    folder = pcfg.digests_dir / name
    folder.mkdir(parents=True)
    (folder / "episode.m4a").write_bytes(b"a")
    gh = FakeGh()
    rec = podcasts.publish(pcfg, folder, RESULT, gh=gh)
    assert rec["status"] == "skipped" and gh.calls == []


def test_no_episode_is_skipped(pcfg):
    folder = make_folder(pcfg, audio=None)
    gh = FakeGh()
    rec = podcasts.publish(pcfg, folder, RESULT, gh=gh)
    assert rec["status"] == "skipped" and rec["reason"] == "no episode"
    assert record(folder)["status"] == "skipped" and gh.calls == []


@pytest.mark.parametrize(
    "headline",
    [
        "Sent to owner.name@example.org today",
        "Saved in C:\\Users\\someone\\Documents",
    ],
)
def test_personal_marker_in_text_skips_before_any_release_call(pcfg, headline):
    pcfg.email.to = "owner.name@example.org"
    folder = make_folder(pcfg)
    gh = FakeGh()
    rec = podcasts.publish(pcfg, folder, {**RESULT, "headline": headline}, gh=gh)
    assert rec["status"] == "skipped"
    assert rec["reason"] == "release text contains personal information"
    assert gh.calls == []
    assert record(folder)["reason"] == rec["reason"]


def test_secret_pattern_in_notes_skips(pcfg):
    folder = make_folder(pcfg)
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    result = {"headline": "H", "items": [{"title": token, "primary_url": ""}]}
    gh = FakeGh()
    rec = podcasts.publish(pcfg, folder, result, gh=gh)
    assert rec["status"] == "skipped" and gh.calls == []


def test_marker_in_item_title_skips(pcfg):
    pcfg.email.to = "owner.name@example.org"
    folder = make_folder(pcfg)
    result = {**RESULT, "items": [{"title": "by owner.name@example.org", "primary_url": ""}]}
    gh = FakeGh()
    rec = podcasts.publish(pcfg, folder, result, gh=gh)
    assert rec["status"] == "skipped" and gh.calls == []


def test_high_entropy_url_is_not_a_finding(pcfg):
    folder = make_folder(pcfg)
    url = "https://example.org/p/" + "aZ3kQ9xLm2Vb7TnR4sWc8YdE1uHf6GjP0oIq5Ut"
    result = {"headline": "H", "items": [{"title": "T", "primary_url": url}]}
    rec = podcasts.publish(pcfg, folder, result, gh=FakeGh())
    assert rec["status"] == "published"


def test_gh_failure_is_failed_with_fixed_phrase(pcfg):
    folder = make_folder(pcfg)
    gh = FakeGh(release=None, fail=[("release", "create")])
    rec = podcasts.publish(pcfg, folder, RESULT, gh=gh)
    assert rec["status"] == "failed" and rec["reason"] == podcasts.FAILED
    assert "SECRET" not in json.dumps(rec) and "someone" not in rec["reason"]
    assert "url" not in rec and record(folder) == rec


def test_gh_oserror_is_failed(pcfg):
    folder = make_folder(pcfg)

    def boom(args, **kw):
        raise OSError("gh missing C:/Users/someone")

    rec = podcasts.publish(pcfg, folder, RESULT, gh=boom)
    assert rec["status"] == "failed" and rec["reason"] == podcasts.FAILED


def test_upload_failure_on_existing_release_is_failed(pcfg):
    folder = make_folder(pcfg)
    gh = FakeGh(release=[], fail=[("release", "upload")])
    assert podcasts.publish(pcfg, folder, RESULT, gh=gh)["status"] == "failed"


def test_no_repo_is_skipped(pcfg):
    pcfg.podcasts.repo = ""
    folder = make_folder(pcfg)
    gh = FakeGh()
    rec = podcasts.publish(pcfg, folder, RESULT, gh=gh)
    assert rec["status"] == "skipped" and gh.calls == []


def test_mp3_preferred_like_the_email(pcfg):
    folder = make_folder(pcfg)
    (folder / "episode.mp3").write_bytes(b"mp3")
    rec = podcasts.publish(pcfg, folder, RESULT, gh=FakeGh())
    assert rec["url"].endswith("/episode.mp3")


# ---- repo setup ---------------------------------------------------------------------------------
def test_init_repo_creates_public_repo_and_readme(pcfg):
    gh = FakeGh(repo_exists=False)
    out = podcasts.init_repo(pcfg, gh=gh)
    assert out == f"https://github.com/{REPO}"
    create = next(a for a, _ in gh.calls if a[:2] == ["repo", "create"])
    assert create[2] == REPO and "--public" in create and "--description" in create
    put = next(a for a, _ in gh.calls if a[0] == "api" and "PUT" in a)
    assert f"repos/{REPO}/contents/README.md" in put
    content = next(x for x in put if x.startswith("content="))[len("content=") :]
    assert base64.b64decode(content).decode() == podcasts.README_TEXT
    assert podcasts.README_TEXT.startswith("Daily Research Radar podcast episodes")


def test_init_repo_existing_repo_and_readme_do_nothing(pcfg):
    gh = FakeGh(repo_exists=True, readme_exists=True)
    podcasts.init_repo(pcfg, gh=gh)
    assert ("repo", "create") not in gh.verbs()
    assert not [a for a, _ in gh.calls if "PUT" in a]


def test_init_repo_failure_is_a_fixed_phrase(pcfg):
    gh = FakeGh(repo_exists=False, fail=[("repo", "create")])
    with pytest.raises(ActionsError) as e:
        podcasts.init_repo(pcfg, gh=gh)
    assert "SECRET" not in str(e.value) and "someone" not in str(e.value)


def test_init_repo_needs_repo(pcfg):
    pcfg.podcasts.repo = ""
    with pytest.raises(ActionsError, match="no target repo"):
        podcasts.init_repo(pcfg, gh=FakeGh())


# ---- config, settings, jobs ----------------------------------------------------------------------
def test_podcasts_config_defaults_disabled(cfg):
    assert cfg.podcasts == PodcastsCfg(enabled=False, repo="")


def test_settings_fields_declared():
    en = settings.BY_ID["podcasts.enabled"]
    assert en.kind == "bool" and en.label == "Publish podcasts to GitHub"
    assert settings.BY_ID["podcasts.repo"].kind == "str"


def test_settings_validation():
    f = settings.BY_ID["podcasts.repo"]
    assert settings.coerce(f, "me/daily-podcasts") == "me/daily-podcasts"
    assert settings.coerce(f, "") == ""
    with pytest.raises(ValueError, match="owner/name"):
        settings.coerce(f, "not a repo")
    with pytest.raises(settings.SettingsError) as e:
        settings.validate({"podcasts.enabled": True}, {"podcasts.repo": ""})
    assert "podcasts.repo" in e.value.errors
    assert settings.validate({"podcasts.enabled": True, "podcasts.repo": "me/x"}, {})


def test_settings_save_roundtrip(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.toml").write_text("[podcasts]\nenabled = false\n")
    settings.save(tmp_path, {"podcasts.enabled": True, "podcasts.repo": "me/x"})
    vals = settings.load(tmp_path)
    assert vals["podcasts.enabled"] is True and vals["podcasts.repo"] == "me/x"


def test_job_actions():
    assert jobs.ACTIONS["podcast-publish"] == ["podcast", "--publish"]
    assert jobs.ACTIONS["podcast-init-repo"] == ["podcast", "--init-repo"]
    assert jobs.ACTIONS["podcast"] == ["podcast"]


# ---- daily run ------------------------------------------------------------------------------------
RES = {"headline": "H", "items": [{"title": "T", "primary_url": "https://example.org/x"}]}


def daily_setup(cfg, monkeypatch, *, digest=True, gh=None, headline="H"):
    cfg.notebooklm.enabled = False
    cfg.email.enabled = False  # the real config may enable email; never send from a test
    monkeypatch.setattr(cli, "stage_email", lambda *a, **k: pytest.fail("email sent"))
    calls = []
    monkeypatch.setattr(cli, "stage_scan", lambda cfg, *a, **k: {})

    def fake_run_digest(cfg_, store, make_audio=None, after_codex=None, **_):
        if not digest:
            return None
        folder = make_folder(cfg_)
        res = {**RES, "headline": headline}
        (folder / "codex_result.json").write_text(json.dumps(res), encoding="utf-8")
        return SimpleNamespace(
            kept=1, reviewed=1, summary_md=folder / "summary.md", audio=None, folder=folder
        )

    import radar.digest

    monkeypatch.setattr(radar.digest, "run_digest", fake_run_digest)

    def fake_publish(cfg_, folder, result, gh=None):
        calls.append((folder.name, result["headline"]))
        return {"tag": folder.name, "status": "published"}

    if gh is None:
        monkeypatch.setattr(podcasts, "publish", fake_publish)
    else:  # the real publisher with a fake gh
        real = podcasts.publish
        monkeypatch.setattr(podcasts, "publish", lambda c, f, r, gh_=gh: real(c, f, r, gh=gh_))
    return calls


def stages(cfg):
    return {s["name"]: s for s in latest_run(cfg)["stages"]}


def test_daily_publishes_after_digest(pcfg, monkeypatch):
    calls = daily_setup(pcfg, monkeypatch)
    assert cli.cmd_daily(pcfg, SimpleNamespace()) == 0
    assert [s["name"] for s in latest_run(pcfg)["stages"]] == ["scan", "digest", "podcast-publish"]
    assert calls == [("2026-11-21", "H")]


def test_daily_without_podcasts_has_no_stage(cfg, monkeypatch):
    calls = daily_setup(cfg, monkeypatch)
    assert cli.cmd_daily(cfg, SimpleNamespace()) == 0
    assert "podcast-publish" not in stages(cfg) and calls == []


def test_daily_without_digest_skips_cleanly(pcfg, monkeypatch):
    calls = daily_setup(pcfg, monkeypatch, digest=False)
    assert cli.cmd_daily(pcfg, SimpleNamespace()) == 0
    st = stages(pcfg)["podcast-publish"]
    assert st["ok"] and st["skipped"] and calls == []


def test_publish_failure_never_fails_the_run(pcfg, monkeypatch):
    daily_setup(pcfg, monkeypatch)
    monkeypatch.setattr(
        podcasts, "publish", lambda *a, **k: {"tag": "t", "status": "failed", "reason": "x"}
    )
    assert cli.cmd_daily(pcfg, SimpleNamespace()) == 0
    st = stages(pcfg)["podcast-publish"]
    assert not st["ok"] and st["optional"]


def test_publish_crash_never_fails_the_run(pcfg, monkeypatch):
    daily_setup(pcfg, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(podcasts, "publish", boom)
    assert cli.cmd_daily(pcfg, SimpleNamespace()) == 0


def test_cli_publish_and_init_repo(pcfg, monkeypatch, capsys):
    folder = make_folder(pcfg)
    (folder / "codex_result.json").write_text(json.dumps(RES), encoding="utf-8")
    seen = []
    monkeypatch.setattr(
        podcasts,
        "publish",
        lambda c, f, r, gh=None: seen.append(f.name) or {"status": "published", "tag": f.name},
    )
    monkeypatch.setattr(podcasts, "init_repo", lambda c, gh=None: "https://github.com/x/y")
    args = cli.build_parser().parse_args(["podcast", "--publish", "2026-11-21"])
    assert cli.cmd_podcast(pcfg, args) == 0 and seen == ["2026-11-21"]
    args = cli.build_parser().parse_args(["podcast", "--init-repo"])
    assert cli.cmd_podcast(pcfg, args) == 0
    assert "https://github.com/x/y" in capsys.readouterr().out


# ---- review fixes ---------------------------------------------------------------------------------
def test_notes_are_only_titles_and_links(pcfg):
    folder = make_folder(pcfg)
    gh = FakeGh()
    podcasts.publish(pcfg, folder, RESULT, gh=gh)
    notes = next(i for a, i in gh.calls if a[:2] == ["release", "create"])
    assert notes == "- LoRA for MACE: https://arxiv.org/abs/2611.00001\n- No link item\n"


def test_only_http_links_are_published(pcfg):
    folder = make_folder(pcfg)
    items = [
        {"title": "A", "primary_url": "file:///x/y"},
        {"title": "B", "primary_url": "javascript:alert(1)"},
        {"title": "C", "primary_url": "HTTP://example.org/c"},
    ]
    gh = FakeGh()
    podcasts.publish(pcfg, folder, {"headline": "H", "items": items}, gh=gh)
    notes = next(i for a, i in gh.calls if a[:2] == ["release", "create"])
    assert notes == "- A\n- B\n- C: HTTP://example.org/c\n"


def test_later_skip_or_failure_keeps_published_record(pcfg):
    folder = make_folder(pcfg)
    first = podcasts.publish(pcfg, folder, RESULT, gh=FakeGh())
    assert first["status"] == "published"
    pcfg.email.to = "a@b.example.org"
    marked = {**RESULT, "headline": "mail a@b.example.org"}
    skipped = podcasts.publish(pcfg, folder, marked, gh=FakeGh())
    assert skipped["status"] == "skipped" and record(folder) == first
    failed = podcasts.publish(pcfg, folder, RESULT, gh=FakeGh(release=None, fail=[("release",)]))
    assert failed["status"] == "failed" and record(folder) == first


def test_prepare_failure_is_failed_with_fixed_phrase(pcfg, monkeypatch):
    import radar.podcast

    def boom(path):
        raise RuntimeError("decoder crashed on C:/Users/someone/x.m4a")

    monkeypatch.setattr(radar.podcast, "email_ready", boom)
    folder = make_folder(pcfg)
    gh = FakeGh()
    rec = podcasts.publish(pcfg, folder, RESULT, gh=gh)
    assert rec == {"tag": "2026-11-21", "status": "failed", "reason": "could not prepare episode"}
    assert gh.calls == [] and record(folder) == rec


def test_daily_all_gh_calls_fail_reports_fixed_phrase_and_rc0(pcfg, monkeypatch):
    gh = FakeGh(release=None, fail=[("release",)])
    daily_setup(pcfg, monkeypatch, gh=gh)
    assert cli.cmd_daily(pcfg, SimpleNamespace()) == 0
    st = stages(pcfg)["podcast-publish"]
    assert not st["ok"] and st["optional"]
    assert podcasts.FAILED in st["error"] and "SECRET" not in st["error"]
    assert ("release", "view") in gh.verbs() and ("release", "create") in gh.verbs()
    folder = pcfg.digests_dir / "2026-11-21"
    assert record(folder)["status"] == "failed"


def test_daily_marker_in_headline_skips_without_release_call_and_rc0(pcfg, monkeypatch):
    pcfg.email.to = "owner.name@example.org"
    gh = FakeGh()
    daily_setup(pcfg, monkeypatch, gh=gh, headline="mail owner.name@example.org")
    assert cli.cmd_daily(pcfg, SimpleNamespace()) == 0
    st = stages(pcfg)["podcast-publish"]
    assert st["ok"] and st["skipped"] and podcasts.PERSONAL in st["error"]
    assert gh.calls == []
