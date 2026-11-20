"""Research Radar command line.

Pipeline:  scan | reclassify | digest | podcast | email | daily
Setup:     onboard | setup | connections | login | notebooklm-login | email-setup | doctor
Inspect:   status | queue | demo | ui

Everything here also has a button in the control panel (`radar ui`); the panel runs these
commands as subprocesses, and so does the daily scheduled task.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .classify import Classifier
from .config import Config, load_config
from .db import Store
from .llm import OllamaModel
from .pipeline import LockHeld, RunLock, run_stages, save_run


class UserError(RuntimeError):
    """A problem the user can fix; printed without a traceback."""


def _model(cfg: Config) -> OllamaModel:
    m = OllamaModel(
        cfg.llm.host, cfg.llm.model, cfg.llm.num_ctx, cfg.llm.timeout_s, think=cfg.llm.think
    )
    if not m.available():
        raise UserError(
            f"Ollama model {cfg.llm.model!r} not reachable at {cfg.llm.host}. "
            f"Start Ollama and run `ollama pull {cfg.llm.model}`."
        )
    return m


# ---- stage functions: do the work, return a detail dict for run records -------------------


def stage_scan(cfg: Config, max_posts: int | None = None, from_file: str | None = None) -> dict:
    from .queue import write_queue
    from .scanner import scan_file, scan_live

    store = Store(cfg.db_path)
    classify = Classifier(_model(cfg), cfg.field_profile_text())
    if from_file:
        rep = scan_file(Path(from_file), store, classify)
    else:
        if max_posts:
            cfg.scan.max_posts = max_posts
        rep = asyncio.run(scan_live(cfg, store, classify))
    print("scan:", rep.line())
    for e in rep.errors:
        print("  error:", e)
    md, _, n = write_queue(store, cfg.queue_dir)
    print(f"queue: {n} people -> {md}")
    if rep.new and len(rep.errors) >= rep.new:
        raise RuntimeError(f"all {rep.new} new posts failed classification: {rep.errors[0]}")
    return {
        "seen": rep.seen,
        "new": rep.new,
        "saved": rep.saved,
        "skipped": rep.skipped,
        "errors": len(rep.errors),
        "queue": n,
    }


def _audio_maker(cfg: Config):
    if not cfg.notebooklm.enabled:
        return None
    from .notebooklm import make_audio

    return lambda folder, result: make_audio(cfg, folder, result)


def stage_digest(cfg: Config, no_audio: bool = False) -> dict:
    from .digest import run_digest

    store = Store(cfg.db_path)
    res = run_digest(cfg, store, make_audio=None if no_audio else _audio_maker(cfg))
    if not res:
        return {"kept": 0, "reviewed": 0}
    print(f"digest: kept {res.kept} of {res.reviewed} -> {res.summary_md}")
    if res.audio:
        print(f"audio: {res.audio}")
    elif cfg.notebooklm.enabled and not no_audio and res.kept:
        raise RuntimeError(
            f"digest written but podcast failed; retry: radar podcast {res.folder.name}"
        )
    return {
        "kept": res.kept,
        "reviewed": res.reviewed,
        "folder": res.folder.name,
        "audio": res.audio.name if res.audio else None,
    }


def _latest_digest(cfg: Config, today_only: bool = False) -> Path | None:
    folders = sorted(
        (p for p in cfg.digests_dir.glob("*") if (p / "summary.md").exists()),
        key=lambda p: p.stat().st_mtime,
    )
    if today_only:
        folders = [p for p in folders if p.name.startswith(dt.date.today().isoformat())]
    return folders[-1] if folders else None


def stage_podcast(cfg: Config, folder_name: str | None = None) -> dict:
    from .notebooklm import make_audio

    folder = cfg.digests_dir / folder_name if folder_name else _latest_digest(cfg)
    if folder is None or not (folder / "codex_result.json").exists():
        raise UserError("No finished digest found.")
    result = json.loads((folder / "codex_result.json").read_text(encoding="utf-8"))
    out = make_audio(cfg, folder, result)
    print(f"audio: {out}")
    return {"folder": folder.name, "audio": out.name}


def stage_email(
    cfg: Config, folder_name: str | None = None, today_only: bool = False, test: bool = False
) -> dict:
    from .mailer import build_message, compose_daily, send

    if not cfg.email.to or cfg.email.to.endswith("example.com"):
        raise UserError("Set the email address in Settings (or [email] to in config.toml) first.")
    if test:
        msg = build_message(
            cfg.email,
            f"{cfg.email.subject_prefix}: test email",
            "Research Radar can send you email. The daily digest will arrive like this.",
            [],
        )
        send(cfg.email, msg)
        print(f"test email sent to {cfg.email.to}")
        return {"attachments": []}
    folder = cfg.digests_dir / folder_name if folder_name else _latest_digest(cfg, today_only)
    msg = compose_daily(cfg.email, folder, cfg.queue_dir)
    send(cfg.email, msg)
    names = [p.get_filename() for p in msg.iter_attachments()]
    print(
        f"email sent to {cfg.email.to}: {msg['Subject']} (attachments: {', '.join(names) or 'none'})"
    )
    return {"attachments": names}


def stage_reclassify(cfg: Config) -> dict:
    from .queue import write_queue
    from .scanner import reclassify_stored

    store = Store(cfg.db_path)
    checked, changed = reclassify_stored(store, Classifier(_model(cfg), cfg.field_profile_text()))
    print(f"reclassify: checked {checked}, changed {changed}")
    md, _, n = write_queue(store, cfg.queue_dir)
    print(f"queue: {n} people -> {md}")
    return {"checked": checked, "changed": changed, "queue": n}


# ---- commands ------------------------------------------------------------------------------


def _locked(cfg: Config, name: str, fn) -> int:
    with RunLock(cfg, owner=name):
        fn()
    return 0


def cmd_scan(cfg, args) -> int:
    return _locked(cfg, "scan", lambda: stage_scan(cfg, args.max_posts, args.from_file))


def cmd_reclassify(cfg, args) -> int:
    return _locked(cfg, "reclassify", lambda: stage_reclassify(cfg))


def cmd_digest(cfg, args) -> int:
    return _locked(cfg, "digest", lambda: stage_digest(cfg, args.no_audio))


def cmd_podcast(cfg, args) -> int:
    return _locked(cfg, "podcast", lambda: stage_podcast(cfg, args.folder))


def cmd_email(cfg, args) -> int:
    return _locked(cfg, "email", lambda: stage_email(cfg, args.folder, args.today_only, args.test))


def cmd_onboard(cfg, args) -> int:
    from . import onboard

    def draft():
        onboard.draft_profile(cfg)

    def calibrate():
        from .scanner import collect_live, stored_posts

        state = onboard.load_state(cfg)
        prof = onboard.current_profile(state)
        if not prof:
            raise UserError("Draft a profile first.")
        if args.source == "stored":
            store = Store(cfg.db_path)
            try:
                posts = stored_posts(store, args.max_posts)
            finally:
                store.close()
            print(f"calibrating on {len(posts)} stored posts")
        else:
            cfg.scan.max_posts = args.max_posts
            print(f"reading up to {args.max_posts} posts from the feed (nothing is saved)")
            posts = asyncio.run(collect_live(cfg))
        classify = Classifier(_model(cfg), prof["profile"])
        onboard.calibrate(cfg, posts, classify, args.source)

    def refine():
        onboard.refine_profile(cfg)

    step = {"draft": draft, "calibrate": calibrate, "refine": refine}[args.step]
    try:
        return _locked(cfg, f"onboard-{args.step}", step)
    except ValueError as e:
        raise UserError(str(e)) from None


PLAYWRIGHT_VERSION = "1.65.0-alpha-2026-10-30"


def _stream(argv: list[str]) -> int:
    """Run a setup tool and pass its output through line by line (the panel log shows it)."""
    import subprocess

    exe = shutil.which(argv[0])
    if exe is None:
        raise UserError(f"{argv[0]} is not installed or not on PATH.")
    print("$", " ".join(argv), flush=True)
    proc = subprocess.Popen(
        [exe, *argv[1:]],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout
    for line in proc.stdout:
        print(line.rstrip(), flush=True)
    return proc.wait()


def cmd_setup(cfg, args) -> int:
    root = cfg.root
    if args.what == "config":
        target = root / "config" / "config.toml"
        if target.exists():
            print(f"{target.name} already exists; nothing to do")
            return 0
        shutil.copyfile(root / "config" / "config.example.toml", target)
        print(f"created {target.relative_to(root)} from the example")
        return 0
    commands = {
        "model": ["ollama", "pull", cfg.llm.model],
        "chromium": ["npx", "-y", f"playwright@{PLAYWRIGHT_VERSION}", "install", "chromium"],
        "codex": ["npm", "install", "-g", "@openai/codex"],
        "codex-login": [cfg.digest.codex_command, "login"],
    }
    code = _stream(commands[args.what])
    print("done" if code == 0 else f"failed with exit code {code}")
    return code


def cmd_daily(cfg, args) -> int:
    stages = {
        "scan": lambda: stage_scan(cfg),
        "digest": lambda: stage_digest(cfg),
    }
    if cfg.email.enabled:
        stages["email"] = lambda: stage_email(cfg, today_only=True)
    with RunLock(cfg, owner="daily"):
        rec = run_stages(stages)
    path = save_run(cfg, rec)
    print(f"run {'ok' if rec.ok else 'had failures'}: {path}")
    return 0 if rec.ok else 1


def cmd_queue(cfg, args) -> int:
    from .queue import write_queue

    store = Store(cfg.db_path)
    if args.action == "mark":
        if not args.target or args.status not in ("sent", "skip", "todo", "connected"):
            raise UserError("usage: radar queue mark <profile_url|name> sent|skip|todo|connected")
        print(f"updated {store.set_author_status(args.target, args.status)} author(s)")
    md, csv_path, n = write_queue(store, cfg.queue_dir, include_done=args.all)
    print(f"queue: {n} people -> {md} and {csv_path}")
    return 0


def cmd_connections(cfg, args) -> int:
    from .connections import read_export
    from .queue import write_queue

    path = Path(args.file)
    try:
        conns, pending = read_export(path.name, path.read_bytes())
    except (OSError, ValueError) as e:
        raise UserError(str(e)) from None
    store = Store(cfg.db_path)
    try:
        res = store.import_connections(conns, pending)
        write_queue(store, cfg.queue_dir)
    finally:
        store.close()
    print(
        f"imported {res['connections']} connections and {res['pending']} pending invitations; "
        f"{res['removed_from_queue']} people left the queue"
    )
    return 0


def cmd_login(cfg, args) -> int:
    from .scanner import login_flow

    with RunLock(cfg, owner="login"):  # the scan uses the same browser profile
        return 0 if asyncio.run(login_flow(cfg)) else 1


def cmd_notebooklm_login(cfg, args) -> int:
    from .notebooklm import login_flow

    with RunLock(cfg, owner="notebooklm-login"):
        return 0 if asyncio.run(login_flow(cfg)) else 1


def cmd_email_setup(cfg, args) -> int:
    import getpass

    from .mailer import set_password

    user = cfg.email.sender or cfg.email.to
    if not user or user.endswith("example.com"):
        raise UserError("Set [email] to (and optionally sender) in config/config.toml first.")
    print(f"Paste the Gmail App Password for {user} (input is hidden).")
    pw = getpass.getpass("App Password: ").replace(" ", "")
    if not pw:
        raise UserError("No password entered.")
    set_password(user, pw)
    print("Saved in Windows Credential Manager. Test with `radar email`.")
    return 0


def cmd_doctor(cfg, args) -> int:
    from .doctor import run_checks

    checks = run_checks(cfg)
    for c in checks:
        mark = "ok  " if c.ok else ("FAIL" if c.required else "warn")
        print(f"[{mark}] {c.name}: {c.detail}")
        if not c.ok and c.fix:
            print(f"       fix: {c.fix}")
    return 0 if all(c.ok or not c.required for c in checks) else 1


def cmd_ui(cfg, args) -> int:
    from .ui.app import serve

    serve(cfg, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_demo(cfg, args) -> int:
    """Run the offline half of the pipeline on synthetic fixture posts in a throwaway data dir."""
    demo_dir = cfg.root / "data" / "demo"
    if demo_dir.exists():
        shutil.rmtree(demo_dir)
    cfg.data_dir = demo_dir
    stage_scan(cfg, from_file=str(cfg.root / "tests" / "fixtures" / "feed_sample.json"))
    return 0


def cmd_status(cfg, args) -> int:
    from .pipeline import latest_run

    store = Store(cfg.db_path)
    print("db:", cfg.db_path)
    print("posts by tier:", store.stats())
    print("queue (todo):", len(store.queue_rows()))
    last = store.conn.execute("SELECT day, folder FROM digests ORDER BY id DESC LIMIT 1").fetchone()
    print("last digest:", dict(last) if last else "none")
    run = latest_run(cfg)
    if run:
        stages = ", ".join(f"{s['name']} {'ok' if s['ok'] else 'FAILED'}" for s in run["stages"])
        print(f"last run: {run['started']} ({stages})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="radar", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--version", action="version", version=f"radar {__version__}")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--data-dir", type=Path, default=None, help="override data directory")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="log in to LinkedIn once in the agent browser")
    s = sub.add_parser("scan", help="read the feed, screen posts, refresh the queue")
    s.add_argument("--from-file", help="process extractor-shaped JSON instead of the live feed")
    s.add_argument("--max-posts", type=int)
    sub.add_parser("reclassify", help="re-judge stored posts after editing the field profile")
    q = sub.add_parser("queue", help="export or update the connection queue")
    q.add_argument("action", nargs="?", default="export", choices=["export", "mark"])
    q.add_argument("target", nargs="?")
    q.add_argument("status", nargs="?")
    q.add_argument("--all", action="store_true", help="include sent/skipped people")
    d = sub.add_parser("digest", help="Codex selects, sources, and summarizes; NotebookLM podcast")
    d.add_argument("--no-audio", action="store_true", help="skip the NotebookLM Audio Overview")
    pc = sub.add_parser("podcast", help="(re)generate the NotebookLM podcast for a digest")
    pc.add_argument(
        "folder", nargs="?", help="digest folder name, e.g. 2026-11-19 (default: latest)"
    )
    sub.add_parser(
        "notebooklm-login", help="sign in to Google once in the agent's NotebookLM browser"
    )
    sub.add_parser("email-setup", help="store the Gmail App Password in the OS credential store")
    em = sub.add_parser("email", help="email the latest digest, queue, and podcast to yourself")
    em.add_argument("folder", nargs="?", help="digest folder name (default: latest)")
    em.add_argument("--today-only", action="store_true", help="skip digests from earlier days")
    em.add_argument("--test", action="store_true", help="send a short test message only")
    ob = sub.add_parser("onboard", help="set up the field profile from a CV (the panel's wizard)")
    ob.add_argument("step", choices=["draft", "calibrate", "refine"])
    ob.add_argument("--source", choices=["live", "stored"], default="live")
    ob.add_argument("--max-posts", type=int, default=40)
    cn = sub.add_parser("connections", help="import LinkedIn's Connections.csv export")
    cn.add_argument("action", choices=["import"])
    cn.add_argument("file", help="LinkedIn data export ZIP, Connections.csv, or Invitations.csv")
    st = sub.add_parser("setup", help="install or sign in to a prerequisite")
    st.add_argument("what", choices=["config", "model", "chromium", "codex", "codex-login"])
    sub.add_parser("daily", help="scan, digest, email; isolated stages and a run record")
    sub.add_parser("doctor", help="check that every prerequisite is installed and signed in")
    ui = sub.add_parser("ui", help="open the local control panel")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    sub.add_parser("demo", help="offline demo on synthetic posts")
    sub.add_parser("status", help="show database counts and the last run")
    return p


HANDLERS = {
    "login": cmd_login,
    "scan": cmd_scan,
    "reclassify": cmd_reclassify,
    "queue": cmd_queue,
    "digest": cmd_digest,
    "podcast": cmd_podcast,
    "notebooklm-login": cmd_notebooklm_login,
    "email-setup": cmd_email_setup,
    "onboard": cmd_onboard,
    "connections": cmd_connections,
    "setup": cmd_setup,
    "email": cmd_email,
    "daily": cmd_daily,
    "doctor": cmd_doctor,
    "ui": cmd_ui,
    "demo": cmd_demo,
    "status": cmd_status,
}


def main(argv: list[str] | None = None) -> int:
    # The scheduled task redirects output to a log file, which Windows would encode as cp1252;
    # digests contain characters such as "→" and "⁺", so force UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.data_dir:
        cfg.data_dir = args.data_dir.resolve()
    try:
        return HANDLERS[args.cmd](cfg, args)
    except (UserError, LockHeld) as e:
        print(f"radar: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
