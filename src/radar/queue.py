"""Connection queue export. The agent lists people; the user sends every request by hand."""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

from .db import Store

COLUMNS = ["rank", "name", "profile_url", "degree", "headline", "core_posts", "saved_posts",
           "topics", "why", "first_seen", "status"]


def queue_records(store: Store, include_done: bool = False) -> list[dict]:
    out = []
    for i, r in enumerate(store.queue_rows(include_done=include_done), start=1):
        topics = json.loads(r["latest_topics"] or "[]")
        out.append({
            "rank": i,
            "name": r["name"] or "",
            "profile_url": r["url"],
            "degree": r["degree"] or "not shown",
            "headline": r["headline"] or "",
            "core_posts": r["core_posts"],
            "saved_posts": r["saved_posts"],
            "topics": ", ".join(topics),
            "why": r["latest_summary"] or "",
            "first_seen": (r["first_seen"] or "")[:10],
            "status": r["status"],
        })
    return out


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def write_queue(store: Store, out_dir: Path, include_done: bool = False) -> tuple[Path, Path, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = queue_records(store, include_done=include_done)

    csv_path = out_dir / "connect_queue.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    md_path = out_dir / "connect_queue.md"
    lines = [
        f"# Connection queue ({dt.date.today().isoformat()})",
        "",
        "People behind saved posts who are not 1st-degree connections, most relevant first.",
        "Open each profile, decide, and send the request yourself. Then mark it:",
        "`radar queue mark <profile_url> sent` (or `skip`).",
        "",
        "| # | Name | Degree | Headline | Why (latest saved post) | Topics |",
        "|---|------|--------|----------|-------------------------|--------|",
    ]
    for r in rows:
        name = f"[{_md_escape(r['name'])}]({r['profile_url']})"
        lines.append(
            f"| {r['rank']} | {name} | {r['degree']} | {_md_escape(r['headline'][:90])} | "
            f"{_md_escape(r['why'][:160])} | {_md_escape(r['topics'])} |"
        )
    if not rows:
        lines.append("| | Queue is empty. | | | | |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, csv_path, len(rows)
