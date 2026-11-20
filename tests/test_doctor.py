import httpx

from radar.doctor import Probes, run_checks


def _probes(tmp_path, **kw):
    pw_dir = tmp_path / "ms-playwright"
    (pw_dir / "chromium-1255").mkdir(parents=True, exist_ok=True)
    base = {
        "which": lambda exe: f"C:/bin/{exe}.exe",
        "http_get": lambda url: {"models": [{"name": "mimo-v2.6:latest"}]},
        "password": lambda user: "secret",
        "task_exists": lambda name: True,
        "playwright_dir": pw_dir,
    }
    base.update(kw)
    return Probes(**base)


def _by_name(checks):
    return {c.name: c for c in checks}


def test_all_green_when_everything_present(cfg, tmp_path):
    for d in (cfg.profile_dir, cfg.data_dir / "browser-profile-google"):
        d.mkdir(parents=True)
        (d / "Local State").write_text("{}")
    cfg.email.enabled, cfg.email.to = True, "me@example.org"
    checks = _by_name(run_checks(cfg, _probes(tmp_path)))
    assert all(c.ok for name, c in checks.items() if name != "config file"), {
        n: c.detail for n, c in checks.items() if not c.ok
    }


def test_missing_pieces_have_fixes(cfg, tmp_path):
    def down(url):
        raise httpx.ConnectError("refused")

    cfg.email.enabled, cfg.email.to = True, "me@example.org"
    checks = _by_name(
        run_checks(
            cfg, _probes(tmp_path, http_get=down, which=lambda e: None, password=lambda u: None)
        )
    )
    assert not checks["ollama"].ok and "Ollama" in checks["ollama"].fix
    assert not checks["codex on PATH"].ok and "@openai/codex" in checks["codex on PATH"].fix
    assert not checks["LinkedIn login"].ok and checks["LinkedIn login"].fix == "radar login"
    assert not checks["email password"].ok and "email-setup" in checks["email password"].fix


def test_model_not_pulled(cfg, tmp_path):
    checks = _by_name(run_checks(cfg, _probes(tmp_path, http_get=lambda u: {"models": []})))
    assert checks["ollama"].ok and not checks["local model"].ok
    assert checks["local model"].fix == "ollama pull mimo-v2.6"


def test_mask_hides_personal_details(tmp_path):
    from pathlib import Path

    from radar.doctor import mask

    root = tmp_path / "repo"
    text = (
        f"{root / 'data' / 'browser-profile'} | {Path.home() / 'AppData'} | someone.name@gmail.com"
    )
    out = mask(text, root)
    assert out.startswith(".") and "browser-profile" in out
    assert str(root) not in out and str(Path.home()) not in out
    assert "s***@gmail.com" in out and "someone.name" not in out


def test_model_tag_must_match(cfg, tmp_path):
    cfg.llm.model = "mimo-v2.6:9b-q8"
    checks = _by_name(run_checks(cfg, _probes(tmp_path)))
    assert not checks["local model"].ok  # only :latest is installed
    cfg.llm.model = "mimo-v2.6"
    assert _by_name(run_checks(cfg, _probes(tmp_path)))["local model"].ok


def _builds_probes(tmp_path, **kw):
    base = {
        "wsl_ok": lambda: True,
        "docker_ok": lambda: (True, ""),
        "image_exists": lambda cfg: True,
        "api_key": lambda: "sk-test",
        "repo_visibility": lambda repo: "PUBLIC",
    }
    base.update(kw)
    return _probes(tmp_path, **base)


def test_builds_checks_absent_when_disabled(cfg, tmp_path):
    checks = _by_name(run_checks(cfg, _builds_probes(tmp_path)))
    for name in ("WSL2", "Docker", "build image", "Anthropic API key", "daily-builds repo"):
        assert name not in checks


def test_builds_checks_present_with_actions(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("radar.doctor.IS_WINDOWS", True)
    cfg.builds.enabled, cfg.builds.repo = True, "someone/daily-builds"
    checks = _by_name(
        run_checks(
            cfg,
            _builds_probes(
                tmp_path,
                wsl_ok=lambda: False,
                docker_ok=lambda: (False, "docker not running"),
                image_exists=lambda c: False,
                api_key=lambda: None,
                repo_visibility=lambda r: "PRIVATE",
            ),
        )
    )
    assert checks["WSL2"].action == "https://learn.microsoft.com/windows/wsl/install"
    assert checks["Docker"].action == "https://www.docker.com/products/docker-desktop/"
    assert checks["Docker"].detail == "docker not running"
    assert checks["build image"].action == "build-image"
    assert checks["Anthropic API key"].action == "builds-key"
    assert checks["daily-builds repo"].action == "https://github.com/new"
    assert not any(
        checks[n].ok
        for n in ("WSL2", "Docker", "build image", "Anthropic API key", "daily-builds repo")
    )


def test_wsl_check_omitted_off_windows(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("radar.doctor.IS_WINDOWS", False)
    cfg.builds.enabled = True
    assert "WSL2" not in _by_name(run_checks(cfg, _builds_probes(tmp_path)))


def test_builds_checks_green_and_key_not_leaked(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("radar.doctor.IS_WINDOWS", True)
    cfg.builds.enabled, cfg.builds.repo = True, "someone/daily-builds"
    checks = _by_name(run_checks(cfg, _builds_probes(tmp_path)))
    for name in ("WSL2", "Docker", "build image", "Anthropic API key", "daily-builds repo"):
        assert checks[name].ok, name
    assert "sk-test" not in checks["Anthropic API key"].detail


def test_repo_check_needs_repo_set(cfg, tmp_path):
    cfg.builds.enabled, cfg.builds.repo = True, ""
    checks = _by_name(run_checks(cfg, _builds_probes(tmp_path)))
    assert not checks["daily-builds repo"].ok


def test_image_exists_uses_minimal_env_runner(cfg):
    from types import SimpleNamespace

    from radar.builds import sandbox

    seen = {}

    def run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        return SimpleNamespace(returncode=0)

    assert sandbox.image_exists(cfg, run)
    assert seen["argv"] == ["docker", "image", "inspect", cfg.builds.image]
    assert "ANTHROPIC_API_KEY" not in seen["env"]
