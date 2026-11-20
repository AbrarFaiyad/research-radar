"""Editable settings: a typed schema over config/config.toml, saved with comments preserved."""

from __future__ import annotations

import math
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit


@dataclass(frozen=True)
class Field:
    section: str
    key: str
    kind: str  # int | float | bool | str | choice | email
    label: str
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""
    pattern: str = ""  # str kinds: a non-empty value must match this regex
    message: str = ""  # shown when the pattern does not match (default: "has the wrong format")

    @property
    def id(self) -> str:
        return f"{self.section}.{self.key}"


SCHEMA: tuple[Field, ...] = (
    Field(
        "profile",
        "reader",
        "str",
        "Who the digest is for",
        help="One phrase; topics live in the field profile.",
    ),
    Field("scan", "max_posts", "int", "Posts per scan", 1, 300),
    Field("scan", "max_minutes", "float", "Max scan minutes", 1, 60),
    Field("scan", "pause_s_min", "float", "Min pause between scrolls (s)", 0.5, 60),
    Field("scan", "pause_s_max", "float", "Max pause between scrolls (s)", 0.5, 120),
    Field("scan", "scroll_px_min", "int", "Min scroll step (px)", 100, 5000),
    Field("scan", "scroll_px_max", "int", "Max scroll step (px)", 100, 5000),
    Field("scan", "stale_steps_stop", "int", "Stop after N scrolls with nothing new", 1, 50),
    Field(
        "browser",
        "headless",
        "bool",
        "Hide the agent browser window",
        help="LinkedIn may challenge headless browsers.",
    ),
    Field("llm", "model", "str", "Local model (Ollama)"),
    Field("llm", "num_ctx", "int", "Context window (tokens)", 2048, 262144),
    Field("llm", "think", "bool", "Let the local model reason first", help="About 3x slower."),
    Field("llm", "timeout_s", "float", "Local model timeout (s)", 10, 600),
    Field("digest", "min_items", "int", "Codex keeps at least", 1, 50),
    Field("digest", "max_items", "int", "Codex keeps at most", 1, 50),
    Field("digest", "max_candidates", "int", "Max candidates sent to Codex", 1, 300),
    Field("digest", "backlog_days", "float", "Drop saved posts older than (days)", 0.5, 30),
    Field("digest", "web_search", "bool", "Codex web search"),
    Field("digest", "codex_model", "str", "Codex model (empty = default)"),
    Field("notebooklm", "enabled", "bool", "Make the NotebookLM podcast"),
    Field(
        "notebooklm", "length", "choice", "Podcast length", choices=("shorter", "default", "longer")
    ),
    Field("notebooklm", "add_web_sources", "bool", "Add source URLs to the notebook"),
    Field("notebooklm", "timeout_min", "float", "Podcast generation timeout (min)", 5, 120),
    Field("email", "enabled", "bool", "Send the daily email"),
    Field("email", "to", "email", "Send to"),
    Field("email", "sender", "email", "Send from (empty = same)"),
    Field("email", "subject_prefix", "str", "Subject prefix"),
    Field(
        "builds",
        "enabled",
        "bool",
        "Make a daily learning build",
        help="Needs a repo. Build files are kept in %LOCALAPPDATA%\\ResearchRadar\\builds.",
    ),
    Field(
        "builds",
        "mode",
        "choice",
        "When to build",
        choices=("with_digest", "separate"),
        help="With the digest, or at its own time.",
    ),
    Field(
        "builds",
        "separate_time",
        "str",
        "Separate build time (HH:MM)",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        message="must be a 24-hour time like 08:30",
    ),
    Field(
        "builds",
        "repo",
        "str",
        "GitHub repo (owner/name)",
        pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$",
        message="must look like owner/name",
    ),
    Field("builds", "git_name", "str", "Commit author name"),
    Field(
        "builds",
        "git_email",
        "email",
        "Commit author email",
        help="Your GitHub noreply address, from github.com/settings/emails.",
        pattern=r"^[^@\s]+@users\.noreply\.github\.com$",
        message="must be your GitHub noreply address (ending in @users.noreply.github.com)",
    ),
    Field("builds", "poll_min", "int", "Wait for the build workflow (min)", 5, 240),
)
BY_ID = {f.id: f for f in SCHEMA}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SettingsError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def config_path(root: Path) -> Path:
    path = root / "config" / "config.toml"
    if not path.exists():
        shutil.copyfile(root / "config" / "config.example.toml", path)
    return path


def load(root: Path) -> dict[str, Any]:
    doc = tomlkit.parse(config_path(root).read_text(encoding="utf-8"))
    values = {}
    for f in SCHEMA:
        v = doc.get(f.section, {}).get(f.key)
        values[f.id] = v.unwrap() if hasattr(v, "unwrap") else v
    return values


def coerce(f: Field, raw: Any) -> Any:
    if f.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("true", "1", "on", "yes"):
            return True
        if str(raw).lower() in ("false", "0", "off", "no", ""):
            return False
        raise ValueError("must be true or false")
    if f.kind in ("int", "float"):
        try:
            num = int(raw) if f.kind == "int" else float(raw)
        except (TypeError, ValueError):
            raise ValueError("must be a number") from None
        if not math.isfinite(num):
            raise ValueError("must be a finite number")
        if f.kind == "int" and isinstance(raw, float) and not raw.is_integer():
            raise ValueError("must be a whole number")
        if (f.lo is not None and num < f.lo) or (f.hi is not None and num > f.hi):
            raise ValueError(f"must be between {f.lo:g} and {f.hi:g}")
        return num
    text = str(raw if raw is not None else "").strip()
    if f.kind == "choice" and text not in f.choices:
        raise ValueError(f"must be one of {', '.join(f.choices)}")
    if f.kind == "email" and text and not _EMAIL.match(text):
        raise ValueError("is not an email address")
    if f.pattern and text and not re.match(f.pattern, text):
        raise ValueError(f.message or "has the wrong format")
    if len(text) > 500:
        raise ValueError("is too long")
    return text


def validate(updates: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    clean, errors = {}, {}
    for key, raw in updates.items():
        f = BY_ID.get(key)
        if f is None:
            errors[key] = "unknown setting"
            continue
        try:
            clean[key] = coerce(f, raw)
        except ValueError as e:
            errors[key] = str(e)
    merged = {**current, **clean}
    for lo, hi in (
        ("scan.pause_s_min", "scan.pause_s_max"),
        ("scan.scroll_px_min", "scan.scroll_px_max"),
        ("digest.min_items", "digest.max_items"),
    ):
        a, b = merged.get(lo), merged.get(hi)
        if a is not None and b is not None and a > b:
            errors[hi] = f"must be at least {BY_ID[lo].label.lower()} ({a:g})"
    if merged.get("email.enabled") and not merged.get("email.to"):
        errors["email.to"] = "is required when email is on"
    if merged.get("builds.enabled") and not merged.get("builds.repo"):
        errors["builds.repo"] = "is required when daily builds are on"
    if errors:
        raise SettingsError(errors)
    return clean


def save(root: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Validate and write. Raises SettingsError and leaves the file untouched on bad input."""
    path = config_path(root)
    clean = validate(updates, load(root))
    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    for key, value in clean.items():
        f = BY_ID[key]
        if f.section not in doc:
            doc[f.section] = tomlkit.table()
        doc[f.section][f.key] = value
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    os.replace(tmp, path)
    return load(root)


def schema_json() -> list[dict]:
    return [
        {
            "id": f.id,
            "section": f.section,
            "kind": f.kind,
            "label": f.label,
            "lo": f.lo,
            "hi": f.hi,
            "choices": list(f.choices),
            "help": f.help,
        }
        for f in SCHEMA
    ]
