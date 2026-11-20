"""Publish a scanned build folder to the owner's public repo, from a dedicated clone.

Every git call runs with hooks, filters, signing and user config switched off and with a
scrubbed environment. Messages raised in PublishBlocked are fixed phrases: they are recorded
as the build's reason, so they never carry git output, file contents, paths or secrets.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from radar.builds.scan import scan_bytes
from radar.config import Config

if TYPE_CHECKING:
    from radar.builds.run import BuildRecord

GIT_TIMEOUT_S = 300
FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:_[a-z0-9]+(?:-[a-z0-9]+)*)+$")
WHAT_MAX = 120
LICENSE_MAX = 40
INDEX_TITLE = "# Daily builds"
INDEX_INTRO = (
    "These are AI-generated learning demos, built automatically by Claude Sonnet from "
    "Research Radar's daily digest. They are unreviewed and educational only, and they may "
    "contain bugs."
)
INDEX_HEADER = "| Date | Package | What it shows | License | Ran |"
INDEX_RULE = "| --- | --- | --- | --- | --- |"
LICENSE_RE = re.compile(r"^\W*licen[cs]e\W*[:\-]\s*(.+)$", re.IGNORECASE | re.MULTILINE)


class PublishBlocked(Exception):  # noqa: N818 - matched by class name in radar.builds.run
    """Publishing refused; the message is a fixed phrase."""


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
    gh = shutil.which("gh")
    if os.name == "nt":
        return f'!"{gh or "gh"}" auth git-credential'
    return f'!"{gh}" auth git-credential' if gh else "!gh auth git-credential"


def _prefix(cfg: Config) -> list[str]:
    b = cfg.builds
    pairs = [
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


ENV_ALLOW = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")


def _gh_config_dir() -> str | None:
    """Where the host's gh keeps its login, so the credential helper still finds it.

    Only this path is handed on, never a token. gh on Windows keeps the token in Credential
    Manager (found through the user session), elsewhere in hosts.yml inside this folder.
    """
    host = os.environ
    if host.get("GH_CONFIG_DIR"):
        cand = Path(host["GH_CONFIG_DIR"])
    elif os.name == "nt":
        cand = Path(host.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "GitHub CLI"
    elif host.get("XDG_CONFIG_HOME"):
        cand = Path(host["XDG_CONFIG_HOME"]) / "gh"
    else:
        cand = Path.home() / ".config" / "gh"
    return str(cand) if cand.is_dir() else None


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
    gh_dir = _gh_config_dir()
    if gh_dir:
        env["GH_CONFIG_DIR"] = gh_dir
    return env


def _is_link(path: str, st: os.stat_result) -> bool:
    isj = getattr(os.path, "isjunction", None)
    return stat.S_ISLNK(st.st_mode) or bool(isj and isj(path))


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy regular, non-hidden files byte for byte; refuse links."""
    dst.mkdir()
    with os.scandir(src) as it:
        entries = sorted(it, key=lambda e: e.name)
    for entry in entries:
        if entry.name.startswith("."):
            continue
        st = os.lstat(entry.path)
        if _is_link(entry.path, st):
            raise PublishBlocked("link in the build folder")
        if stat.S_ISDIR(st.st_mode):
            _copy_tree(Path(entry.path), dst / entry.name)
        elif stat.S_ISREG(st.st_mode):
            shutil.copyfile(entry.path, dst / entry.name)


def _cell(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())


def _what_it_shows(readme: str) -> str:
    lines = [ln.strip() for ln in readme.splitlines()]
    text = ""
    for ln in lines:
        if ln.startswith("#"):
            text = ln.lstrip("#").strip()
            break
    if not text:
        body = " ".join(ln for ln in lines if ln and not ln.startswith(">"))
        m = re.search(r"[.!?](?:\s|$)", body)
        text = body[: m.end()].strip() if m else body
    return _cell(text[:WHAT_MAX])


def _license(readme: str) -> str:
    m = LICENSE_RE.search(readme)
    return _cell(m.group(1).strip()[:LICENSE_MAX]) if m else "unknown"


def _row(record: BuildRecord, readme: str) -> str:
    cells = [
        _cell(str(record.day)),
        _cell(record.package),
        _what_it_shows(readme),
        _license(readme),
        "no" if record.status == "did_not_run" else "yes",
    ]
    return "| " + " | ".join(cells) + " |"


def _index(existing: str, new_row: str) -> str:
    rows = [
        ln
        for ln in existing.splitlines()
        if ln.startswith("|") and ln not in (INDEX_HEADER, INDEX_RULE)
    ]
    body = [INDEX_TITLE, "", INDEX_INTRO, "", INDEX_HEADER, INDEX_RULE, new_row, *rows]
    return "\n".join(body) + "\n"


def _verify_commit(g, cfg: Config, folder: str, markers: list[str], secrets: Sequence[str]) -> None:
    b = cfg.builds
    out = g(["log", "-1", "--format=%an%n%ae%n%cn%n%ce%n%ad%n%cd", "--date=raw"])
    lines = [x.strip() for x in out.decode("utf-8", "replace").split("\n")]
    if lines[:4] != [b.git_name, b.git_email] * 2:
        raise PublishBlocked("commit check failed")
    if len(lines) < 6 or not all(d.endswith(" +0000") for d in lines[4:6]):
        raise PublishBlocked("commit check failed")
    names = (
        g(["diff-tree", "--root", "--no-commit-id", "-r", "--name-only", "HEAD"])
        .decode("utf-8", "replace")
        .splitlines()
    )
    if not names or any(n != "README.md" and not n.startswith(folder + "/") for n in names):
        raise PublishBlocked("commit check failed")
    listing = g(["ls-tree", "-r", "-z", "HEAD", "--", folder, "README.md"])
    for entry in filter(None, listing.split(b"\0")):
        meta, _, path = entry.partition(b"\t")
        mode, kind, sha = meta.decode("utf-8", "replace").split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise PublishBlocked("commit check failed")
        data = g(["cat-file", "blob", sha])
        if scan_bytes(path.decode("utf-8", "replace"), data, markers, secrets):
            raise PublishBlocked("commit check failed")


def publish(
    cfg: Config,
    work_folder: Path,
    record: BuildRecord,
    markers: list[str],
    secrets: Sequence[str] = (),
    git: GitFn = run_git,
    remote_url: str | None = None,
) -> str:
    b = cfg.builds
    if not b.git_name.strip() or not b.git_email.strip():
        raise PublishBlocked("commit identity not set")
    folder = record.folder
    if not FOLDER_RE.match(folder):
        raise PublishBlocked("invalid folder name")
    expected = remote_url or f"https://github.com/{b.repo}.git"
    clone = cfg.builds_dir / "repo"
    prefix = _prefix(cfg)

    with tempfile.TemporaryDirectory(prefix="radar-git-") as td:
        env = _env(Path(td))

        def g(
            args: list[str],
            cwd: Path = clone,
            fail: str = "git command failed",
            extra_env: dict[str, str] | None = None,
        ) -> bytes:
            try:
                proc = git([*prefix, *args], cwd, {**env, **(extra_env or {})})
            except (OSError, subprocess.TimeoutExpired):
                raise PublishBlocked(fail) from None
            if proc.returncode != 0:
                raise PublishBlocked(fail)
            return proc.stdout

        def probe(args: list[str]) -> bool:
            try:
                return git([*prefix, *args], clone, env).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False

        if not (clone / ".git").is_dir():
            clone.parent.mkdir(parents=True, exist_ok=True)
            g(["clone", "--quiet", expected, str(clone)], cwd=clone.parent, fail="clone failed")
            shutil.rmtree(clone / ".git" / "hooks", ignore_errors=True)
            (clone / ".git" / "hooks").mkdir()

        if g(["remote", "get-url", "origin"]).decode("utf-8", "replace").strip() != expected:
            raise PublishBlocked("remote URL does not match")
        g(["fetch", "--quiet", "origin"], fail="fetch failed")

        has_head = probe(["rev-parse", "--verify", "--quiet", "HEAD"])
        if g(["status", "--porcelain"]).strip():
            raise PublishBlocked("clone has local changes")
        if not has_head and probe(["rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"]):
            # Unborn local clone, but origin already has main: adopt it.
            g(["checkout", "--quiet", "-B", "main", "--track", "origin/main"])
            has_head = True
        if has_head:
            branch = g(["rev-parse", "--abbrev-ref", "HEAD"]).decode().strip()
            if branch != "main" or not probe(["rev-parse", "--verify", "--quiet", "@{u}"]):
                raise PublishBlocked("clone is not tracking main")
            if g(["rev-list", "@{u}..HEAD"]).strip():
                raise PublishBlocked("clone has unpushed commits")
            g(["merge", "--ff-only", "--quiet", "@{u}"])
        else:
            g(["symbolic-ref", "HEAD", "refs/heads/main"])

        dest = clone / folder
        if dest.exists():
            raise PublishBlocked("folder already published")

        def rollback() -> None:
            def quiet(args: list[str]) -> None:
                try:
                    git([*prefix, *args], clone, env)
                except (OSError, subprocess.TimeoutExpired):
                    pass

            if has_head:
                quiet(["reset", "--hard", "--quiet", "@{u}"])
            else:
                quiet(["update-ref", "-d", "HEAD"])
                quiet(["read-tree", "--empty"])
                (clone / "README.md").unlink(missing_ok=True)
            shutil.rmtree(dest, ignore_errors=True)

        try:
            _copy_tree(work_folder, dest)
            work_readme = work_folder / "README.md"
            readme = ""
            if work_readme.is_file() and not work_readme.is_symlink():
                readme = work_readme.read_text(encoding="utf-8", errors="replace")
            index_path = clone / "README.md"
            existing = ""
            if index_path.is_file():
                existing = index_path.read_text(encoding="utf-8", errors="replace")
            index = _index(existing, _row(record, readme)).encode("utf-8")
            if scan_bytes("README.md", index, markers, secrets):
                raise PublishBlocked("index check failed")
            index_path.write_bytes(index)

            g(["add", "--", folder, "README.md"])
            stamp = f"{int(time.time())} +0000"
            g(
                ["commit", "--quiet", "--no-verify", "--no-gpg-sign", "-m", f"Build {folder}"],
                fail="commit failed",
                extra_env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
            )
            _verify_commit(g, cfg, folder, markers, secrets)
            push = ["push", "--quiet", *([] if has_head else ["-u"]), "origin", "HEAD:main"]
            g(push, fail="push failed")
        except PublishBlocked:
            rollback()
            raise
        except OSError:
            rollback()
            raise PublishBlocked("could not copy build folder") from None
        except Exception:  # noqa: BLE001 - any unexpected failure must not leave a local commit
            rollback()
            raise PublishBlocked("commit check failed") from None
    return f"https://github.com/{b.repo}/tree/main/{folder}"
