"""Set up the daily-builds repo (workflow, publish tool, vendored scan) and store its token secret.

The one commit made here is the owner's, with the owner's configured GitHub noreply identity.
Every git call runs in a temporary clone with hooks, filters, signing and user config switched
off and with an allowlisted environment. Messages raised in ActionsError are fixed phrases:
never git or gh output, paths, or the token.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from radar.builds import actions
from radar.builds.actions import ActionsError

if TYPE_CHECKING:
    from radar.config import Config

GIT_TIMEOUT_S = 300
NOREPLY_SUFFIX = "@users.noreply.github.com"
SECRET_NAME = actions.SECRET_NAME
KEY_MIN = 20
SCOPE_MSG = "gh needs the workflow scope: run gh auth refresh -s workflow"
TEMPLATE = Path(__file__).parent / "template"
SCAN_SRC = Path(__file__).parent / "scan.py"
ENV_ALLOW = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
# Files kept in step with this version of Research Radar: (path in the repo, source here).
MANAGED = (
    (".github/workflows/build.yml", TEMPLATE / "build.yml"),
    ("tools/publish_build.py", TEMPLATE / "publish_build.py"),
    ("tools/scan.py", SCAN_SRC),
    ("tools/phases/plan.md", TEMPLATE / "tools" / "phases" / "plan.md"),
    ("tools/phases/implement.md", TEMPLATE / "tools" / "phases" / "implement.md"),
    ("tools/phases/review.md", TEMPLATE / "tools" / "phases" / "review.md"),
)
README = ("README.md", TEMPLATE / "README.md")  # written only when the repo has none

GitFn = Callable[..., "subprocess.CompletedProcess[bytes]"]


def run_git(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=GIT_TIMEOUT_S,
        check=False,
    )


def _credential_helper() -> str:
    exe = shutil.which("gh")
    if os.name == "nt":
        return f'!"{exe or "gh"}" auth git-credential'
    return f'!"{exe}" auth git-credential' if exe else "!gh auth git-credential"


def _prefix(cfg: Config, clone: Path) -> list[str]:
    b = cfg.builds
    pairs = [
        # The isolated config below cannot carry safe.directory, and a clone in a synced or
        # differently owned folder would otherwise fail every call with "dubious ownership".
        f"safe.directory={clone.as_posix()}",
        f"core.hooksPath={os.devnull}",
        "core.fsmonitor=false",
        "commit.gpgsign=false",
        "core.autocrlf=false",
        "core.quotepath=false",
        "filter.lfs.smudge=",
        "filter.lfs.clean=",
        "filter.lfs.process=",
        "filter.lfs.required=false",
        f"user.name={b.git_name}",
        f"user.email={b.git_email}",
        "credential.helper=",
        f"credential.helper={_credential_helper()}",
    ]
    return [arg for pair in pairs for arg in ("-c", pair)]


def _env(tmp: Path) -> dict[str, str]:
    """Git runs with an allowlisted environment: no tokens, askpass helpers or git settings."""
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_ALLOW}
    empty = tmp / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=str(empty),
        GIT_TERMINAL_PROMPT="0",
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(home),
        TZ="UTC",
    )
    gh_dir = actions.gh_env().get("GH_CONFIG_DIR")  # only the login folder, never a token
    if gh_dir:
        env["GH_CONFIG_DIR"] = gh_dir
    return env


def _scopes(text: str) -> set[str]:
    for line in text.splitlines():
        if "token scopes" in line.lower():
            _, _, tail = line.partition(":")
            return {s.strip().strip("'\"") for s in tail.split(",") if s.strip()}
    return set()


def _check_scope(gh: actions.GhFn) -> None:
    try:
        proc = gh(["auth", "status"])
    except (OSError, subprocess.TimeoutExpired):
        raise ActionsError("could not check the gh login") from None
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if proc.returncode != 0 and not _scopes(text):
        raise ActionsError("could not check the gh login")
    if "workflow" not in _scopes(text):
        raise ActionsError(SCOPE_MSG)


def _check_identity(cfg: Config) -> None:
    b = cfg.builds
    if not b.repo.strip():
        raise ActionsError("no target repo")
    if not b.git_name.strip() or not b.git_email.strip():
        raise ActionsError("commit identity not set")
    if not b.git_email.strip().lower().endswith(NOREPLY_SUFFIX):
        raise ActionsError("commit email is not a GitHub noreply address")


def _verify_commit(g, cfg: Config, allowed: set[str]) -> None:
    b = cfg.builds
    out = g(["log", "-1", "--format=%an%n%ae%n%cn%n%ce%n%ad%n%cd", "--date=raw"])
    lines = [x.strip() for x in out.decode("utf-8", "replace").split("\n")]
    if lines[:4] != [b.git_name, b.git_email] * 2:
        raise ActionsError("commit check failed")
    if len(lines) < 6 or not all(d.endswith(" +0000") for d in lines[4:6]):
        raise ActionsError("commit check failed")
    names = (
        g(["diff-tree", "--root", "--no-commit-id", "-r", "--name-only", "HEAD"])
        .decode("utf-8", "replace")
        .splitlines()
    )
    if not names or any(n not in allowed for n in names):
        raise ActionsError("commit check failed")


def init_repo(
    cfg: Config,
    git: GitFn = run_git,
    gh: actions.GhFn = actions.gh,
    remote_url: str | None = None,
) -> str:
    """Write or refresh the workflow files in the repo's main branch; returns the repo URL."""
    _check_identity(cfg)
    _check_scope(gh)
    b = cfg.builds
    expected = remote_url or f"https://github.com/{b.repo}.git"
    repo_url = f"https://github.com/{b.repo}"

    with tempfile.TemporaryDirectory(prefix="radar-git-") as td:
        tmp = Path(td)
        env = _env(tmp)
        clone = tmp / "repo"
        prefix = _prefix(cfg, clone)

        def g(args: list[str], cwd: Path = clone, fail: str = "git command failed") -> bytes:
            try:
                proc = git([*prefix, *args], cwd, env)
            except (OSError, subprocess.TimeoutExpired):
                raise ActionsError(fail) from None
            if proc.returncode != 0:
                raise ActionsError(fail)
            return proc.stdout

        def probe(args: list[str]) -> bool:
            try:
                return git([*prefix, *args], clone, env).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False

        # The clone is a temporary folder, so a failure at any step below leaves nothing behind.
        g(["clone", "--quiet", expected, str(clone)], cwd=tmp, fail="clone failed")
        shutil.rmtree(clone / ".git" / "hooks", ignore_errors=True)
        (clone / ".git" / "hooks").mkdir()
        if g(["remote", "get-url", "origin"]).decode("utf-8", "replace").strip() != expected:
            raise ActionsError("remote URL does not match")
        has_main = probe(["rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"])
        if has_main:
            g(["checkout", "--quiet", "-B", "main", "--track", "origin/main"])
        elif g(["for-each-ref", "--count=1", "refs/remotes/origin/"]).strip():
            raise ActionsError("the repository's default branch must be main")
        else:
            g(["symbolic-ref", "HEAD", "refs/heads/main"])

        try:
            existing = any((clone / rel).exists() for rel, _ in MANAGED)
            files = list(MANAGED)
            if not (clone / README[0]).exists():
                files.append(README)
            allowed = set()
            for rel, src in files:
                data = src.read_bytes()
                dest = clone / rel
                if dest.is_file() and dest.read_bytes() == data:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                allowed.add(rel)
        except OSError:
            raise ActionsError("could not write the workflow files") from None
        if not allowed:
            return repo_url  # already up to date

        g(["add", "--", *sorted(allowed)])
        stamp = f"{int(time.time())} +0000"
        message = "Update daily builds workflow" if existing else "Set up daily builds"
        try:
            proc = git(
                [*prefix, "commit", "--quiet", "--no-verify", "--no-gpg-sign", "-m", message],
                clone,
                {**env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ActionsError("commit failed") from None
        if proc.returncode != 0:
            raise ActionsError("commit failed")
        _verify_commit(g, cfg, allowed)
        g(
            ["push", "--quiet", *([] if has_main else ["-u"]), "origin", "HEAD:main"],
            fail="push failed",
        )
    return repo_url


def set_secret(cfg: Config, key: str, gh: actions.GhFn = actions.gh) -> None:
    """Store the Claude subscription token as the repo secret. It goes to gh on stdin only."""
    if not cfg.builds.repo.strip():
        raise ActionsError("no target repo")
    if len(key) < KEY_MIN or re.search(r"\s", key):
        raise ActionsError("that does not look like a Claude subscription token")
    try:
        proc = gh(["secret", "set", SECRET_NAME, "-R", cfg.builds.repo], input=key)
    except (OSError, subprocess.TimeoutExpired):
        raise ActionsError("could not store the secret") from None
    if proc.returncode != 0:
        raise ActionsError("could not store the secret")
