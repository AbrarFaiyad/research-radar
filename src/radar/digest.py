"""Daily digest: local LLM curates saved posts, Codex researches them, output is rendered."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import Store
from .llm import ChatModel
from .models import DigestItem

CURATE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "post_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["post_id", "title", "why"],
            },
        }
    },
    "required": ["items"],
}

CURATE_SYSTEM = """You pick today's research briefing items for one reader. Today is {today}.

{profile}

You get saved LinkedIn posts. Choose at most {k} that point to a specific paper, preprint,
dataset, model, or tool worth a deep read. Merge duplicates (two posts about the same paper:
keep the one with the better link). Order by importance to the reader, core topics first.
title: your best guess at the work's title or a short descriptive name.
why: one short clause on why it made the cut.
Return only JSON."""

_S = {"type": "string"}
CODEX_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "overview", "papers", "podcast"],
    "properties": {
        "headline": _S,
        "overview": _S,
        "papers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "authors", "venue", "url", "what", "how",
                             "why_it_matters", "relevance", "limitations", "confidence"],
                "properties": {
                    "title": _S, "authors": _S, "venue": _S, "url": _S, "what": _S, "how": _S,
                    "why_it_matters": _S, "relevance": _S, "limitations": _S,
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        },
        "podcast": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["speaker", "text"],
                "properties": {"speaker": {"type": "string", "enum": ["A", "B"]}, "text": _S},
            },
        },
    },
}


@dataclass
class DigestResult:
    folder: Path
    summary_md: Path
    audio: Path | None
    items: list[DigestItem]


def _post_view(row) -> dict:
    links = json.loads(row["links"] or "[]")
    return {
        "post_id": row["id"],
        "tier": row["tier"],
        "author": row["author_name"],
        "summary": row["summary"],
        "topics": json.loads(row["topics"] or "[]"),
        "links": [link["url"] for link in links],
        "text": (row["text"] or "")[:1500],
    }


def curate(rows, model: ChatModel | None, profile: str, k: int,
           today: dt.date | None = None) -> list[DigestItem]:
    """Local LLM picks and orders items. Falls back to a rule ranking if no model."""
    views = [_post_view(r) for r in rows]
    by_id = {v["post_id"]: v for v in views}
    if not views:
        return []
    picked: list[DigestItem] = []
    if model is not None:
        system = CURATE_SYSTEM.format(today=(today or dt.date.today()).isoformat(),
                                      profile=profile.strip(), k=k)
        raw = model.json_chat(system, json.dumps(views, ensure_ascii=False, indent=1), CURATE_SCHEMA)
        for it in raw.get("items", []):
            v = by_id.get(it.get("post_id"))
            if v and all(p.post_id != v["post_id"] for p in picked):
                picked.append(DigestItem(v["post_id"], str(it.get("title") or v["summary"]),
                                         v["links"], str(it.get("why") or "")))
    if not picked:  # fallback or model returned nothing usable
        ranked = sorted(views, key=lambda v: (v["tier"] != "core", not v["links"]))
        picked = [DigestItem(v["post_id"], v["summary"] or "", v["links"], v["tier"]) for v in ranked]
    return picked[:k]


def build_codex_task(items: list[DigestItem], rows_by_id: dict, profile: str, template: str,
                     today: dt.date | None = None) -> str:
    blocks = []
    for i, it in enumerate(items, start=1):
        v = rows_by_id[it.post_id]
        blocks.append(
            f"### Item {i}: {it.title}\n"
            f"- Posted by: {v['author']}\n"
            f"- Poster summary: {v['summary']}\n"
            f"- Links: {', '.join(it.urls) or 'none'}\n"
            f"- Why selected: {it.why}\n"
            f"- Post text:\n{v['text']}\n"
        )
    return template.format(today=(today or dt.date.today()).isoformat(), profile=profile.strip(),
                           n=len(items), items="\n".join(blocks))


def codex_command(cfg: Config, folder: Path, schema_path: Path, out_path: Path) -> list[str]:
    exe = shutil.which(cfg.digest.codex_command) or cfg.digest.codex_command
    cmd = [exe]
    if cfg.digest.web_search:
        cmd.append("--search")
    cmd += ["exec", "-s", "read-only", "--skip-git-repo-check", "--ephemeral",
            "-C", str(folder), "--output-schema", str(schema_path), "-o", str(out_path)]
    if cfg.digest.codex_model:
        cmd += ["-m", cfg.digest.codex_model]
    cmd.append("-")
    return cmd


def run_codex(cmd: list[str], task: str, timeout_s: int = 3600) -> None:
    proc = subprocess.run(cmd, input=task, text=True, encoding="utf-8", capture_output=True,
                          timeout=timeout_s, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"codex exited {proc.returncode}: {proc.stderr[-2000:]}")


def render_markdown(result: dict, day: str, items: list[DigestItem]) -> str:
    out = [f"# Research digest, {day}", "", f"**{result.get('headline', '').strip()}**", "",
           result.get("overview", "").strip(), ""]
    for i, p in enumerate(result.get("papers", []), start=1):
        out += [
            f"## {i}. {p['title']}",
            f"*{p['authors']}* · {p['venue']} · confidence: {p['confidence']}  ",
            f"<{p['url']}>" if p["url"].strip() else "Primary source not found.",
            "",
            f"**What.** {p['what']}",
            "",
            f"**How.** {p['how']}",
            "",
            f"**Why it matters.** {p['why_it_matters']}",
            "",
            f"**For your work.** {p['relevance']}",
            "",
            f"**Limitations.** {p['limitations']}",
            "",
        ]
    out += ["---", "Selected from the feed:", ""]
    out += [f"- {it.title} ({it.why})" for it in items]
    return "\n".join(out) + "\n"


def run_digest(cfg: Config, store: Store, model: ChatModel | None,
               runner: Callable[[list[str], str], None] = run_codex,
               synthesize: Callable[[list[dict], Path], Path | None] | None = None,
               log: Callable[[str], None] = print,
               today: dt.date | None = None) -> DigestResult | None:
    today = today or dt.date.today()
    rows = store.saved_posts(undigested_only=True)
    if not rows:
        log("No new saved posts since the last digest.")
        return None
    profile = cfg.field_profile_text()
    items = curate(rows, model, profile, cfg.digest.max_items, today=today)
    if not items:
        log("Curation picked nothing.")
        return None
    log(f"curated {len(items)} of {len(rows)} saved posts")

    folder = cfg.digests_dir / today.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    rows_by_id = {r["id"]: _post_view(r) for r in rows}
    template = (cfg.root / "prompts" / "codex_digest.md").read_text(encoding="utf-8")
    task = build_codex_task(items, rows_by_id, profile, template, today=today)
    (folder / "codex_task.md").write_text(task, encoding="utf-8")
    schema_path = folder / "codex_schema.json"
    schema_path.write_text(json.dumps(CODEX_SCHEMA, indent=1), encoding="utf-8")
    out_path = folder / "codex_result.json"

    log("running Codex research (web search on); this can take several minutes")
    runner(codex_command(cfg, folder, schema_path, out_path), task)
    result = json.loads(out_path.read_text(encoding="utf-8"))

    summary_md = folder / "summary.md"
    summary_md.write_text(render_markdown(result, today.isoformat(), items), encoding="utf-8")
    script_md = folder / "podcast_script.md"
    script_md.write_text("\n\n".join(f"**{ln['speaker']}:** {ln['text']}" for ln in result["podcast"]),
                         encoding="utf-8")

    audio = None
    if synthesize is not None and result.get("podcast"):
        try:
            audio = synthesize(result["podcast"], folder / "episode.wav")
        except Exception as e:  # noqa: BLE001 - audio is optional; the written digest stands
            log(f"audio synthesis failed: {e}")

    # Every undigested saved post is now accounted for, picked or not.
    store.record_digest(today.isoformat(), str(folder), [r["id"] for r in rows])
    return DigestResult(folder, summary_md, audio, items)
