"""Relevance classification: cheap rules first, then the local LLM."""

from __future__ import annotations

import datetime as dt
import re

from .llm import ChatModel
from .models import KINDS, TIERS, RawPost, Verdict

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "tier": {"type": "string", "enum": list(TIERS)},
        "kind": {"type": "string", "enum": list(KINDS)},
        "topics": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "summary": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["tier", "kind", "topics", "summary", "reason"],
}

SYSTEM_TEMPLATE = """You screen LinkedIn posts for one reader. Today is {today}.
Decide whether each post reports informative scientific progress in the reader's field.

{profile}

Rules:
- tier "core": the post's main content is a paper, preprint, dataset, model, tool, or concrete result in a Core area.
- tier "adjacent": a paper, preprint, tool, system, or concrete result in an Adjacent area. Apply the
  Adjacent list generously: new HPC or AI-for-science computing resources, scientific-computing tools,
  and scientific studies of AI agents all qualify even when they are not about materials.
- tier "skip": everything else, including ads, business news, unrelated fields, and personal milestones
  (graduations, defenses, new jobs, awards, congratulations) that do not themselves report a result.
  An award or workshop post that describes the new technical work behind it is not a milestone; judge the work.
- Event or talk announcements count only if they describe specific new technical work in the field.
- You are a first-pass screen. A stronger model reviews everything you save, so when a post is
  borderline between "adjacent" and "skip", choose "adjacent". Still skip ads, milestones, and
  clearly unrelated posts.
- Dates up to today are valid; do not reject a post because a paper or arXiv id looks recent.
- summary: one plain sentence on what was done or found. reason: one short clause on why this tier.
- topics: up to 5 short lowercase tags, e.g. "mlip", "mace", "mos2", "molten salts", "hpc".
Return only the JSON object."""


def prefilter(post: RawPost) -> Verdict | None:
    """Rule-based verdicts that need no model call."""
    if post.promoted:
        return Verdict("skip", "ad", [], "Promoted post.", "promoted")
    if len(post.text) < 40 and not post.links:
        return Verdict("skip", "other", [], "Too little text to judge.", "empty")
    return None


def build_user_prompt(post: RawPost) -> str:
    parts = [
        f"Author: {post.author or 'unknown'} ({post.author_kind})",
        f"Author headline: {post.headline or 'n/a'}",
        f"Links: {', '.join(post.links[:6]) or 'none'}",
        "Post text:",
        post.text[:6000],
    ]
    return "\n".join(parts)


class Classifier:
    def __init__(self, model: ChatModel, profile: str, today: dt.date | None = None):
        self.model = model
        self.system = SYSTEM_TEMPLATE.format(
            today=(today or dt.date.today()).isoformat(), profile=profile.strip()
        )

    def __call__(self, post: RawPost) -> Verdict:
        quick = prefilter(post)
        if quick is not None:
            return quick
        raw = self.model.json_chat(self.system, build_user_prompt(post), VERDICT_SCHEMA)
        return coerce_verdict(raw)


_INVISIBLE = re.compile("[\u200b-\u200f\u2060\ufeff]")


def _clean_tag(t) -> str:
    return " ".join(_INVISIBLE.sub("", str(t)).lower().split())


def coerce_verdict(raw: dict) -> Verdict:
    tier = raw.get("tier") if raw.get("tier") in TIERS else "skip"
    kind = raw.get("kind") if raw.get("kind") in KINDS else "other"
    topics = [c for c in (_clean_tag(t) for t in (raw.get("topics") or [])) if c][:5]
    return Verdict(
        tier=tier,
        kind=kind,
        topics=topics,
        summary=str(raw.get("summary") or "").strip(),
        reason=str(raw.get("reason") or "").strip(),
    )
