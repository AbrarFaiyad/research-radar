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
