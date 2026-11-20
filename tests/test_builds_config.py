import shutil
from pathlib import Path

import pytest

from radar import settings


@pytest.fixture
def tmp_root(tmp_path):
    """A throwaway repo root with the example config and field profile from the real repo."""
    from radar.config import REPO_ROOT

    (tmp_path / "config").mkdir()
    shutil.copy(
        REPO_ROOT / "config" / "config.example.toml", tmp_path / "config" / "config.example.toml"
    )
    shutil.copy(REPO_ROOT / "config" / "field_profile.md", tmp_path / "config" / "field_profile.md")
    return tmp_path


def test_builds_defaults_off(cfg):
    assert cfg.builds.enabled is False and cfg.builds.mode == "with_digest"
    assert "api.anthropic.com" in cfg.builds.allow_hosts and cfg.builds.timeout_min == 45
    assert cfg.builds_dir == Path(cfg.builds.work_dir).resolve()
    assert cfg.builds_dir.is_dir()


def test_builds_dir_defaults_to_user_app_data(tmp_path):
    from radar.config import default_builds_dir

    env = {"LOCALAPPDATA": str(tmp_path / "lad"), "XDG_DATA_HOME": str(tmp_path / "xdg")}
    assert default_builds_dir(True, env) == tmp_path / "lad" / "ResearchRadar" / "builds"
    assert default_builds_dir(False, env) == tmp_path / "xdg" / "research-radar" / "builds"
    home = default_builds_dir(False, {})
    assert home == Path.home() / ".local" / "share" / "research-radar" / "builds"
    assert default_builds_dir(True, {}).parts[-4:] == (
        "AppData",
        "Local",
        "ResearchRadar",
        "builds",
    )


def test_default_builds_dir_is_outside_the_data_folder(cfg, tmp_path):
    import os

    cfg.builds.work_dir = ""
    expected = (
        tmp_path / "localappdata" / "ResearchRadar" / "builds"
        if os.name == "nt"
        else tmp_path / "xdg-data" / "research-radar" / "builds"
    )
    assert cfg.builds_dir == expected.resolve() and cfg.builds_dir.is_dir()
    assert cfg.data_dir not in cfg.builds_dir.parents


def test_relative_work_dir_is_under_root(cfg):
    cfg.builds.work_dir = "custom-builds"
    assert cfg.builds_dir == (cfg.root / "custom-builds").resolve()
    cfg.builds_dir.rmdir()


def test_allowlist_has_download_hosts(cfg):
    for host in ("cas-bridge.xethub.hf.co", "release-assets.githubusercontent.com"):
        assert host in cfg.builds.allow_hosts


def test_git_email_must_be_github_noreply():
    f = settings.BY_ID["builds.git_email"]
    assert settings.coerce(f, "1234+me@users.noreply.github.com") == (
        "1234+me@users.noreply.github.com"
    )
    assert settings.coerce(f, "") == ""
    for bad in ("me@gmail.com", "me@users.noreply.github.com.evil.org", "not an email"):
        with pytest.raises(ValueError):
            settings.coerce(f, bad)
    with pytest.raises(ValueError, match="@users.noreply.github.com"):
        settings.coerce(f, "me@gmail.com")
    assert "github.com/settings/emails" in f.help


def test_settings_mention_build_data_location():
    assert "ResearchRadar" in settings.BY_ID["builds.enabled"].help


def test_builds_settings_validate(tmp_root):
    with pytest.raises(settings.SettingsError) as e:
        settings.save(tmp_root, {"builds.mode": "later", "builds.repo": "not a repo"})
    assert set(e.value.errors) == {"builds.mode", "builds.repo"}
    settings.save(tmp_root, {"builds.repo": "AbrarFaiyad/daily-builds", "builds.enabled": True})


def test_enabling_builds_requires_repo(tmp_root):
    with pytest.raises(settings.SettingsError) as e:
        settings.save(tmp_root, {"builds.enabled": True})
    assert "builds.repo" in e.value.errors
