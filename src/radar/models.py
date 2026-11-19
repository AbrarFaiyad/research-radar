"""Plain data records passed between pipeline stages."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

TIERS = ("core", "adjacent", "skip")
KINDS = ("paper", "preprint", "dataset", "model", "software", "result", "talk", "event", "news", "milestone", "ad", "other")


@dataclass
class RawPost:
    """One feed card as returned by extract_feed.js."""

    key: str
    author: str | None
    author_url: str | None
    author_kind: str  # "person" | "company" | "school" | "unknown"
    degree: str | None  # "1st" | "2nd" | "3rd+" | None
    headline: str | None
    text: str
    links: list[str] = field(default_factory=list)
    promoted: bool = False
    age: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> RawPost:
        return cls(
            key=str(d.get("key") or ""),
            author=d.get("author") or None,
            author_url=_clean_profile_url(d.get("authorUrl") or d.get("author_url")),
            author_kind=d.get("authorKind") or d.get("author_kind") or "unknown",
            degree=_norm_degree(d.get("degree")),
            headline=(d.get("headline") or None),
            text=(d.get("text") or "").strip(),
            links=[u for u in (d.get("links") or []) if isinstance(u, str)],
            promoted=bool(d.get("promoted")),
            age=d.get("age") or None,
        )

    @property
    def text_hash(self) -> str:
        norm = re.sub(r"\s+", " ", self.text.lower()).strip()
        return hashlib.sha1(f"{self.author}|{norm[:500]}".encode()).hexdigest()[:16]


@dataclass
class Verdict:
    tier: str
    kind: str
    topics: list[str]
    summary: str
    reason: str

    @property
    def saved(self) -> bool:
        return self.tier in ("core", "adjacent")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DigestItem:
    post_id: int
    title: str
    urls: list[str]
    why: str


def _norm_degree(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    if v.startswith("1"):
        return "1st"
    if v.startswith("2"):
        return "2nd"
    if v.startswith("3"):
        return "3rd+"
    return None


def _clean_profile_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.split("?")[0]
    return url if url.endswith("/") else url + "/"
