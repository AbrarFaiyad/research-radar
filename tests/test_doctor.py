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
        "repo_visibility": lambda repo: "PUBLIC",
        "gh_scopes": lambda: {"repo", "workflow"},
        "workflow_installed": lambda repo: True,
        "secret_names": lambda repo: {"CLAUDE_CODE_OAUTH_TOKEN"},
    }
    base.update(kw)
    return _probes(tmp_path, **base)


def test_builds_checks_absent_when_disabled(cfg, tmp_path):
    checks = _by_name(run_checks(cfg, _builds_probes(tmp_path)))
    assert "daily-builds repo" not in checks


def test_no_docker_checks_remain(cfg, tmp_path):
    cfg.builds.enabled, cfg.builds.repo = True, "someone/daily-builds"
    checks = _by_name(run_checks(cfg, _builds_probes(tmp_path)))
    for name in ("WSL2", "Docker", "build image"):
        assert name not in checks


def test_repo_check_has_action_and_goes_green(cfg, tmp_path):
    cfg.builds.enabled, cfg.builds.repo = True, "someone/daily-builds"
    private = _by_name(
        run_checks(cfg, _builds_probes(tmp_path, repo_visibility=lambda r: "PRIVATE"))
    )
    assert not private["daily-builds repo"].ok
    assert private["daily-builds repo"].action == "https://github.com/new"
    assert _by_name(run_checks(cfg, _builds_probes(tmp_path)))["daily-builds repo"].ok


def test_repo_check_needs_repo_set(cfg, tmp_path):
    cfg.builds.enabled, cfg.builds.repo = True, ""
    checks = _by_name(run_checks(cfg, _builds_probes(tmp_path)))
    assert not checks["daily-builds repo"].ok


def _on(cfg):
    cfg.builds.enabled, cfg.builds.repo = True, "someone/daily-builds"


def test_builds_checks_all_green(cfg, tmp_path):
    _on(cfg)
    checks = _by_name(run_checks(cfg, _builds_probes(tmp_path)))
    for name in (
        "gh signed in with workflow scope",
        "daily-builds repo",
        "workflow installed",
        "Claude subscription token secret",
    ):
        assert checks[name].ok, name


def test_scope_check_fails_without_workflow_scope(cfg, tmp_path):
    _on(cfg)
    c = _by_name(run_checks(cfg, _builds_probes(tmp_path, gh_scopes=lambda: {"repo"}))).get(
        "gh signed in with workflow scope"
    )
    assert c and not c.ok and c.action.startswith("https://") and "workflow" in c.fix
    c = _by_name(run_checks(cfg, _builds_probes(tmp_path, gh_scopes=lambda: None)))[
        "gh signed in with workflow scope"
    ]
    assert not c.ok


def test_workflow_check_offers_repo_setup(cfg, tmp_path):
    _on(cfg)
    c = _by_name(run_checks(cfg, _builds_probes(tmp_path, workflow_installed=lambda r: False)))[
        "workflow installed"
    ]
    assert not c.ok and c.action == "build-init-repo"


def test_key_secret_check_offers_add_key(cfg, tmp_path):
    _on(cfg)
    for names in (set(), {"OTHER"}, None):
        c = _by_name(run_checks(cfg, _builds_probes(tmp_path, secret_names=lambda r, n=names: n)))[
            "Claude subscription token secret"
        ]
        assert not c.ok and c.action == "builds-key"


def test_probes_receive_the_configured_repo(cfg, tmp_path):
    _on(cfg)
    seen = []
    run_checks(
        cfg,
        _builds_probes(
            tmp_path,
            workflow_installed=lambda r: seen.append(("wf", r)) or True,
            secret_names=lambda r: seen.append(("sec", r)) or {"CLAUDE_CODE_OAUTH_TOKEN"},
        ),
    )
    assert seen == [("wf", "someone/daily-builds"), ("sec", "someone/daily-builds")]


def test_builds_details_are_masked(cfg, tmp_path):
    cfg.builds.enabled, cfg.builds.repo = True, "me@gmail.com"
    checks = _by_name(run_checks(cfg, _builds_probes(tmp_path)))
    assert "me@gmail.com" not in checks["daily-builds repo"].detail
    assert "m***@gmail.com" in checks["daily-builds repo"].detail


def test_default_probes_use_gh_with_minimal_env(monkeypatch):
    from types import SimpleNamespace

    from radar import doctor
    from radar.builds import actions

    calls = []

    def fake_gh(args, **kw):
        calls.append(args)
        if args[:2] == ["auth", "status"]:
            return SimpleNamespace(
                returncode=0, stdout="  - Token scopes: 'repo', 'workflow'", stderr=""
            )
        if args[0] == "secret":
            return SimpleNamespace(
                returncode=0, stdout='[{"name": "CLAUDE_CODE_OAUTH_TOKEN"}]', stderr=""
            )
        if args[0] == "repo":
            return SimpleNamespace(returncode=0, stdout='{"visibility": "PUBLIC"}', stderr="")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(actions, "gh", fake_gh)
    assert doctor._gh_scopes() == {"repo", "workflow"}
    assert doctor._secret_names("o/r") == {"CLAUDE_CODE_OAUTH_TOKEN"}
    assert doctor._repo_visibility("o/r") == "PUBLIC"
    assert doctor._workflow_installed("o/r") is True
    assert ["api", "repos/o/r/contents/.github/workflows/build.yml"] in calls


def test_default_probes_fail_closed(monkeypatch):
    from radar import doctor
    from radar.builds import actions

    def boom(args, **kw):
        raise OSError("no gh")

    monkeypatch.setattr(actions, "gh", boom)
    assert doctor._gh_scopes() is None
    assert doctor._secret_names("o/r") is None
    assert doctor._repo_visibility("o/r") is None
    assert doctor._workflow_installed("o/r") is False
