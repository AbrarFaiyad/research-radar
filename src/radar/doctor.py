"""`radar doctor`: check every prerequisite and say how to fix what is missing."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from .config import Config
from .llm import model_tag

TASK_NAME = "ResearchRadarDaily"
IS_WINDOWS = os.name == "nt"
DOCS_URL = "https://github.com/AbrarFaiyad/research-radar#daily-builds"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = True
    action: str = ""  # panel fix: a job action, "email-password", "schedule", or a URL

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Probes:
    """Side-effecting lookups, injectable for tests."""

    which: Callable[[str], str | None] = shutil.which
    http_get: Callable[[str], dict] = lambda url: httpx.get(url, timeout=4).json()
    password: Callable[[str], str | None] = lambda user: _keyring_password(user)
    task_exists: Callable[[str], bool] = lambda name: _task_exists(name)
    repo_visibility: Callable[[str], str | None] = lambda repo: _repo_visibility(repo)
    gh_scopes: Callable[[], set[str] | None] = lambda: _gh_scopes()
    workflow_installed: Callable[[str], bool] = lambda repo: _workflow_installed(repo)
    secret_names: Callable[[str], set[str] | None] = lambda repo: _secret_names(repo)
    playwright_dir: Path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"


def _keyring_password(user: str) -> str | None:
    from .mailer import get_password

    return get_password(user)


def _task_exists(name: str) -> bool:
    if os.name != "nt":
        return False
    proc = subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True, check=False)
    return proc.returncode == 0


def _gh_json(args: list[str]) -> object | None:
    """Run gh with the minimal environment; None on any failure."""
    from .builds import actions

    try:
        proc = actions.gh(args, timeout=30)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


def _repo_visibility(repo: str) -> str | None:
    data = _gh_json(["repo", "view", repo, "--json", "visibility"])
    return data.get("visibility") if isinstance(data, dict) else None


def _gh_scopes() -> set[str] | None:
    from .builds import actions, repo_init

    try:
        proc = actions.gh(["auth", "status"], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    text = "\n".join([proc.stdout or "", proc.stderr or ""])
    scopes = repo_init._scopes(text)
    if proc.returncode != 0 and not scopes:
        return None
    return scopes


def _workflow_installed(repo: str) -> bool:
    from .builds import actions

    try:
        proc = actions.gh(["api", f"repos/{repo}/contents/.github/workflows/build.yml"], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _secret_names(repo: str) -> set[str] | None:
    from .builds import actions

    return actions.secret_names(repo)


def _dir_has_files(path: Path) -> bool:
    return path.is_dir() and any(path.iterdir())


def run_checks(cfg: Config, probes: Probes | None = None) -> list[Check]:
    p = probes or Probes()
    out: list[Check] = []
    cfg_file = cfg.root / "config" / "config.toml"
    out.append(
        Check(
            "config file",
            cfg_file.exists(),
            str(cfg_file) if cfg_file.exists() else "using config.example.toml defaults",
            "Copy-Item config/config.example.toml config/config.toml",
            required=False,
            action="setup-config",
        )
    )
    out.append(
        Check(
            "field profile",
            cfg.field_profile.exists(),
            str(cfg.field_profile),
            "create config/field_profile.md describing your field",
            action="onboard",
        )
    )

    try:
        tags = p.http_get(f"{cfg.llm.host.rstrip('/')}/api/tags")
        names = {model_tag(m["name"]) for m in tags.get("models", [])}
        out.append(Check("ollama", True, f"reachable at {cfg.llm.host}"))
        has = model_tag(cfg.llm.model) in names
        out.append(
            Check(
                "local model",
                has,
                cfg.llm.model if has else f"{cfg.llm.model} not pulled",
                f"ollama pull {cfg.llm.model}",
                action="setup-model",
            )
        )
    except (httpx.HTTPError, ValueError, KeyError, OSError):
        out.append(
            Check(
                "ollama",
                False,
                f"not reachable at {cfg.llm.host}",
                "start the Ollama app (winget install Ollama.Ollama)",
                action="https://ollama.com/download",
            )
        )

    for exe, fix, action in (
        (cfg.digest.codex_command, "npm install -g @openai/codex, then codex login", "setup-codex"),
        (
            cfg.browser.mcp_command,
            "winget install OpenJS.NodeJS.LTS",
            "https://nodejs.org/en/download",
        ),
    ):
        path = p.which(exe)
        out.append(Check(f"{exe} on PATH", bool(path), path or "not found", fix, action=action))

    chromium = p.playwright_dir.is_dir() and any(p.playwright_dir.glob("chromium-*"))
    out.append(
        Check(
            "Playwright Chromium",
            chromium,
            str(p.playwright_dir),
            "npx -y playwright@1.65.0-alpha-2026-10-30 install chromium",
            action="setup-chromium",
        )
    )
    out.append(
        Check(
            "LinkedIn login",
            _dir_has_files(cfg.profile_dir),
            str(cfg.profile_dir),
            "radar login",
            action="login",
        )
    )
    if cfg.notebooklm.enabled:
        google = cfg.data_dir / "browser-profile-google"
        out.append(
            Check(
                "NotebookLM login",
                _dir_has_files(google),
                str(google),
                "radar notebooklm-login",
                action="notebooklm-login",
            )
        )
    if cfg.email.enabled:
        user = cfg.email.sender or cfg.email.to
        has_pw = bool(user) and not user.endswith("example.com") and bool(p.password(user))
        out.append(
            Check(
                "email password",
                has_pw,
                user or "no recipient set",
                "set [email] to in config.toml, then radar email-setup",
                action="email-password",
            )
        )
    if cfg.builds.enabled:
        out += _builds_checks(cfg, p)
    if cfg.podcasts.enabled:
        out.append(_podcasts_check(cfg, p))
    out.append(
        Check(
            "daily schedule",
            p.task_exists(TASK_NAME),
            TASK_NAME,
            "powershell -ExecutionPolicy Bypass -File scripts\\install_schedule.ps1 -Time 07:00",
            required=False,
            action="schedule",
        )
    )
    for c in out:
        c.detail = mask(c.detail, cfg.root)
    return out


def _builds_checks(cfg: Config, p: Probes) -> list[Check]:
    repo = cfg.builds.repo
    scopes = p.gh_scopes()
    out = [
        Check(
            "gh signed in with workflow scope",
            scopes is not None and "workflow" in scopes,
            "workflow scope present"
            if scopes is not None and "workflow" in scopes
            else ("signed in without it" if scopes is not None else "gh not signed in"),
            "gh auth login, then gh auth refresh -s workflow",
            action=DOCS_URL,
        )
    ]
    visibility = p.repo_visibility(repo) if repo else None
    out.append(
        Check(
            "daily-builds repo",
            visibility == "PUBLIC",
            repo or "no repo set",
            "create a public GitHub repo and set [builds] repo = owner/name",
            action="https://github.com/new",
        )
    )
    out.append(
        Check(
            "workflow installed",
            bool(repo) and p.workflow_installed(repo),
            ".github/workflows/build.yml",
            "press Set up repo in the panel (radar build --init-repo)",
            action="build-init-repo",
        )
    )
    names = p.secret_names(repo) if repo else None
    out.append(
        Check(
            "Claude subscription token secret",
            names is not None and "CLAUDE_CODE_OAUTH_TOKEN" in names,
            "CLAUDE_CODE_OAUTH_TOKEN stored in the repo",
            "run claude setup-token on your PC and paste the token on the Setup tab",
            action="builds-key",
        )
    )
    return out


def _podcasts_check(cfg: Config, p: Probes) -> Check:
    repo = cfg.podcasts.repo
    visibility = p.repo_visibility(repo) if repo else None
    return Check(
        "daily-podcasts repo",
        visibility == "PUBLIC",
        repo or "no repo set",
        "set [podcasts] repo = owner/name, then press Set up podcasts repo in the panel",
        action="podcast-init-repo",
    )


_EMAIL = re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def mask(text: str, root: Path) -> str:
    """Hide personal details in check output: repo paths become relative, the home folder
    becomes ~, and email addresses keep only their first letter and domain."""
    for base, label in ((str(root), "."), (str(Path.home()), "~")):
        text = text.replace(base, label)
    return _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
