"""Podcast publishing in the doctor checks, email and Digests tab."""

import datetime as dt
import json

import pytest

from radar import mailer
from radar.doctor import run_checks
from tests.test_doctor import _builds_probes, _by_name
from tests.test_mailer import CFG, _digest
from tests.test_ui import H, client, root  # noqa: F401  (fixtures)

# ---- doctor


def _pod_on(cfg):
    cfg.podcasts.enabled, cfg.podcasts.repo = True, "someone/daily-podcasts"


def test_podcasts_check_absent_when_disabled(cfg, tmp_path):
    assert "daily-podcasts repo" not in _by_name(run_checks(cfg, _builds_probes(tmp_path)))


def test_podcasts_check_green_when_public(cfg, tmp_path):
    _pod_on(cfg)
    assert _by_name(run_checks(cfg, _builds_probes(tmp_path)))["daily-podcasts repo"].ok


def test_podcasts_check_offers_repo_setup(cfg, tmp_path):
    _pod_on(cfg)
    seen = []

    def vis(repo):
        seen.append(repo)

    probes = _builds_probes(tmp_path, repo_visibility=vis)
    c = _by_name(run_checks(cfg, probes))["daily-podcasts repo"]
    assert not c.ok and c.action == "podcast-init-repo"
    assert seen == ["someone/daily-podcasts"]


def test_podcasts_check_private_and_unset_fail(cfg, tmp_path):
    _pod_on(cfg)
    private = _builds_probes(tmp_path, repo_visibility=lambda r: "PRIVATE")
    assert not _by_name(run_checks(cfg, private))["daily-podcasts repo"].ok
    cfg.podcasts.repo = ""
    assert not _by_name(run_checks(cfg, _builds_probes(tmp_path)))["daily-podcasts repo"].ok


# ---- email


def _pod_body(tmp_path, rec):
    d, q = _digest(tmp_path)
    if rec is not None:
        (d / "podcast.json").write_text(json.dumps(rec), encoding="utf-8")
    msg = mailer.compose_daily(CFG, d, q, today=dt.date(2026, 11, 19))
    return msg.get_body(("plain",)).get_content()


def test_email_podcast_line_when_published(tmp_path):
    page = "https://github.com/o/r/releases/tag/2026-11-19"
    body = _pod_body(tmp_path, {"status": "published", "tag": "t", "page": page})
    assert f"Podcast online: {page}" in body


@pytest.mark.parametrize(
    "rec",
    [
        None,
        {"status": "skipped", "reason": "no audio"},
        {"status": "failed", "reason": "x"},
        {"status": "published", "page": "https://evil.example/x"},
        {"status": "published"},
    ],
)
def test_email_no_podcast_line_otherwise(tmp_path, rec):
    assert "Podcast online" not in _pod_body(tmp_path, rec)


# ---- Digests tab


def _digest_dir(c, name="2026-11-19"):
    d = c.cfg.digests_dir / name
    d.mkdir(parents=True)
    (d / "summary.md").write_text("# D\n", encoding="utf-8")
    return d


def test_digests_list_carries_podcast_record(client):  # noqa: F811
    d = _digest_dir(client)
    assert client.get("/api/digests").json()[0]["podcast"] is None
    page = "https://github.com/o/r/releases/tag/t"
    (d / "podcast.json").write_text(
        json.dumps({"status": "published", "tag": "t", "url": page, "page": page}), encoding="utf-8"
    )
    assert client.get("/api/digests").json()[0]["podcast"] == {"status": "published", "page": page}


def test_digests_list_drops_foreign_page_and_keeps_reason(client):  # noqa: F811
    d = _digest_dir(client)
    (d / "podcast.json").write_text(
        json.dumps({"status": "published", "page": "javascript:alert(1)"}), encoding="utf-8"
    )
    assert client.get("/api/digests").json()[0]["podcast"] == {"status": "published", "page": None}
    (d / "podcast.json").write_text(
        json.dumps({"status": "skipped", "reason": "<b>no audio</b>", "page": "https://x"}),
        encoding="utf-8",
    )
    assert client.get("/api/digests").json()[0]["podcast"] == {
        "status": "skipped",
        "page": None,
        "reason": "<b>no audio</b>",
    }
    (d / "podcast.json").write_text("not json", encoding="utf-8")
    assert client.get("/api/digests").json()[0]["podcast"] is None


def test_podcast_publish_job_gets_the_folder(client):  # noqa: F811
    r = client.post(
        "/api/jobs", json={"action": "podcast-publish", "folder": "2026-11-19"}, headers=H
    )
    assert r.status_code == 200
    assert client.runner.started[-1] == ("podcast-publish", ["2026-11-19"])


def test_page_has_podcast_controls(client):  # noqa: F811
    html = client.get("/").text
    for needle in (
        "podcast-publish",
        "podcast-init-repo",
        "Publish podcast",
        "Set up podcasts repo",
    ):
        assert needle in html


def test_podcast_publish_job_replace_adds_the_flag(client):  # noqa: F811
    r = client.post(
        "/api/jobs",
        json={"action": "podcast-publish", "folder": "2026-11-19", "replace": True},
        headers=H,
    )
    assert r.status_code == 200
    assert client.runner.started[-1] == ("podcast-publish", ["2026-11-19", "--replace"])
    client.runner.current.finished = 1.0
    client.post("/api/jobs", json={"action": "podcast-publish", "replace": "yes"}, headers=H)
    assert client.runner.started[-1] == ("podcast-publish", [])  # only a real true counts


def test_digests_list_says_whether_publishing_is_on(client):  # noqa: F811
    _digest_dir(client)
    assert client.get("/api/digests").json()[0]["podcasts_enabled"] is False
    r = client.post(
        "/api/settings", json={"podcasts.enabled": True, "podcasts.repo": "o/r"}, headers=H
    )
    assert r.status_code == 200
    assert client.get("/api/digests").json()[0]["podcasts_enabled"] is True


def test_page_keeps_digest_and_offers_publish_again(client):  # noqa: F811
    html = client.get("/").text
    assert "selectedDigest" in html and "Publish again" in html
    assert "podcasts_enabled" in html and "replace: true" in html


def test_cli_replace_flag_reaches_publish(cfg, monkeypatch):
    import json as _json
    from types import SimpleNamespace

    from radar import cli, podcasts

    d = cfg.digests_dir / "2026-11-19"
    d.mkdir(parents=True)
    (d / "summary.md").write_text("# D\n", encoding="utf-8")
    (d / "codex_result.json").write_text(_json.dumps({"headline": "H", "items": []}))
    seen = []

    def fake(c, f, r, **kw):
        seen.append(kw)
        return {"status": "published", "tag": f.name}

    monkeypatch.setattr(podcasts, "publish", fake)
    args = cli.build_parser().parse_args(["podcast", "--publish", "--replace", "2026-11-19"])
    assert args.replace is True and cli.cmd_podcast(cfg, args) == 0
    args = SimpleNamespace(init_repo=False, publish=True, folder="2026-11-19", replace=False)
    assert cli.cmd_podcast(cfg, args) == 0
    assert seen == [{"replace": True}, {}]
