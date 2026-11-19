"""Command line: radar login | scan | reclassify | queue | digest | podcast | daily | demo | status."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import shutil
import sys
from pathlib import Path

from .classify import Classifier
from .config import load_config
from .db import Store
from .llm import OllamaModel


def _model(cfg):
    m = OllamaModel(cfg.llm.host, cfg.llm.model, cfg.llm.num_ctx, cfg.llm.timeout_s,
                    think=cfg.llm.think)
    if not m.available():
        sys.exit(f"Ollama model {cfg.llm.model!r} not reachable at {cfg.llm.host}. "
                 f"Start Ollama and run `ollama pull {cfg.llm.model}`.")
    return m


def cmd_login(cfg, args) -> int:
    from .scanner import login_flow
    ok = asyncio.run(login_flow(cfg))
    return 0 if ok else 1


def cmd_scan(cfg, args) -> int:
    from .scanner import scan_file, scan_live
    store = Store(cfg.db_path)
    classify = Classifier(_model(cfg), cfg.field_profile_text())
    if args.from_file:
        rep = scan_file(Path(args.from_file), store, classify)
    else:
        if args.max_posts:
            cfg.scan.max_posts = args.max_posts
        rep = asyncio.run(scan_live(cfg, store, classify))
    print("scan:", rep.line())
    for e in rep.errors:
        print("  error:", e)
    from .queue import write_queue
    md, _, n = write_queue(store, cfg.queue_dir)
    print(f"queue: {n} people -> {md}")
    return 0


def cmd_reclassify(cfg, args) -> int:
    from .queue import write_queue
    from .scanner import reclassify_stored
    store = Store(cfg.db_path)
    checked, changed = reclassify_stored(store, Classifier(_model(cfg), cfg.field_profile_text()))
    print(f"reclassify: checked {checked}, changed {changed}")
    md, _, n = write_queue(store, cfg.queue_dir)
    print(f"queue: {n} people -> {md}")
    return 0


def cmd_queue(cfg, args) -> int:
    from .queue import write_queue
    store = Store(cfg.db_path)
    if args.action == "mark":
        if not args.target or args.status not in ("sent", "skip", "todo", "connected"):
            sys.exit("usage: radar queue mark <profile_url|name> sent|skip|todo|connected")
        n = store.set_author_status(args.target, args.status)
        print(f"updated {n} author(s)")
    md, csv_path, n = write_queue(store, cfg.queue_dir, include_done=args.all)
    print(f"queue: {n} people -> {md} and {csv_path}")
    return 0


def cmd_digest(cfg, args) -> int:
    from .digest import run_digest
    from .podcast import make_synthesizer, wav_seconds
    store = Store(cfg.db_path)
    model = None if args.no_llm else _model(cfg)
    synth = None if args.no_audio else make_synthesizer(cfg.podcast, cfg.root)
    res = run_digest(cfg, store, model, synthesize=synth)
    if res:
        print(f"digest: {len(res.items)} items -> {res.summary_md}")
        if res.audio:
            print(f"audio: {res.audio} ({wav_seconds(res.audio) / 60:.1f} min)")
    return 0


def cmd_podcast(cfg, args) -> int:
    """Re-run text-to-speech for a finished digest (for example after a TTS failure)."""
    from .digest import resynthesize
    from .podcast import make_synthesizer, wav_seconds
    folders = sorted((p for p in cfg.digests_dir.glob("*") if (p / "codex_result.json").exists()),
                     key=lambda p: p.stat().st_mtime)
    if args.folder:
        folders = [cfg.digests_dir / args.folder]
    if not folders or not (folders[-1] / "codex_result.json").exists():
        sys.exit("No finished digest found.")
    out = resynthesize(folders[-1], make_synthesizer(cfg.podcast, cfg.root))
    print(f"audio: {out} ({wav_seconds(out) / 60:.1f} min)")
    return 0


def cmd_daily(cfg, args) -> int:
    rc = cmd_scan(cfg, argparse.Namespace(from_file=None, max_posts=None))
    if rc:
        return rc
    return cmd_digest(cfg, argparse.Namespace(no_llm=False, no_audio=False))


def cmd_demo(cfg, args) -> int:
    """Run the offline half of the pipeline on synthetic fixture posts in a throwaway data dir."""
    from .queue import write_queue
    from .scanner import scan_file
    demo_dir = cfg.root / "data" / "demo"
    if demo_dir.exists():
        shutil.rmtree(demo_dir)
    cfg.data_dir = demo_dir
    store = Store(cfg.db_path)
    classify = Classifier(_model(cfg), cfg.field_profile_text())
    fixture = cfg.root / "tests" / "fixtures" / "feed_sample.json"
    rep = scan_file(fixture, store, classify)
    print("demo scan:", rep.line())
    md, _, n = write_queue(store, cfg.queue_dir)
    print(f"demo queue: {n} people -> {md}")
    return 0


def cmd_status(cfg, args) -> int:
    store = Store(cfg.db_path)
    print("db:", cfg.db_path)
    print("posts by tier:", store.stats())
    print("queue (todo):", len(store.queue_rows()))
    last = store.conn.execute("SELECT day, folder FROM digests ORDER BY id DESC LIMIT 1").fetchone()
    print("last digest:", dict(last) if last else "none")
    print("today:", dt.date.today().isoformat())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="radar", description=__doc__)
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--data-dir", type=Path, default=None, help="override data directory")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="open the agent browser once and log in to LinkedIn by hand")

    s = sub.add_parser("scan", help="read the feed, classify, store, refresh the queue")
    s.add_argument("--from-file", help="process extractor-shaped JSON instead of the live feed")
    s.add_argument("--max-posts", type=int)

    sub.add_parser("reclassify", help="re-judge stored posts after editing the field profile")

    q = sub.add_parser("queue", help="export or update the connection queue")
    q.add_argument("action", nargs="?", default="export", choices=["export", "mark"])
    q.add_argument("target", nargs="?")
    q.add_argument("status", nargs="?")
    q.add_argument("--all", action="store_true", help="include sent/skipped people")

    d = sub.add_parser("digest", help="curate saved posts, research with Codex, render podcast")
    d.add_argument("--no-llm", action="store_true", help="skip local curation, rank by rules")
    d.add_argument("--no-audio", action="store_true")

    pc = sub.add_parser("podcast", help="rebuild episode.wav for a finished digest")
    pc.add_argument("folder", nargs="?", help="digest folder name, e.g. 2026-11-19 (default: latest)")

    sub.add_parser("daily", help="scan then digest (what the scheduled task runs)")
    sub.add_parser("demo", help="offline demo on synthetic posts")
    sub.add_parser("status", help="show database counts")

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    if args.data_dir:
        cfg.data_dir = args.data_dir.resolve()
    handlers = {"login": cmd_login, "scan": cmd_scan, "reclassify": cmd_reclassify, "queue": cmd_queue, "digest": cmd_digest,
                "podcast": cmd_podcast, "daily": cmd_daily, "demo": cmd_demo,
                "status": cmd_status}
    return handlers[args.cmd](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
