"""Configuration loading. Reads config/config.toml, falling back to config/config.example.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ProfileCfg:
    # Who the digest and podcast are for, in one phrase. The topics live in field_profile.md.
    reader: str = "a researcher who wants the most important new work in the field described below"


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
    image: str = "research-radar-build:1"
    cpus: int = 4
    memory_gb: int = 8
    timeout_min: int = 45
    max_file_mb: float = 5
    max_folder_mb: float = 20
    allow_hosts: list[str] = field(
        default_factory=lambda: [
            "pypi.org",
            "files.pythonhosted.org",
            "github.com",
            "codeload.github.com",
            "objects.githubusercontent.com",
            "raw.githubusercontent.com",
            "huggingface.co",
            "cdn-lfs.huggingface.co",
            "readthedocs.io",
            "readthedocs.org",
            "github.io",
            "api.anthropic.com",
            "registry.npmjs.org",
        ]
    )
    claude_model: str = "sonnet"
    max_turns: int = 60


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
        return self.data_dir / "builds"

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
        source=path.resolve() if path.exists() else None,
    )
