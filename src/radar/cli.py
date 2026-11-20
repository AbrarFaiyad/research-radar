"""Research Radar command line.

Pipeline:  scan | reclassify | digest | podcast [--publish] | email | build | daily
Setup:     onboard | setup | connections | login | notebooklm-login | email-setup | builds-key | doctor
Inspect:   status | queue | demo | ui

Everything here also has a button in the control panel (`radar ui`); the panel runs these
commands as subprocesses, and so does the daily scheduled task.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as dt
import json
import shutil
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .classify import Classifier
from .config import Config, load_config
from .db import Store
from .llm import OllamaModel
from .pipeline import LockHeld, RunLock, StageSkipped, run_stages, save_run


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


# The daily run waits for the workflow up to `builds.poll_min`, plus setup and a grace period;
# a run still going after that stays `building` and `radar build --sync` finishes it later.
BUILD_SETUP_MIN = 10


def _sync_builds(cfg: Config) -> None:
    """Finish unfinished builds; a failure here never stops anything else."""
    from .builds import run as build_run

    try:
        build_run.sync_builds(cfg, log=print)
    except Exception as e:  # noqa: BLE001
        print(f"builds: sync crashed: {e.__class__.__name__}")


class DailyBuild:
    """The build that runs beside the podcast in the daily run (mode = with_digest).

    It runs inside the daily command's run lock (same process), so it takes no lock itself.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.thread: threading.Thread | None = None
        self.record = None
        self.day = dt.date.today().isoformat()

    def start(self, folder: Path, result: dict) -> None:
        # The podcast reads `result` at the same time, so the build gets its own copy.
        self.thread = threading.Thread(
            target=self._run, args=(folder, copy.deepcopy(result)), daemon=True
        )
        self.thread.start()

    def _run(self, folder: Path, result: dict) -> None:
        from .builds import run as build_run

        _sync_builds(self.cfg)  # finish unfinished builds first
        try:
            self.record = build_run.run_build(self.cfg, folder, result, log=print)
        except Exception as e:  # noqa: BLE001 - a build must never break the daily run
            print(f"builds: crashed: {e.__class__.__name__}")
            self.record = build_run.BuildRecord(self.day, "failed", reason="build crashed")

    def stage(self) -> dict:
        from .builds.run import BuildRecord

        if self.thread is None:
            _sync_builds(self.cfg)  # no new digest today, but unfinished builds still get closed
            raise StageSkipped("no new digest to build from")
        budget_min = self.cfg.builds.poll_min + BUILD_SETUP_MIN
        self.thread.join(max(budget_min, 0) * 60)
        record = self.record
        if self.thread.is_alive() or record is None:
            # Not finished: keep the run if one was started (sync finishes it later).
            from .builds.run import read_record

            started = read_record(self.cfg, self.day)
            if started is not None and started.status == "building" and started.run_id:
                record = started
            else:
                record = BuildRecord(self.day, "failed", reason="build did not finish")
        if record.status == "skipped":
            raise StageSkipped(record.reason)
        if record.status in ("failed", "blocked"):
            raise RuntimeError(f"build {record.status}: {record.reason}")
        return asdict(record)


def stage_digest(cfg: Config, no_audio: bool = False, after_codex: Callable | None = None) -> dict:
    from .digest import run_digest

    store = Store(cfg.db_path)
    res = run_digest(
        cfg,
        store,
        make_audio=None if no_audio else _audio_maker(cfg),
        after_codex=after_codex,
    )
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


def stage_podcast_publish(cfg: Config, folder_name: str | None = None) -> dict:
    """Publish the digest's episode as a GitHub release. A skip raises StageSkipped, a failure
    RuntimeError; both carry fixed phrases."""
    from . import podcasts

    folder = cfg.digests_dir / folder_name if folder_name else _latest_digest(cfg)
    if folder is None or not (folder / "codex_result.json").exists():
        raise StageSkipped("no new digest to publish")
    result = json.loads((folder / "codex_result.json").read_text(encoding="utf-8"))
    rec = podcasts.publish(cfg, folder, result)
    if rec["status"] == "skipped":
        raise StageSkipped(rec.get("reason", "skipped"))
    if rec["status"] == "failed":
        raise RuntimeError(f"podcast publish failed: {rec.get('reason', '')}")
    print(f"podcast: published {rec.get('page', '')}")
    return rec


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
    msg = compose_daily(cfg.email, folder, cfg.queue_dir, build_repo=cfg.builds.repo)
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
    if args.init_repo:
        from . import podcasts
        from .builds.actions import ActionsError

        with RunLock(cfg, owner="podcast"):
            try:
                print(f"podcast: repo ready: {podcasts.init_repo(cfg)}")
            except ActionsError as e:
                raise UserError(str(e)) from None
        return 0
    if args.publish:
        with RunLock(cfg, owner="podcast"):
            try:
                stage_podcast_publish(cfg, args.folder)
            except StageSkipped as e:
                print(f"podcast: skipped: {e}")
            except RuntimeError as e:
                raise UserError(str(e)) from None
        return 0
    return _locked(cfg, "podcast", lambda: stage_podcast(cfg, args.folder))


def cmd_email(cfg, args) -> int:
    return _locked(cfg, "email", lambda: stage_email(cfg, args.folder, args.today_only, args.test))


# Separate mode: the scheduled build may start while the daily run still holds the lock.
BUILD_LOCK_POLL_S = 60
BUILD_LOCK_WAIT_S = 2 * 3600
DAILY_BUSY = "daily run still in progress"


def _build_lock(cfg: Config, wait_for_daily: bool) -> RunLock | None:
    """Take the run lock for a build. With `wait_for_daily`, a lock held by the daily run is
    polled every BUILD_LOCK_POLL_S for up to BUILD_LOCK_WAIT_S; None means the wait ran out.
    Any other holder raises LockHeld at once."""
    deadline = time.monotonic() + BUILD_LOCK_WAIT_S
    while True:
        lock = RunLock(cfg, owner="build")
        try:
            return lock.__enter__()
        except LockHeld:
            held = lock.holder()
            if not wait_for_daily or (held is not None and held.get("owner") != "daily"):
                raise
            if time.monotonic() >= deadline:
                return None
            time.sleep(BUILD_LOCK_POLL_S)


def _print_build(rec) -> None:
    detail = rec.url if rec.status == "published" else rec.reason
    print(" ".join(x for x in ("build:", rec.status, rec.package, detail) if x))


def cmd_build(cfg, args) -> int:
    from .builds import repo_init
    from .builds import run as build_run
    from .builds.actions import ActionsError

    if args.init_repo:
        with RunLock(cfg, owner="build"):
            try:
                print(f"build: daily-builds repo ready: {repo_init.init_repo(cfg)}")
            except ActionsError as e:
                raise UserError(str(e)) from None
        return 0
    if args.sync:
        with RunLock(cfg, owner="build"):
            finished = build_run.sync_builds(cfg)
        for rec in finished:
            _print_build(rec)
        if not finished:
            print("build: nothing to sync")
        return 1 if any(r.status in ("failed", "blocked") for r in finished) else 0
    # The scheduled build of separate mode builds from today's digest only.
    separate = cfg.builds.mode == "separate" and not args.folder and not args.retry
    today = dt.date.today().isoformat()
    if args.folder:
        folder = cfg.digests_dir / args.folder
    else:
        folder = _latest_digest(cfg, today_only=separate)
        if folder is None and separate:
            _print_build(build_run.record_skip(cfg, today, "no digest today"))
            return 0
    if folder is None or not (folder / "codex_result.json").exists():
        raise UserError("No finished digest found.")
    marker = folder / "build.json"
    if marker.exists() and not args.retry:
        try:
            status = json.loads(marker.read_text(encoding="utf-8")).get("status", "unknown")
        except (OSError, ValueError, AttributeError):
            status = "unknown"
        print(f"build: {folder.name} already has a record ({status}); use --retry to run again")
        return 0
    result = json.loads((folder / "codex_result.json").read_text(encoding="utf-8"))
    lock = _build_lock(cfg, wait_for_daily=separate)
    if lock is None:
        _print_build(build_run.record_skip(cfg, today, DAILY_BUSY, folder))
        return 0
    try:
        _sync_builds(cfg)  # otherwise a still-unrecorded publish could be built twice
        rec = build_run.run_build(cfg, folder, result, retry=args.retry)
    finally:
        lock.__exit__(None, None, None)
    _print_build(rec)
    return 1 if rec.status in ("failed", "blocked") else 0


def cmd_builds_key(cfg, args) -> int:
    import getpass

    from .builds import repo_init
    from .builds.actions import ActionsError

    print(
        "Run `claude setup-token` on your PC, then paste the Claude subscription token "
        "for daily builds (input is hidden)."
    )
    key = "".join(getpass.getpass("Claude subscription token: ").split())
    if not key:
        raise UserError("No token entered.")
    try:
        repo_init.set_secret(cfg, key)
    except ActionsError as e:
        raise UserError(str(e)) from None
    print(
        "Saved as the repo secret CLAUDE_CODE_OAUTH_TOKEN. "
        "Run the first build with `radar build --retry`."
    )
    return 0


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
    build = DailyBuild(cfg) if cfg.builds.enabled and cfg.builds.mode == "with_digest" else None
    made: dict = {}

    def digest_stage() -> dict:
        made.update(stage_digest(cfg, after_codex=build.start if build else None))
        return made

    stages = {"scan": lambda: stage_scan(cfg), "digest": digest_stage}
    optional: set[str] = set()
    if cfg.podcasts.enabled:

        def publish_stage() -> dict:
            if not made.get("folder"):
                raise StageSkipped("no new digest to publish")
            return stage_podcast_publish(cfg, made["folder"])

        stages["podcast-publish"] = publish_stage
        optional.add("podcast-publish")
    if build:
        stages["build"] = build.stage
        optional.add("build")
    if cfg.email.enabled:  # after the build, so the email can report it
        stages["email"] = lambda: stage_email(cfg, today_only=True)
    with RunLock(cfg, owner="daily"):
        if cfg.builds.enabled and not build:
            _sync_builds(cfg)  # separate mode: the build command syncs too, but not every day
        rec = run_stages(stages, optional=optional)
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


def _stage_text(s: dict) -> str:
    if s.get("skipped"):
        return f"{s['name']} skipped: {s.get('error') or 'skipped'}"
    if s["ok"]:
        return f"{s['name']} ok"
    return f"{s['name']} failed (optional)" if s.get("optional") else f"{s['name']} FAILED"


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
        stages = ", ".join(_stage_text(s) for s in run["stages"])
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
    pc.add_argument(
        "--publish", action="store_true", help="publish the episode as a GitHub release"
    )
    pc.add_argument(
        "--init-repo", action="store_true", help="create the public podcasts repo if missing"
    )
    sub.add_parser(
        "notebooklm-login", help="sign in to Google once in the agent's NotebookLM browser"
    )
    sub.add_parser("email-setup", help="store the Gmail App Password in the OS credential store")
    em = sub.add_parser("email", help="email the latest digest, queue, and podcast to yourself")
    em.add_argument("folder", nargs="?", help="digest folder name (default: latest)")
    em.add_argument("--today-only", action="store_true", help="skip digests from earlier days")
    em.add_argument("--test", action="store_true", help="send a short test message only")
    sub.add_parser(
        "builds-key", help="store the Claude subscription token as the builds repo secret"
    )
    bd = sub.add_parser("build", help="build the day's learning demo from a digest and publish it")
    bd.add_argument("folder", nargs="?", help="digest folder name (default: latest)")
    bd.add_argument("--retry", action="store_true", help="run again for a day that has a record")
    bd.add_argument("--sync", action="store_true", help="finish builds that were still running")
    bd.add_argument(
        "--init-repo", action="store_true", help="write the workflow files into the builds repo"
    )
    ob = sub.add_parser("onboard", help="set up the field profile from a CV (the panel's wizard)")
    ob.add_argument("step", choices=["draft", "calibrate", "refine"])
    ob.add_argument("--source", choices=["live", "stored"], default="live")
    ob.add_argument("--max-posts", type=int, default=40)
    cn = sub.add_parser("connections", help="import LinkedIn's Connections.csv export")
    cn.add_argument("action", choices=["import"])
    cn.add_argument("file", help="LinkedIn data export ZIP, Connections.csv, or Invitations.csv")
    st = sub.add_parser("setup", help="install or sign in to a prerequisite")
    st.add_argument("what", choices=["config", "model", "chromium", "codex", "codex-login"])
    sub.add_parser("daily", help="scan, digest, build, email; isolated stages and a run record")
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
    "build": cmd_build,
    "builds-key": cmd_builds_key,
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
