import os
import subprocess
from types import SimpleNamespace

import pytest

from radar.builds import publish as publish_mod
from radar.builds.publish import PublishBlocked, publish, run_git

NAME = "Fake Owner"
EMAIL = "1234+fakeowner@users.noreply.github.com"
MARKERS = ["zed-private-marker"]


def sh(*args, cwd):
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def rejecting_hook(origin):
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    return hook


@pytest.fixture
def origin(tmp_path):
    path = tmp_path / "origin.git"
    sh("init", "--bare", "-b", "main", str(path), cwd=tmp_path)
    return path


@pytest.fixture
def pcfg(cfg, origin):
    cfg.builds.repo = "owner/daily"
    cfg.builds.git_name = NAME
    cfg.builds.git_email = EMAIL
    cfg.remote = origin.as_posix()
    return cfg


def rec(day="2026-11-21", package="foo", status="published", folder=None):
    folder = folder or f"{day}_{package}_demo"
    return SimpleNamespace(day=day, package=package, folder=folder, status=status)


def work(tmp_path, name="w", readme="# Shows a thing\nLicense: MIT\n", extra=None):
    w = tmp_path / name
    w.mkdir()
    (w / "README.md").write_text(readme, encoding="utf-8")
    (w / "demo.py").write_bytes(b"print('hi')\r\n")
    for k, v in (extra or {}).items():
        p = w / k
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(v)
    return w


def go(cfg, w, r, **kw):
    kw.setdefault("remote_url", cfg.remote)
    return publish(cfg, w, r, MARKERS, ["sk-ant-secret"], **kw)


def clone_of(cfg):
    return cfg.builds_dir / "repo"


def index_rows(origin):
    return [ln for ln in sh("show", "main:README.md", cwd=origin).splitlines() if "| 2026-" in ln]


def test_first_publish_creates_index_and_folder(pcfg, tmp_path, origin):
    url = go(pcfg, work(tmp_path), rec())
    assert url == "https://github.com/owner/daily/tree/main/2026-11-21_foo_demo"
    assert sh("ls-tree", "-r", "--name-only", "main", cwd=origin).split() == [
        "2026-11-21_foo_demo/README.md",
        "2026-11-21_foo_demo/demo.py",
        "README.md",
    ]
    index = sh("show", "main:README.md", cwd=origin)
    assert index.startswith("# Daily builds")
    assert "unreviewed" in index
    assert "| Date | Package | What it shows | License | Ran |" in index
    assert "| 2026-11-21 | foo | Shows a thing | MIT | yes |" in index
    blob = subprocess.run(
        ["git", "-c", "safe.directory=*", "show", "main:2026-11-21_foo_demo/demo.py"],
        cwd=origin,
        capture_output=True,
        check=True,
    ).stdout
    assert blob == b"print('hi')\r\n"


def test_second_publish_newest_first(pcfg, tmp_path, origin):
    go(pcfg, work(tmp_path, "a"), rec())
    go(pcfg, work(tmp_path, "b"), rec("2026-11-22", "bar", "did_not_run"))
    rows = index_rows(origin)
    assert rows[0].startswith("| 2026-11-22 | bar |")
    assert rows[0].endswith("| no |")
    assert rows[1].startswith("| 2026-11-21 | foo |")


def test_existing_folder_blocked(pcfg, tmp_path):
    go(pcfg, work(tmp_path, "a"), rec())
    with pytest.raises(PublishBlocked, match="folder already published"):
        go(pcfg, work(tmp_path, "b"), rec())


def test_dirty_clone_blocked(pcfg, tmp_path):
    go(pcfg, work(tmp_path, "a"), rec())
    (clone_of(pcfg) / "stray.txt").write_text("x", encoding="utf-8")
    with pytest.raises(PublishBlocked, match="clone has local changes"):
        go(pcfg, work(tmp_path, "b"), rec("2026-11-22"))


def test_wrong_remote_blocked(pcfg, tmp_path):
    go(pcfg, work(tmp_path, "a"), rec())
    with pytest.raises(PublishBlocked, match="remote URL does not match"):
        go(pcfg, work(tmp_path, "b"), rec("2026-11-22"), remote_url="https://github.com/x/y.git")


def test_unpushed_commit_blocked(pcfg, tmp_path):
    go(pcfg, work(tmp_path, "a"), rec())
    c = clone_of(pcfg)
    (c / "x.txt").write_text("x", encoding="utf-8")
    sh("add", "x.txt", cwd=c)
    sh("-c", "user.name=a", "-c", "user.email=a@b.c", "commit", "-m", "x", cwd=c)
    with pytest.raises(PublishBlocked, match="unpushed"):
        go(pcfg, work(tmp_path, "b"), rec("2026-11-22"))


def test_identity_required(pcfg, tmp_path):
    pcfg.builds.git_email = ""
    with pytest.raises(PublishBlocked, match="commit identity not set"):
        go(pcfg, work(tmp_path), rec())


def test_hook_never_runs_and_identity_is_configured(pcfg, tmp_path, origin):
    go(pcfg, work(tmp_path, "a"), rec())
    marker = tmp_path / "hook-ran"
    hook = clone_of(pcfg) / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho x > '{marker.as_posix()}'\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    go(pcfg, work(tmp_path, "b"), rec("2026-11-22"))
    assert not marker.exists()
    out = sh("log", "-1", "--format=%an%n%ae%n%cn%n%ce", "main", cwd=origin).splitlines()
    assert out == [NAME, EMAIL, NAME, EMAIL]


def test_index_row_escapes_pipe_and_newline(pcfg, tmp_path, origin):
    go(pcfg, work(tmp_path, readme="# a | b\nsecond\n"), rec())
    row = index_rows(origin)[0]
    assert "a \\| b" in row
    assert row.count(" | ") == 4


def test_first_sentence_used_without_heading(pcfg, tmp_path, origin):
    w = work(tmp_path, readme="> **DID NOT RUN.** banner\n\nFits a model. More text.\n")
    go(pcfg, w, rec())
    assert "| Fits a model. |" in index_rows(origin)[0]


def test_hidden_files_not_copied(pcfg, tmp_path, origin):
    w = work(tmp_path, extra={".claude/x.txt": b"x", ".hidden": b"y", "sub/ok.txt": b"z"})
    go(pcfg, w, rec())
    names = sh("ls-tree", "-r", "--name-only", "main", cwd=origin).split()
    assert "2026-11-21_foo_demo/sub/ok.txt" in names
    assert not any("/." in n for n in names)


def test_link_blocked(pcfg, tmp_path):
    w = work(tmp_path)
    try:
        os.symlink(w / "demo.py", w / "ln.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(PublishBlocked, match="link in the build folder"):
        go(pcfg, w, rec())
    assert not (clone_of(pcfg) / "2026-11-21_foo_demo").exists()


def test_leak_in_blob_blocked_and_rolled_back(pcfg, tmp_path, origin):
    go(pcfg, work(tmp_path, "a"), rec())
    w = work(tmp_path, "b", extra={"notes.txt": b"key zed-private-marker here"})
    with pytest.raises(PublishBlocked, match="commit check failed"):
        go(pcfg, w, rec("2026-11-22"))
    c = clone_of(pcfg)
    assert sh("status", "--porcelain", cwd=c) == ""
    assert sh("rev-parse", "HEAD", cwd=c) == sh("rev-parse", "main", cwd=origin)
    assert not (c / "2026-11-22_foo_demo").exists()


def test_push_failure_rolled_back(pcfg, tmp_path, origin):
    go(pcfg, work(tmp_path, "a"), rec())
    rejecting_hook(origin)
    with pytest.raises(PublishBlocked, match="push failed"):
        go(pcfg, work(tmp_path, "b"), rec("2026-11-22"))
    c = clone_of(pcfg)
    assert sh("status", "--porcelain", cwd=c) == ""
    assert sh("rev-parse", "HEAD", cwd=c) == sh("rev-parse", "main", cwd=origin)


def test_first_publish_failure_leaves_clean_empty_clone(pcfg, tmp_path, origin):
    hook = rejecting_hook(origin)
    with pytest.raises(PublishBlocked, match="push failed"):
        go(pcfg, work(tmp_path), rec())
    c = clone_of(pcfg)
    assert sh("status", "--porcelain", cwd=c) == ""
    assert not (c / "README.md").exists()
    hook.unlink()
    assert go(pcfg, work(tmp_path, "again"), rec()).endswith("2026-11-21_foo_demo")


def test_bad_folder_name_blocked(pcfg, tmp_path):
    with pytest.raises(PublishBlocked, match="invalid folder name"):
        go(pcfg, work(tmp_path), rec(folder="../evil"))


def test_commit_dates_are_utc(pcfg, tmp_path, origin, monkeypatch):
    monkeypatch.setenv("TZ", "PST8PDT")
    go(pcfg, work(tmp_path), rec())
    out = sh("log", "-1", "--format=%ad|%cd", "--date=raw", "main", cwd=origin)
    assert all(d.endswith(" +0000") for d in out.strip().split("|"))


def test_unexpected_error_after_commit_rolls_back(pcfg, tmp_path, origin, monkeypatch):
    go(pcfg, work(tmp_path, "a"), rec())
    real = publish_mod.scan_bytes
    calls = []

    def flaky(*args, **kw):
        calls.append(1)
        if len(calls) == 2:  # first call is the index, second is a committed blob
            raise ValueError("boom")
        return real(*args, **kw)

    monkeypatch.setattr(publish_mod, "scan_bytes", flaky)
    with pytest.raises(PublishBlocked, match="commit check failed"):
        go(pcfg, work(tmp_path, "b"), rec("2026-11-22"))
    monkeypatch.setattr(publish_mod, "scan_bytes", real)
    c = clone_of(pcfg)
    assert sh("rev-parse", "HEAD", cwd=c) == sh("rev-parse", "main", cwd=origin)
    assert go(pcfg, work(tmp_path, "c"), rec("2026-11-22")).endswith("2026-11-22_foo_demo")


def test_unborn_clone_adopts_origin_main(pcfg, tmp_path, origin):
    hook = rejecting_hook(origin)
    with pytest.raises(PublishBlocked, match="push failed"):
        go(pcfg, work(tmp_path, "a"), rec())
    hook.unlink()
    other = tmp_path / "other"
    sh("clone", "--quiet", str(origin), str(other), cwd=tmp_path)
    (other / "README.md").write_text("hello\n", encoding="utf-8")
    sh("add", "README.md", cwd=other)
    sh("-c", "user.name=o", "-c", "user.email=o@x.y", "commit", "-m", "init", cwd=other)
    sh("push", "origin", "HEAD:main", cwd=other)
    assert go(pcfg, work(tmp_path, "b"), rec()).endswith("2026-11-21_foo_demo")
    assert "2026-11-21_foo_demo/demo.py" in sh("ls-tree", "-r", "--name-only", "main", cwd=origin)


def test_git_env_is_allowlisted(pcfg, tmp_path, monkeypatch):
    monkeypatch.setenv("SSH_ASKPASS", "evil")
    monkeypatch.setenv("GH_TOKEN", "ghp_secretsecret")
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    seen = []

    def spy(args, cwd, env):
        seen.append(dict(env))
        return run_git(args, cwd, env)

    go(pcfg, work(tmp_path), rec(), git=spy)
    assert seen
    for env in seen:
        assert not {"SSH_ASKPASS", "GH_TOKEN", "GIT_SSH_COMMAND"} & set(env)
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["TZ"] == "UTC"


def test_email_must_be_github_noreply(pcfg, tmp_path):
    pcfg.builds.git_email = "owner@gmail.com"
    with pytest.raises(PublishBlocked, match="commit email is not a GitHub noreply address"):
        go(pcfg, work(tmp_path), rec())


def test_clone_owned_by_someone_else_still_publishes(pcfg, tmp_path, origin):
    """Box Drive and similar folders look foreign-owned to git; the clone is trusted explicitly."""
    # The test origin is a local folder, so it looks foreign too; trust it (and only it) in a
    # global config file. The clone must be trusted by publish itself.
    trust = tmp_path / "trust-origin.gitconfig"
    trust.write_text(f"[safe]\n\tdirectory = {origin.as_posix()}\n", encoding="utf-8")

    def foreign(args, cwd, env):
        env = {**env, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1", "GIT_CONFIG_GLOBAL": str(trust)}
        return run_git(args, cwd, env)

    go(pcfg, work(tmp_path, "a"), rec(), git=foreign)
    go(pcfg, work(tmp_path, "b"), rec("2026-11-22"), git=foreign)
    assert len(index_rows(origin)) == 2


def test_prefix_trusts_only_the_clone(pcfg):
    prefix = publish_mod._prefix(pcfg)
    trusted = [prefix[i + 1] for i, a in enumerate(prefix) if a == "-c"]
    safe = [x for x in trusted if x.startswith("safe.directory=")]
    assert safe == [f"safe.directory={(pcfg.builds_dir / 'repo').as_posix()}"]
