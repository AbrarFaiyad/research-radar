import os
import subprocess
from pathlib import Path

import pytest

from radar.builds import repo_init, scan
from radar.builds.actions import ActionsError
from radar.builds.repo_init import init_repo, run_git, set_secret

NAME = "Fake Owner"
EMAIL = "1234+fakeowner@users.noreply.github.com"
TEMPLATE = Path(scan.__file__).parent / "template"
KEY = "sk-ant-api03-" + "q" * 40
SCOPES = "'gist', 'read:org', 'repo', 'workflow'"


def sh(*args, cwd):
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def auth_status(scopes=SCOPES, code=0, stream="stdout"):
    text = (
        "github.com\n  - Logged in to github.com account someone (keyring)\n"
        f"  - Token scopes: {scopes}\n"
    )
    out, err = (text, "") if stream == "stdout" else ("", text)
    return subprocess.CompletedProcess([], code, stdout=out, stderr=err)


class FakeGh:
    def __init__(self, status=None, secret_code=0):
        self.status = status or auth_status()
        self.secret_code = secret_code
        self.calls = []

    def __call__(self, args, *, input=None, timeout=60):
        self.calls.append((list(args), input))
        if args[:2] == ["auth", "status"]:
            return self.status
        return subprocess.CompletedProcess(
            [], self.secret_code, stdout="", stderr="SECRET-stderr " + (input or "")
        )


@pytest.fixture
def origin(tmp_path):
    path = tmp_path / "origin.git"
    sh("init", "--bare", "-b", "main", str(path), cwd=tmp_path)
    return path


@pytest.fixture
def icfg(cfg, origin):
    cfg.builds.repo = "owner/daily"
    cfg.builds.git_name = NAME
    cfg.builds.git_email = EMAIL
    cfg.remote = origin.as_posix()
    return cfg


def go(cfg, **kw):
    kw.setdefault("gh", FakeGh())
    kw.setdefault("remote_url", cfg.remote)
    return init_repo(cfg, **kw)


def files(origin):
    return sh("ls-tree", "-r", "--name-only", "main", cwd=origin).split()


def show(origin, path):
    return subprocess.run(
        ["git", "-c", "safe.directory=*", "show", f"main:{path}"],
        cwd=origin,
        capture_output=True,
        check=True,
    ).stdout


def rejecting_hook(origin):
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    return hook


def test_init_writes_the_four_files_and_returns_the_url(icfg, origin):
    assert go(icfg) == "https://github.com/owner/daily"
    assert files(origin) == [
        ".github/workflows/build.yml",
        "README.md",
        "tools/publish_build.py",
        "tools/scan.py",
    ]
    assert show(origin, ".github/workflows/build.yml") == (TEMPLATE / "build.yml").read_bytes()
    assert show(origin, "tools/publish_build.py") == (TEMPLATE / "publish_build.py").read_bytes()
    assert show(origin, "README.md") == (TEMPLATE / "README.md").read_bytes()
    assert sh("log", "--format=%s", "main", cwd=origin).splitlines() == ["Set up daily builds"]


def test_vendored_scan_is_identical(icfg, origin):
    go(icfg)
    assert show(origin, "tools/scan.py") == Path(scan.__file__).read_bytes()


def test_commit_is_the_owners_with_utc_dates(icfg, origin, monkeypatch):
    monkeypatch.setenv("TZ", "PST8PDT")
    go(icfg)
    out = sh("log", "-1", "--format=%an%n%ae%n%cn%n%ce", "main", cwd=origin).splitlines()
    assert out == [NAME, EMAIL, NAME, EMAIL]
    dates = sh("log", "-1", "--format=%ad|%cd", "--date=raw", "main", cwd=origin)
    assert all(d.endswith(" +0000") for d in dates.strip().split("|"))


def test_existing_readme_is_kept(icfg, origin, tmp_path):
    other = tmp_path / "other"
    sh("clone", "--quiet", str(origin), str(other), cwd=tmp_path)
    (other / "README.md").write_text("# my own index\n", encoding="utf-8")
    sh("add", "README.md", cwd=other)
    sh("-c", "user.name=o", "-c", "user.email=o@x.y", "commit", "-m", "init", cwd=other)
    sh("push", "origin", "HEAD:main", cwd=other)
    go(icfg)
    assert show(origin, "README.md") == b"# my own index\n"
    assert "tools/scan.py" in files(origin)


def test_second_run_changes_nothing(icfg, origin):
    go(icfg)
    head = sh("rev-parse", "main", cwd=origin)
    go(icfg)
    assert sh("rev-parse", "main", cwd=origin) == head


def test_changed_files_are_updated_with_their_own_message(icfg, origin, tmp_path):
    go(icfg)
    other = tmp_path / "other"
    sh("clone", "--quiet", str(origin), str(other), cwd=tmp_path)
    (other / "tools" / "scan.py").write_text("# old version\n", encoding="utf-8")
    (other / "README.md").write_text("# edited by the owner\n", encoding="utf-8")
    sh("add", "-A", cwd=other)
    sh("-c", "user.name=o", "-c", "user.email=o@x.y", "commit", "-m", "old", cwd=other)
    sh("push", "origin", "HEAD:main", cwd=other)
    go(icfg)
    assert (
        sh("log", "-1", "--format=%s", "main", cwd=origin).strip() == "Update daily builds workflow"
    )
    assert show(origin, "tools/scan.py") == Path(scan.__file__).read_bytes()
    assert show(origin, "README.md") == b"# edited by the owner\n"
    assert sh("diff-tree", "--no-commit-id", "--name-only", "-r", "main", cwd=origin).split() == [
        "tools/scan.py"
    ]


@pytest.mark.parametrize(
    "scopes",
    ["'gist', 'repo'", "'repo', 'workflow-extra', 'read:workflow'", "", "'repo', 'workflow:x'"],
)
def test_refuses_without_the_workflow_scope(icfg, origin, scopes):
    with pytest.raises(ActionsError) as err:
        go(icfg, gh=FakeGh(auth_status(scopes)))
    assert str(err.value) == "gh needs the workflow scope: run gh auth refresh -s workflow"
    assert sh("for-each-ref", cwd=origin) == ""  # nothing was pushed


def test_scope_is_read_from_stderr_too(icfg, origin):
    go(icfg, gh=FakeGh(auth_status(stream="stderr")))
    assert "tools/scan.py" in files(origin)


def test_unreadable_gh_login_is_a_fixed_phrase(icfg):
    bad = subprocess.CompletedProcess([], 1, stdout="", stderr="SECRET-stderr")
    with pytest.raises(ActionsError, match="^could not check the gh login$"):
        go(icfg, gh=FakeGh(bad))

    def broken(args, *, input=None, timeout=60):
        raise OSError("gh missing at C:/secret/path")

    with pytest.raises(ActionsError, match="^could not check the gh login$"):
        go(icfg, gh=broken)


def test_identity_required(icfg):
    icfg.builds.git_email = ""
    with pytest.raises(ActionsError, match="commit identity not set"):
        go(icfg)
    icfg.builds.git_email, icfg.builds.git_name = EMAIL, " "
    with pytest.raises(ActionsError, match="commit identity not set"):
        go(icfg)


def test_email_must_be_github_noreply(icfg, origin):
    icfg.builds.git_email = "owner@gmail.com"
    with pytest.raises(ActionsError, match="commit email is not a GitHub noreply address"):
        go(icfg)
    assert sh("for-each-ref", cwd=origin) == ""


def test_wrong_remote_refused(icfg, origin):
    def spy(args, cwd, env):
        proc = run_git(args, cwd, env)
        if "get-url" in args:
            return subprocess.CompletedProcess(args, 0, stdout=b"https://github.com/x/y.git\n")
        return proc

    with pytest.raises(ActionsError, match="remote URL does not match"):
        go(icfg, git=spy)
    assert sh("for-each-ref", cwd=origin) == ""


def test_hooks_never_run(icfg, origin, tmp_path, monkeypatch):
    marker = tmp_path / "hook-ran"
    tpl = tmp_path / "tpl" / "hooks"
    tpl.mkdir(parents=True)
    for name in ("pre-commit", "commit-msg", "post-commit", "pre-push"):
        hook = tpl / name
        hook.write_text(f"#!/bin/sh\necho x > '{marker.as_posix()}'\n", encoding="utf-8")
        os.chmod(hook, 0o755)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tmp_path / "tpl"))
    go(icfg)
    assert not marker.exists()
    assert "tools/scan.py" in files(origin)


def test_git_env_is_allowlisted_and_hooks_are_off(icfg, monkeypatch):
    monkeypatch.setenv("SSH_ASKPASS", "evil")
    monkeypatch.setenv("GH_TOKEN", "ghp_secretsecret")
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    seen = []

    def spy(args, cwd, env):
        seen.append((list(args), dict(env)))
        return run_git(args, cwd, env)

    go(icfg, git=spy)
    assert seen
    for args, env in seen:
        assert not {"SSH_ASKPASS", "GH_TOKEN", "GIT_SSH_COMMAND"} & set(env)
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["TZ"] == "UTC"
        assert f"core.hooksPath={os.devnull}" in args
    trusted = [a for a in seen[0][0] if a.startswith("safe.directory=")]
    assert len(trusted) == 1  # only the temporary clone is trusted


def test_push_failure_leaves_nothing_behind(icfg, origin):
    rejecting_hook(origin)
    with pytest.raises(ActionsError, match="^push failed$"):
        go(icfg)
    assert sh("for-each-ref", cwd=origin) == ""


def test_error_messages_carry_no_git_output(icfg, origin):
    rejecting_hook(origin)
    with pytest.raises(ActionsError) as err:
        go(icfg)
    assert str(origin.name) not in str(err.value) and "rejected" not in str(err.value)


# ---- set_secret --------------------------------------------------------------------------------
def test_secret_key_goes_on_stdin_only(icfg):
    gh = FakeGh()
    set_secret(icfg, KEY, gh=gh)
    args, stdin = gh.calls[0]
    assert args == ["secret", "set", "ANTHROPIC_API_KEY", "-R", "owner/daily"]
    assert stdin == KEY
    assert not any(KEY in a for a in args)


@pytest.mark.parametrize("key", ["short", "x" * 19, "sk-ant " + "x" * 30, "x" * 30 + "\n", ""])
def test_secret_rejects_bad_keys_without_calling_gh(icfg, key):
    gh = FakeGh()
    with pytest.raises(ActionsError, match="does not look like an API key") as err:
        set_secret(icfg, key, gh=gh)
    assert gh.calls == [] and (not key or key not in str(err.value))


def test_secret_accepts_the_minimum_length(icfg):
    gh = FakeGh()
    set_secret(icfg, "x" * 20, gh=gh)
    assert len(gh.calls) == 1


def test_secret_failure_is_a_fixed_phrase(icfg):
    with pytest.raises(ActionsError) as err:
        set_secret(icfg, KEY, gh=FakeGh(secret_code=1))
    assert str(err.value) == "could not store the secret"
    assert KEY not in str(err.value) and "SECRET-stderr" not in str(err.value)

    def broken(args, *, input=None, timeout=60):
        raise subprocess.TimeoutExpired(["gh", *args], 60)

    with pytest.raises(ActionsError, match="^could not store the secret$") as err:
        set_secret(icfg, KEY, gh=broken)
    assert KEY not in repr(err.value) and err.value.__cause__ is None


def test_secret_needs_a_repo(icfg):
    icfg.builds.repo = ""
    with pytest.raises(ActionsError, match="no target repo"):
        set_secret(icfg, KEY, gh=FakeGh())


def test_module_constants_point_at_the_template_files():
    assert repo_init.SCAN_SRC == Path(scan.__file__)
    for _, src in (*repo_init.MANAGED, repo_init.README):
        assert src.is_file()
