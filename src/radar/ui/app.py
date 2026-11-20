"""FastAPI app behind `radar ui`.

Security model: the server binds to 127.0.0.1 only. Every request must carry a loopback Host
header (blocks DNS-rebinding pages), and every state-changing request must carry the per-launch
token in `X-Radar-Token` plus a same-origin `Origin` (blocks other websites posting to the
panel). Site passwords (LinkedIn, Google) are typed only into the agent browser. The Gmail App
Password may be typed into the panel; it goes straight to the OS credential store and is never
returned, logged, or written to disk by the app.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import webbrowser
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .. import __version__, onboard, settings
from ..config import Config, load_config
from ..db import Store
from ..pipeline import RunLock, latest_run
from ..queue import queue_records
from . import schedule
from .jobs import ACTIONS, JobBusy, JobRunner

_DIGEST_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")
_PROFILE_MAX = 50_000
_BUILD_ACTIONS = ("build", "build-retry", "build-sync", "build-init-repo")
_KEY_MIN = 20


def create_app(
    cfg: Config, token: str | None = None, port: int = 8765, runner: JobRunner | None = None
) -> FastAPI:
    token = token or secrets.token_urlsafe(24)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "testserver"}
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", "http://testserver"}
    custom_config = cfg.source if cfg.source and cfg.source.name != "config.example.toml" else None
    runner = runner or JobRunner(cfg.root, cfg.data_dir, config=custom_config)
    app = FastAPI(title="Research Radar", version=__version__, docs_url=None, redoc_url=None)
    app.state.token, app.state.runner = token, runner

    def fresh() -> Config:
        c = load_config(custom_config, root=cfg.root)
        c.data_dir = cfg.data_dir
        return c

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "bad host"}, status_code=403)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin is not None and origin not in allowed_origins:
                return JSONResponse({"detail": "bad origin"}, status_code=403)
            if not secrets.compare_digest(request.headers.get("x-radar-token", ""), token):
                return JSONResponse({"detail": "missing or wrong token"}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        html = resources.files("radar.ui").joinpath("static/index.html").read_text(encoding="utf-8")
        return html.replace("__RADAR_TOKEN__", token).replace("__RADAR_VERSION__", __version__)

    # ---- status ---------------------------------------------------------------------------
    @app.get("/api/status")
    def status() -> dict:
        c = fresh()
        store = Store(c.db_path)
        try:
            stats, queue = store.stats(), len(store.queue_rows())
            last = store.conn.execute(
                "SELECT day, folder, item_count FROM digests ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            store.close()
        return {
            "version": __version__,
            "posts": stats,
            "queue": queue,
            "last_digest": dict(last) | {"name": Path(last["folder"]).name} if last else None,
            "last_run": latest_run(c),
            "job": runner.current.summary() if runner.current else None,
            "lock": RunLock(c).holder(),
        }

    @app.get("/api/doctor")
    def doctor() -> list[dict]:
        from ..doctor import run_checks

        return [ch.to_dict() for ch in run_checks(fresh())]

    # ---- jobs -----------------------------------------------------------------------------
    @app.post("/api/jobs")
    def start_job(payload: dict = Body(...)) -> dict:
        action = payload.get("action", "")
        if action not in ACTIONS:
            raise HTTPException(400, f"unknown action {action!r}")
        holder = RunLock(fresh()).holder()
        if holder and not (runner.current and runner.current.running):
            raise HTTPException(409, f"a {holder.get('owner')} run is already active")
        extra = []
        if action == "scan" and payload.get("max_posts"):
            extra = ["--max-posts", str(_bounded_int(payload["max_posts"], 1, 300))]
        if action == "digest" and payload.get("no_audio"):
            extra = ["--no-audio"]
        if action == "onboard-calibrate":
            source = payload.get("source", "live")
            if source not in onboard.SOURCES:
                raise HTTPException(400, "source must be live or stored")
            extra = [
                "--source",
                source,
                "--max-posts",
                str(_bounded_int(payload.get("max_posts", 40), 5, 150)),
            ]
        if action in (
            "podcast",
            "podcast-publish",
            "email",
            "build",
            "build-retry",
        ) and payload.get("folder"):
            if not _DIGEST_NAME.match(str(payload["folder"])):
                raise HTTPException(400, "bad digest name")
            extra = [str(payload["folder"])]
        try:
            return runner.start(action, extra).summary()
        except JobBusy as e:
            raise HTTPException(409, str(e)) from None

    @app.get("/api/runs")
    def runs(limit: int = 20) -> list[dict]:
        out = []
        for path in sorted((fresh().data_dir / "runs").glob("*.json"), reverse=True)[:limit]:
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    @app.get("/api/jobs/log")
    def job_log(since: int = 0) -> dict:
        return runner.log(since)

    @app.post("/api/jobs/cancel")
    def cancel_job() -> dict:
        return {"cancelled": runner.cancel()}

    # ---- schedule -------------------------------------------------------------------------
    @app.get("/api/schedule")
    def get_schedule(task: str = schedule.TASK) -> dict:
        try:
            return schedule.get(task)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/schedule")
    def set_schedule(payload: dict = Body(...)) -> dict:
        try:
            return schedule.set_schedule(
                cfg.root,
                payload.get("time"),
                payload.get("enabled"),
                task=payload.get("task", schedule.TASK),
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        except RuntimeError as e:
            raise HTTPException(500, str(e)) from None

    # ---- settings and profile -------------------------------------------------------------
    @app.get("/api/settings")
    def get_settings() -> dict:
        return {"schema": settings.schema_json(), "values": settings.load(cfg.root)}

    @app.post("/api/settings")
    def save_settings(payload: dict[str, Any] = Body(...)) -> dict:
        try:
            return {"values": settings.save(cfg.root, payload)}
        except settings.SettingsError as e:
            return JSONResponse({"detail": "invalid settings", "errors": e.errors}, status_code=400)

    @app.get("/api/profile")
    def get_profile() -> dict:
        c = fresh()
        return {"path": str(c.field_profile), "text": c.field_profile_text()}

    @app.post("/api/profile")
    def save_profile(payload: dict = Body(...)) -> dict:
        text = str(payload.get("text", ""))
        if not text.strip():
            raise HTTPException(400, "profile cannot be empty")
        if len(text) > _PROFILE_MAX:
            raise HTTPException(400, "profile is too long")
        path = fresh().field_profile
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
        tmp.replace(path)
        return {"saved": True, "chars": len(text)}

    # ---- onboarding -----------------------------------------------------------------------
    def wizard_idle() -> None:
        """Wizard edits wait for a running wizard job, so its result cannot overwrite them."""
        job = runner.current
        if job and job.running and job.action.startswith("onboard"):
            raise HTTPException(409, f"wait for {job.action} to finish")

    @app.get("/api/onboarding")
    def get_onboarding() -> dict:
        c = fresh()
        store = Store(c.db_path)
        try:
            stored = sum(store.stats().values())
        finally:
            store.close()
        state = onboard.load_state(c)
        return {
            "state": state,
            # a fresh install: nothing scanned yet and no profile applied through the wizard
            "first_run": stored == 0 and not state.get("applied_at"),
            "stored_posts": stored,
            "reader": c.profile.reader,
            "profile": c.field_profile_text() if c.field_profile.exists() else "",
        }

    @app.post("/api/onboarding/cv")
    def upload_cv(payload: dict = Body(...)) -> dict:
        wizard_idle()
        try:
            data = base64.b64decode(str(payload.get("data", "")), validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "file data is not valid base64") from None
        c = fresh()
        try:
            cv = onboard.save_cv(c, str(payload.get("filename", "")), data)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        state = onboard.load_state(c)
        state["cv"] = cv
        onboard.save_state(c, state)
        return {"cv": cv}

    @app.post("/api/onboarding/cv/remove")
    def remove_cv() -> dict:
        wizard_idle()
        c = fresh()
        onboard.remove_cv(c)
        state = onboard.load_state(c)
        state["cv"] = None
        onboard.save_state(c, state)
        return {"cv": None}

    @app.post("/api/onboarding/notes")
    def save_notes(payload: dict = Body(...)) -> dict:
        wizard_idle()
        notes = str(payload.get("notes", ""))
        drafter = payload.get("drafter", "codex")
        if len(notes) > 8000:
            raise HTTPException(400, "description is too long (8000 characters max)")
        if drafter not in onboard.DRAFTERS:
            raise HTTPException(400, "drafter must be codex or local")
        c = fresh()
        state = onboard.load_state(c)
        state.update(notes=notes, drafter=drafter)
        onboard.save_state(c, state)
        return {"saved": True}

    @app.post("/api/onboarding/edit")
    def edit_onboarding(payload: dict = Body(...)) -> dict:
        """Keep the user's edits to the draft before testing it on posts."""
        wizard_idle()
        reader = " ".join(str(payload.get("reader", "")).split())
        profile = str(payload.get("profile", "")).replace("\r\n", "\n")
        if not reader or not profile.strip():
            raise HTTPException(400, "reader and profile are both required")
        if len(profile) > onboard.PROFILE_MAX_CHARS or len(reader) > 300:
            raise HTTPException(400, "profile or reader is too long")
        c = fresh()
        state = onboard.load_state(c)
        key = "refined" if state.get("refined") else "draft"
        state[key] = {**(state.get(key) or {}), "reader": reader, "profile": profile}
        onboard.save_state(c, state)
        return {"saved": key}

    @app.post("/api/onboarding/corrections")
    def corrections(payload: dict = Body(...)) -> dict:
        raw = payload.get("corrections", {})
        if not isinstance(raw, dict):
            raise HTTPException(400, "corrections must be an object")
        wizard_idle()
        return {"corrections": onboard.set_corrections(fresh(), raw)}

    @app.post("/api/onboarding/apply")
    def apply_onboarding(payload: dict = Body(...)) -> dict:
        wizard_idle()
        c = fresh()
        try:
            backup = onboard.apply_profile(
                c, str(payload.get("reader", "")), str(payload.get("profile", ""))
            )
        except settings.SettingsError as e:
            raise HTTPException(
                400, f"reader {e.errors.get('profile.reader', 'is invalid')}"
            ) from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"applied": True, "backup": backup.name if backup else None}

    @app.post("/api/onboarding/reset")
    def reset_onboarding() -> dict:
        wizard_idle()
        c = fresh()
        onboard.remove_cv(c)
        onboard.save_state(c, onboard.empty_state())
        return {"reset": True}

    # ---- email password -------------------------------------------------------------------
    @app.post("/api/email/password")
    def email_password(payload: dict = Body(...)) -> dict:
        from ..mailer import set_password

        c = fresh()
        user = c.email.sender or c.email.to
        if not user or user.endswith("example.com"):
            raise HTTPException(400, "set the email address in Settings first")
        pw = "".join(str(payload.get("password", "")).split())
        if not 8 <= len(pw) <= 64:
            raise HTTPException(400, "a Gmail App Password is 16 letters")
        set_password(user, pw)
        return {"saved": True}

    # ---- connections ----------------------------------------------------------------------
    @app.get("/api/connections")
    def connections_summary() -> dict:
        from ..connections import EXPORT_URL

        store = Store(fresh().db_path)
        try:
            return store.connection_summary() | {"export_url": EXPORT_URL}
        finally:
            store.close()

    @app.post("/api/connections/import")
    def import_connections(payload: dict = Body(...)) -> dict:
        """The export is parsed in memory; only names, profile URLs, and dates are stored."""
        from ..connections import read_export
        from ..queue import write_queue

        try:
            data = base64.b64decode(str(payload.get("data", "")), validate=True)
            conns, pending = read_export(str(payload.get("filename", "")), data)
        except binascii.Error:
            raise HTTPException(400, "file data is not valid base64") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        c = fresh()
        store = Store(c.db_path)
        try:
            res = store.import_connections(conns, pending)
            write_queue(store, c.queue_dir)
            return res | store.connection_summary()
        finally:
            store.close()

    # ---- queue ----------------------------------------------------------------------------
    @app.get("/api/queue")
    def get_queue(all: bool = False) -> list[dict]:
        store = Store(fresh().db_path)
        try:
            return queue_records(store, include_done=all)
        finally:
            store.close()

    @app.get("/api/queue.csv")
    def queue_csv(all: bool = False) -> Response:
        """Built in memory: a GET must not rewrite the queue files the daily email attaches."""
        from ..queue import csv_text

        store = Store(fresh().db_path)
        try:
            body = csv_text(queue_records(store, include_done=all))
        finally:
            store.close()
        return Response(
            body.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="connect_queue.csv"'},
        )

    @app.post("/api/queue/mark")
    def mark(payload: dict = Body(...)) -> dict:
        status_ = payload.get("status")
        url = str(payload.get("profile_url", ""))
        if status_ not in ("todo", "sent", "skip", "connected") or not url.startswith("https://"):
            raise HTTPException(400, "need profile_url and status todo|sent|skip|connected")
        c = fresh()
        store = Store(c.db_path)
        try:
            n = store.set_author_status(url, status_)
            from ..queue import write_queue

            write_queue(store, c.queue_dir)
        finally:
            store.close()
        return {"updated": n}

    # ---- daily builds ---------------------------------------------------------------------
    def build_record(day: str) -> dict:
        if not _DIGEST_NAME.match(day):
            raise HTTPException(400, "bad day")
        path = fresh().builds_dir / "records" / f"{day}.json"
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(404, "no build for that day") from None
        if not isinstance(rec, dict):
            raise HTTPException(404, "no build for that day")
        return rec

    def build_links(rec: dict) -> dict:
        """Links to show for a record: the GitHub folder it published, or the live Actions run
        while it is building. Only https://github.com/ addresses and an integer run id pass."""
        links: dict = {"url": "", "run_url": ""}
        url = rec.get("url")
        if isinstance(url, str) and url.startswith("https://github.com/"):
            links["url"] = url
        run_id = rec.get("run_id")
        repo = fresh().builds.repo
        if (
            rec.get("status") == "building"
            and isinstance(run_id, int)
            and not isinstance(run_id, bool)
            and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or "")
        ):
            links["run_url"] = f"https://github.com/{repo}/actions/runs/{run_id}"
        return links

    @app.get("/api/builds")
    def list_builds() -> dict:
        out = []
        for path in sorted((fresh().builds_dir / "records").glob("*.json"), reverse=True):
            if not _DIGEST_NAME.match(path.stem):
                continue
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict):
                out.append({**rec, **build_links(rec)})
        job = runner.current
        running = job.summary() if job and job.running and job.action in _BUILD_ACTIONS else None
        return {"builds": out, "job": running}

    @app.post("/api/builds/key")
    def builds_key(payload: dict = Body(...)) -> dict:
        from ..builds.actions import ActionsError
        from ..builds.repo_init import set_secret

        key = "".join(str(payload.get("key", "")).split())
        if len(key) < _KEY_MIN:
            raise HTTPException(
                400,
                f"that does not look like a Claude subscription token (under {_KEY_MIN} characters)",
            )
        try:
            set_secret(fresh(), key)
        except ActionsError as e:
            raise HTTPException(400, str(e)) from None
        return {"saved": True}

    @app.get("/api/builds/{day}")
    def build_detail(day: str) -> dict:
        rec = build_record(day)
        digests = sorted(
            (p.name for p in fresh().digests_dir.glob(f"{day}*") if _DIGEST_NAME.match(p.name)),
        )
        return {
            "record": {**rec, **build_links(rec)},
            "digest": digests[0] if digests else None,
        }

    # ---- digests --------------------------------------------------------------------------
    def digest_folder(name: str) -> Path:
        if not _DIGEST_NAME.match(name):
            raise HTTPException(400, "bad digest name")
        folder = fresh().digests_dir / name
        if not (folder / "summary.md").exists():
            raise HTTPException(404, "no such digest")
        return folder

    @app.get("/api/digests")
    def list_digests() -> list[dict]:
        out = []
        for folder in sorted(
            fresh().digests_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            if not (folder / "summary.md").exists() or not _DIGEST_NAME.match(folder.name):
                continue
            kept = reviewed = None
            try:
                result = json.loads((folder / "codex_result.json").read_text(encoding="utf-8"))
                kept = len(result.get("items", []))
                reviewed = kept + len(result.get("rejected", []))
                headline = result.get("headline", "")
            except (OSError, ValueError):
                headline = ""
            audio = next(iter(sorted(folder.glob("episode.*"))), None)
            url_file = folder / "notebook_url.txt"
            out.append(
                {
                    "name": folder.name,
                    "headline": headline,
                    "kept": kept,
                    "reviewed": reviewed,
                    "audio": audio.name if audio else None,
                    "notebook": url_file.read_text(encoding="utf-8").strip()
                    if url_file.exists()
                    else None,
                }
            )
        return out

    @app.get("/api/digests/{name}")
    def digest(name: str) -> dict:
        folder = digest_folder(name)
        return {
            "name": name,
            "html": safe_markdown((folder / "summary.md").read_text(encoding="utf-8")),
        }

    @app.get("/api/digests/{name}/audio")
    def digest_audio(name: str):
        folder = digest_folder(name)
        audio = next(iter(sorted(folder.glob("episode.*"))), None)
        if audio is None:
            raise HTTPException(404, "no audio for this digest")
        media = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav"}.get(
            audio.suffix, "audio/mpeg"
        )
        return FileResponse(audio, media_type=media)

    return app


def _bounded_int(value: Any, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"must be a whole number from {lo} to {hi}") from None
    return min(max(n, lo), hi)


_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_BAD_HREF = re.compile(r'href="\s*(javascript|data|vbscript):[^"]*"', re.IGNORECASE)


def safe_markdown(md: str) -> str:
    """Render digest markdown without letting embedded HTML through. Digest text is written by
    Codex from arbitrary web pages, so raw tags and script URLs must not reach the panel."""
    import markdown

    md = _AUTOLINK.sub(lambda m: f"[{m.group(1)}]({m.group(1)})", md)
    md = md.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = markdown.markdown(md, extensions=["tables"])
    html = _BAD_HREF.sub('href="#"', html)
    return html.replace("<a href=", '<a rel="noopener noreferrer" target="_blank" href=')


def serve(cfg: Config, port: int = 8765, open_browser: bool = True) -> None:
    import httpx
    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    try:  # already running (e.g. the shortcut was clicked twice): just open it
        if httpx.get(url + "api/status", timeout=1.5).json().get("version"):
            print(f"Research Radar is already running: {url}")
            if open_browser:
                webbrowser.open(url)
            return
    except (httpx.HTTPError, ValueError):
        pass
    app = create_app(cfg, port=port)
    print(f"Research Radar control panel: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
