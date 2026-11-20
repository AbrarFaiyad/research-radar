"""Every config key is editable in the panel (owner rule), including list and advanced keys."""

import shutil
from dataclasses import fields

import pytest
from fastapi.testclient import TestClient

from radar import config, settings
from radar.ui.app import create_app
from tests.test_ui import TOKEN, FakeRunner, H

SECTIONS = {
    "profile": config.ProfileCfg,
    "browser": config.BrowserCfg,
    "scan": config.ScanCfg,
    "llm": config.LlmCfg,
    "digest": config.DigestCfg,
    "notebooklm": config.NotebookLMCfg,
    "email": config.EmailCfg,
    "builds": config.BuildsCfg,
    "podcasts": config.PodcastsCfg,
}


@pytest.mark.parametrize("section", sorted(SECTIONS))
def test_schema_has_every_dataclass_field(section):
    keys = {f.key for f in settings.SCHEMA if f.section == section}
    assert {f.name for f in fields(SECTIONS[section])} == keys


def test_advanced_flag_exposed_in_schema_json():
    by_id = {f["id"]: f for f in settings.schema_json()}
    for key in (
        "llm.host",
        "email.smtp_host",
        "email.smtp_port",
        "digest.codex_command",
        "browser.mcp_command",
        "browser.mcp_args",
        "browser.feed_url",
        "builds.work_dir",
    ):
        assert by_id[key]["advanced"] is True, key
    assert by_id["scan.max_posts"]["advanced"] is False
    assert by_id["browser.mcp_args"]["kind"] == "lines"


@pytest.fixture
def env(tmp_path):
    (tmp_path / "config").mkdir()
    shutil.copy(
        config.REPO_ROOT / "config" / "config.example.toml",
        tmp_path / "config" / "config.example.toml",
    )
    cfg = config.load_config(root=tmp_path)
    cfg.data_dir = tmp_path / "data"
    return TestClient(create_app(cfg, token=TOKEN, runner=FakeRunner())), tmp_path


def test_mcp_args_round_trip_one_per_line_blank_dropped(env):
    client, root = env
    r = client.post(
        "/api/settings",
        json={"browser.mcp_args": "-y\n\n  @playwright/mcp@0.0.84 \n--headless\n"},
        headers=H,
    )
    assert r.status_code == 200
    want = ["-y", "@playwright/mcp@0.0.84", "--headless"]
    assert r.json()["values"]["browser.mcp_args"] == want
    text = (root / "config" / "config.toml").read_text(encoding="utf-8")
    assert '"--headless"' in text and "# Playwright MCP server, pinned." in text
    assert "# Human-paced" in text
    assert config.load_config(root=root).browser.mcp_args == want
    assert client.get("/api/settings").json()["values"]["browser.mcp_args"] == want


def test_mcp_args_accepts_a_list(env):
    client, _ = env
    r = client.post("/api/settings", json={"browser.mcp_args": ["-y", "x"]}, headers=H)
    assert r.json()["values"]["browser.mcp_args"] == ["-y", "x"]


@pytest.mark.parametrize(
    "key,bad",
    [
        ("llm.host", "ftp://x"),
        ("llm.host", "localhost:11434"),
        ("email.smtp_port", 0),
        ("email.smtp_port", 70000),
        ("email.smtp_host", "bad host!"),
        ("browser.feed_url", "https://evil.example/feed/"),
        ("browser.feed_url", "http://www.linkedin.com/feed/"),
        ("browser.mcp_command", ""),
        ("digest.codex_command", ""),
        ("browser.mcp_args", "x" * 600),
        ("notebooklm.length", "huge"),
    ],
)
def test_new_keys_validated(env, key, bad):
    client, root = env
    client.get("/api/settings")
    before = (root / "config" / "config.toml").read_text(encoding="utf-8")
    r = client.post("/api/settings", json={key: bad}, headers=H)
    assert r.status_code == 400 and key in r.json()["errors"]
    assert (root / "config" / "config.toml").read_text(encoding="utf-8") == before


def test_new_keys_accept_good_values(env):
    client, _ = env
    good = {
        "llm.host": "http://127.0.0.1:11434",
        "email.smtp_host": "smtp.example.org",
        "email.smtp_port": 465,
        "digest.codex_command": "codex",
        "browser.mcp_command": "npx",
        "browser.feed_url": "https://www.linkedin.com/feed/",
        "builds.work_dir": "",
        "notebooklm.length": "shorter",
    }
    r = client.post("/api/settings", json=good, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["values"]["email.smtp_port"] == 465
    assert r.json()["values"]["notebooklm.length"] == "shorter"


def test_page_groups_advanced_and_renders_textarea():
    html = (config.REPO_ROOT / "src" / "radar" / "ui" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    for needle in ("<details", "Advanced", "<textarea", '"lines"', "f.advanced"):
        assert needle in html
