import shutil

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
    assert cfg.builds_dir == cfg.data_dir / "builds"


def test_builds_settings_validate(tmp_root):
    with pytest.raises(settings.SettingsError) as e:
        settings.save(tmp_root, {"builds.mode": "later", "builds.repo": "not a repo"})
    assert set(e.value.errors) == {"builds.mode", "builds.repo"}
    settings.save(tmp_root, {"builds.repo": "AbrarFaiyad/daily-builds", "builds.enabled": True})


def test_enabling_builds_requires_repo(tmp_root):
    with pytest.raises(settings.SettingsError) as e:
        settings.save(tmp_root, {"builds.enabled": True})
    assert "builds.repo" in e.value.errors
