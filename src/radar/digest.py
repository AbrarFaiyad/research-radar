"""Daily digest: Codex selects, sources, and summarizes the day's candidate posts.

The local model only pre-screens the feed. Every candidate it saved goes to Codex, which keeps
the 5 to 15 items with real impact, finds their primary sources and related coverage, and
writes the summary. Audio comes afterward from NotebookLM (see notebooklm.py).
"""

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

_S = {"type": "string"}
CODEX_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "overview", "items", "rejected", "podcast_focus"],
    "properties": {
        "headline": _S,
        "overview": _S,
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["post_ids", "title", "kind", "authors", "venue", "primary_url",
                             "sources", "what", "how", "why_it_matters", "relevance",
                             "limitations", "impact", "confidence"],
                "properties": {
                    "post_ids": {"type": "array", "items": {"type": "integer"}},
                    "title": _S,
                    "kind": {"type": "string", "enum": ["paper", "preprint", "dataset", "model",
                                                        "software", "news", "article", "other"]},
                    "authors": _S,
                    "venue": _S,
                    "primary_url": _S,
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["title", "url"],
                            "properties": {"title": _S, "url": _S},
                        },
                    },
                    "what": _S,
                    "how": _S,
                    "why_it_matters": _S,
                    "relevance": _S,
                    "limitations": _S,
                    "impact": {"type": "string", "enum": ["high", "medium"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        },
        "rejected": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["post_id", "reason"],
                "properties": {"post_id": {"type": "integer"}, "reason": _S},
            },
        },
        "podcast_focus": _S,
    },
}


@dataclass
class DigestResult:
    folder: Path
    summary_md: Path
    audio: Path | None
    kept: int
    reviewed: int


def _post_view(row) -> dict:
    links = json.loads(row["links"] or "[]")
    return {
        "post_id": row["id"],
        "author": row["author_name"],
        "screen_summary": row["summary"],
        "links": [link["url"] for link in links],
        "text": (row["text"] or "")[:2500],
    }


def build_codex_task(rows, profile: str, template: str, min_items: int, max_items: int,
                     today: dt.date | None = None) -> str:
    blocks = []
    for r in rows:
        v = _post_view(r)
        blocks.append(
            f"### Candidate {v['post_id']}\n"
            f"- Posted by: {v['author']}\n"
            f"- Screen summary: {v['screen_summary']}\n"
            f"- Links: {', '.join(v['links']) or 'none'}\n"
            f"- Post text:\n{v['text']}\n"
        )
    return template.format(
        today=(today or dt.date.today()).isoformat(), profile=profile.strip(), n=len(rows),
        min_items=min_items, max_items=max_items, items="\n".join(blocks),
    )


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


def render_markdown(result: dict, day: str, reviewed: int) -> str:
    items = result.get("items", [])
    out = [f"# Research digest, {day}", "", f"**{result.get('headline', '').strip()}**", "",
           result.get("overview", "").strip(), "",
           f"*{len(items)} kept of {reviewed} candidate posts.*", ""]
    for i, p in enumerate(items, start=1):
        src = f"<{p['primary_url']}>" if p["primary_url"].strip() else "Primary source not found."
        out += [
            f"## {i}. {p['title']}",
            (f"*{p['authors']}* · {p['venue']} · {p['kind']} · impact: {p['impact']} · "
             f"confidence: {p['confidence']}  "),
            src,
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
        extra = [s for s in p.get("sources", []) if s.get("url")]
        if extra:
            out += ["**More sources.** " + " · ".join(f"[{s['title']}]({s['url']})" for s in extra), ""]
    return "\n".join(out) + "\n"


def source_urls(result: dict, limit: int = 40) -> list[str]:
    """Primary and supporting URLs, deduped, for NotebookLM website sources."""
    urls: list[str] = []
    for p in result.get("items", []):
        for u in [p.get("primary_url", "")] + [s.get("url", "") for s in p.get("sources", [])]:
            u = (u or "").strip()
            if u.startswith("http") and u not in urls:
                urls.append(u)
    return urls[:limit]


AudioMaker = Callable[[Path, dict], Path | None]


def run_digest(cfg: Config, store: Store,
               runner: Callable[[list[str], str], None] = run_codex,
               make_audio: AudioMaker | None = None,
               log: Callable[[str], None] = print,
               today: dt.date | None = None) -> DigestResult | None:
    today = today or dt.date.today()
    rows = store.saved_posts(undigested_only=True, since_days=cfg.digest.backlog_days)
    rows = rows[: cfg.digest.max_candidates]
    if not rows:
        log("No new saved posts since the last digest.")
        return None
    log(f"sending {len(rows)} candidate posts to Codex for selection and research")

    folder = unique_folder(cfg.digests_dir, today.isoformat())
    template = (cfg.root / "prompts" / "codex_digest.md").read_text(encoding="utf-8")
    task = build_codex_task(rows, cfg.field_profile_text(), template,
                            cfg.digest.min_items, cfg.digest.max_items, today=today)
    (folder / "codex_task.md").write_text(task, encoding="utf-8")
    schema_path = folder / "codex_schema.json"
    schema_path.write_text(json.dumps(CODEX_SCHEMA, indent=1), encoding="utf-8")
    out_path = folder / "codex_result.json"

    log("running Codex (web search on); this can take several minutes")
    runner(codex_command(cfg, folder, schema_path, out_path), task)
    result = json.loads(out_path.read_text(encoding="utf-8"))

    summary_md = folder / "summary.md"
    summary_md.write_text(render_markdown(result, today.isoformat(), len(rows)), encoding="utf-8")
    (folder / "sources.txt").write_text("\n".join(source_urls(result)) + "\n", encoding="utf-8")

    # Codex judged every candidate (kept or rejected), so all of them are done. Recorded
    # before audio, so an audio failure never triggers a second Codex run.
    store.record_digest(today.isoformat(), str(folder), [r["id"] for r in rows])
    log(f"Codex kept {len(result.get('items', []))} of {len(rows)}")

    audio = None
    if make_audio is not None and result.get("items"):
        try:
            audio = make_audio(folder, result)
        except Exception as e:  # noqa: BLE001 - audio is optional; the written digest stands
            log(f"audio failed: {e}. Retry with `radar podcast {folder.name}`.")
    return DigestResult(folder, summary_md, audio, len(result.get("items", [])), len(rows))


def unique_folder(base: Path, name: str) -> Path:
    """data/digests/2026-11-19, then 2026-11-19-2, ... so a second run never overwrites."""
    folder, n = base / name, 2
    while folder.exists():
        folder, n = base / f"{name}-{n}", n + 1
    folder.mkdir(parents=True)
    return folder
