"""Configuration loading. Reads config/config.toml, falling back to config/config.example.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


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


@dataclass
class DigestCfg:
    max_items: int = 8
    codex_command: str = "codex"
    codex_model: str = ""
    web_search: bool = True


@dataclass
class PodcastCfg:
    backend: str = "sapi"
    host_a_voice: str = ""
    host_b_voice: str = ""
    kokoro_model: str = "data/models/kokoro-v1.0.onnx"
    kokoro_voices: str = "data/models/voices-v1.0.bin"
    kokoro_voice_a: str = "af_heart"
    kokoro_voice_b: str = "am_michael"


@dataclass
class Config:
    root: Path
    data_dir: Path
    field_profile: Path
    browser: BrowserCfg
    scan: ScanCfg
    llm: LlmCfg
    digest: DigestCfg
    podcast: PodcastCfg

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
        browser=_section(raw, "browser", BrowserCfg),
        scan=_section(raw, "scan", ScanCfg),
        llm=_section(raw, "llm", LlmCfg),
        digest=_section(raw, "digest", DigestCfg),
        podcast=_section(raw, "podcast", PodcastCfg),
    )
