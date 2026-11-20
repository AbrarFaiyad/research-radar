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
    wsl_ok: Callable[[], bool] = lambda: _wsl_ok()
    docker_ok: Callable[[], tuple[bool, str]] = lambda: _docker_ok()
    image_exists: Callable[[Config], bool] = lambda cfg: _image_exists(cfg)
    api_key: Callable[[], str | None] = lambda: _api_key()
    repo_visibility: Callable[[str], str | None] = lambda repo: _repo_visibility(repo)
    playwright_dir: Path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"


def _keyring_password(user: str) -> str | None:
    from .mailer import get_password

    return get_password(user)


def _task_exists(name: str) -> bool:
    if os.name != "nt":
        return False
    proc = subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True, check=False)
    return proc.returncode == 0


def _wsl_ok() -> bool:
    try:
        return subprocess.run(["wsl", "--status"], capture_output=True, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _docker_ok() -> tuple[bool, str]:
    from .builds.sandbox import docker_ready

    return docker_ready()


def _image_exists(cfg: Config) -> bool:
    from .builds.sandbox import image_exists

    return image_exists(cfg)


def _api_key() -> str | None:
    import keyring.errors

    from .builds.keys import get_key

    try:
        return get_key()
    except keyring.errors.KeyringError:  # no usable backend: same as no key
        return None


def _repo_visibility(repo: str) -> str | None:
    try:
        proc = subprocess.run(
            ["gh", "repo", "view", repo, "--json", "visibility"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout).get("visibility")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


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
    out: list[Check] = []
    if IS_WINDOWS:
        wsl = p.wsl_ok()
        out.append(
            Check(
                "WSL2",
                wsl,
                "ready" if wsl else "not available",
                "wsl --install, then restart Windows",
                action="https://learn.microsoft.com/windows/wsl/install",
            )
        )
    docker, docker_detail = p.docker_ok()
    out.append(
        Check(
            "Docker",
            docker,
            "ready" if docker else docker_detail,
            "install and start Docker Desktop (WSL2 engine)",
            action="https://www.docker.com/products/docker-desktop/",
        )
    )
    image = docker and p.image_exists(cfg)
    out.append(
        Check(
            "build image",
            image,
            cfg.builds.image if image else f"{cfg.builds.image} not built",
            "radar build --prepare",
            action="build-image",
        )
    )
    has_key = bool(p.api_key())
    out.append(
        Check(
            "Anthropic API key",
            has_key,
            "stored" if has_key else "not set",
            "paste the key in the daily builds panel",
            action="builds-key",
        )
    )
    repo = cfg.builds.repo
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
    return out


_EMAIL = re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def mask(text: str, root: Path) -> str:
    """Hide personal details in check output: repo paths become relative, the home folder
    becomes ~, and email addresses keep only their first letter and domain."""
    for base, label in ((str(root), "."), (str(Path.home()), "~")):
        text = text.replace(base, label)
    return _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
