"""Pick the day's item for a build, name its folder, and keep the build history."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

OSI_LICENSES = frozenset(
    {
        "MIT",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "Apache-2.0",
        "MPL-2.0",
        "LGPL-2.1",
        "LGPL-3.0",
        "GPL-2.0",
        "GPL-3.0",
        "AGPL-3.0",
        "ISC",
        "Unlicense",
        "Zlib",
        "EPL-2.0",
        "BSL-1.0",
    }
)

# Privacy boundary: only these item keys may ever reach the sandbox.
PUBLIC_KEYS = (
    "title",
    "kind",
    "authors",
    "venue",
    "primary_url",
    "sources",
    "what",
    "how",
    "why_it_matters",
    "limitations",
    "code_url",
    "license",
)

NO_ITEM = "no item with public code, an open license, and a CPU demo"
MAX_SLUG = 40


@dataclass
class Candidate:
    rank: int
    title: str
    package: str
    code_url: str
    license: str
    item: dict


def _slugify(text: str, limit: int = MAX_SLUG) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug[:limit].strip("-")


def package_slug(code_url: str, title: str) -> str:
    """Repo name from a GitHub URL, project name from PyPI, else the slugified title."""
    parts = urlparse((code_url or "").strip())
    host = (parts.hostname or "").lower()
    segs = [s for s in parts.path.split("/") if s]
    name = ""
    if host in ("github.com", "www.github.com") and len(segs) >= 2:
        name = segs[1].removesuffix(".git")
    elif host in ("pypi.org", "www.pypi.org") and len(segs) >= 2 and segs[0] == "project":
        name = segs[1]
    return _slugify(name) or _slugify(title) or "item"


def folder_name(day: str, package: str, task: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError(f"day must be YYYY-MM-DD, got {day!r}")
    # Fallbacks keep the name publishable when a title has no ASCII letters or digits.
    return f"{day}_{_slugify(package) or 'item'}_{_slugify(task) or 'demo'}"


def _eligible(item: dict) -> bool:
    return (
        bool(str(item.get("code_url") or "").strip())
        and str(item.get("license") or "").strip() in OSI_LICENSES
        and item.get("cpu_demo") is True
    )


def pick(result: dict, built: set[str]) -> tuple[Candidate | None, str]:
    """First eligible, not yet built item in rank order, or the skip reason."""
    for rank, item in enumerate(result.get("items") or [], start=1):
        if not isinstance(item, dict) or not _eligible(item):
            continue
        code_url = str(item["code_url"]).strip()
        title = str(item.get("title") or "")
        package = package_slug(code_url, title)
        if package in built:
            continue
        public = {k: item[k] for k in PUBLIC_KEYS if k in item}
        return (
            Candidate(
                rank=rank,
                title=title,
                package=package,
                code_url=code_url,
                license=str(item["license"]).strip(),
                item=public,
            ),
            "",
        )
    return None, NO_ITEM


class HistoryCorrupt(RuntimeError):
    """The history file exists but cannot be read; it is left untouched."""


class History:
    """JSON record of published builds and attempts."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {"published": [], "attempts": []}
        except OSError as exc:
            raise HistoryCorrupt(f"cannot read {self.path}: {exc}") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
        except ValueError as exc:
            raise HistoryCorrupt(
                f"{self.path} is corrupt ({exc}); file left untouched, fix or delete it"
            ) from exc
        data.setdefault("published", [])
        data.setdefault("attempts", [])
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def built(self) -> set[str]:
        return {p["package"] for p in self._load()["published"] if "package" in p}

    def add_published(self, package: str, folder: str, url: str) -> None:
        data = self._load()
        data["published"].append({"package": package, "folder": folder, "url": url})
        self._save(data)

    def add_attempt(self, package: str, status: str, reason: str) -> None:
        data = self._load()
        data["attempts"].append({"package": package, "status": status, "reason": reason})
        self._save(data)

    def published_on(self, day: str) -> bool:
        return any(
            str(p.get("folder", "")).split("_", 1)[0] == day for p in self._load()["published"]
        )
