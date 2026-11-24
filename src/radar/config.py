"""Configuration loading. Reads config/config.toml, falling back to config/config.example.toml."""

from __future__ import annotations

import contextlib
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ProfileCfg:
    # Who the digest and podcast are for, in one phrase. The topics live in field_profile.md.
    reader: str = "a researcher who wants the most important new work in the field described below"
    # Names, handles or words that must never be published (checked before anything is published).
    private_markers: list[str] = field(default_factory=list)


@dataclass
class BrowserCfg:
    mcp_command: str = "npx"
    mcp_args: list[str] = field(
        default_factory=lambda: ["-y", "@playwright/mcp@0.0.84", "--browser", "chromium"]
    )
    headless: bool = False
    feed_url: str = "https://www.linkedin.com/feed/"


@dataclass
class ScanCfg:
    max_posts: int = 60
    max_minutes: float = 10
    scroll_px_min: int = 600
    scroll_px_max: int = 1100
    pause_s_min: float = 2.5
    pause_s_max: float = 6.0
    stale_steps_stop: int = 6


@dataclass
class LlmCfg:
    host: str = "http://localhost:11434"
    model: str = "mimo-v2.6"
    num_ctx: int = 16384
    timeout_s: float = 120
    think: bool = False


@dataclass
class DigestCfg:
    min_items: int = 5
    max_items: int = 15
    max_candidates: int = 80
    backlog_days: float = 3
    codex_command: str = "codex"
    codex_model: str = ""
    web_search: bool = True


@dataclass
class NotebookLMCfg:
    enabled: bool = True
    length: str = "default"  # NotebookLM Audio Overview length: shorter | default | longer
    timeout_min: float = 30  # generation usually takes 5 to 15 minutes
    add_web_sources: bool = True  # add Codex's source URLs as website sources


@dataclass
class EmailCfg:
    enabled: bool = False
    to: str = ""
    sender: str = ""  # Gmail account that sends; empty = same as `to`
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    subject_prefix: str = "Research Radar"


@dataclass
class BuildsCfg:
    enabled: bool = False
    mode: str = "with_digest"  # with_digest | separate
    separate_time: str = "08:30"
    repo: str = ""  # owner/name
    git_name: str = ""
    git_email: str = ""
    work_dir: str = ""  # empty = default_builds_dir(); relative paths are under the repo root
    poll_min: int = 90  # how long the daily run waits for the workflow before leaving it


@dataclass
class PodcastsCfg:
    enabled: bool = False
    repo: str = ""  # owner/name of the public GitHub repo whose releases hold the episodes


def default_builds_dir(windows: bool | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Build data lives in the user's local app data, outside the repo and any synced folder
    (sync would upload records and logs)."""
    windows = os.name == "nt" if windows is None else windows
    env = os.environ if env is None else env
    if windows:
        base = Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "ResearchRadar" / "builds"
    base = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "research-radar" / "builds"


@dataclass
class Config:
    root: Path
    data_dir: Path
    field_profile: Path
    profile: ProfileCfg
    browser: BrowserCfg
    scan: ScanCfg
    llm: LlmCfg
    digest: DigestCfg
    notebooklm: NotebookLMCfg
    email: EmailCfg = field(default_factory=EmailCfg)
    builds: BuildsCfg = field(default_factory=BuildsCfg)
    podcasts: PodcastsCfg = field(default_factory=PodcastsCfg)
    source: Path | None = None  # the TOML file this config came from

    @property
    def db_path(self) -> Path:
        return self.data_dir / "radar.db"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def queue_dir(self) -> Path:
        return self.data_dir / "queue"

    @property
    def digests_dir(self) -> Path:
        return self.data_dir / "digests"

    @property
    def builds_dir(self) -> Path:
        """Records, history, logs, work folders and the publish clone (created on demand)."""
        raw = self.builds.work_dir.strip()
        path = Path(raw).expanduser() if raw else default_builds_dir()
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        with contextlib.suppress(OSError):  # callers that write report their own errors
            path.mkdir(parents=True, exist_ok=True)
        return path

    def field_profile_text(self) -> str:
        return self.field_profile.read_text(encoding="utf-8")


def _section(raw: dict, name: str, cls):
    values = raw.get(name, {})
    known = {k: v for k, v in values.items() if k in cls.__dataclass_fields__}
    return cls(**known)


def load_config(path: Path | None = None, root: Path = REPO_ROOT) -> Config:
    if path is None:
        path = root / "config" / "config.toml"
        if not path.exists():
            path = root / "config" / "config.example.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    paths = raw.get("paths", {})
    data_dir = (root / paths.get("data_dir", "data")).resolve()
    field_profile = (root / paths.get("field_profile", "config/field_profile.md")).resolve()
    return Config(
        root=root,
        data_dir=data_dir,
        field_profile=field_profile,
        profile=_section(raw, "profile", ProfileCfg),
        browser=_section(raw, "browser", BrowserCfg),
        scan=_section(raw, "scan", ScanCfg),
        llm=_section(raw, "llm", LlmCfg),
        digest=_section(raw, "digest", DigestCfg),
        notebooklm=_section(raw, "notebooklm", NotebookLMCfg),
        email=_section(raw, "email", EmailCfg),
        builds=_section(raw, "builds", BuildsCfg),
        podcasts=_section(raw, "podcasts", PodcastsCfg),
        source=path.resolve() if path.exists() else None,
    )
