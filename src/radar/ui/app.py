"""FastAPI app behind `radar ui`.

Security model: the server binds to 127.0.0.1 only. Every request must carry a loopback Host
header (blocks DNS-rebinding pages), and every state-changing request must carry the per-launch
token in `X-Radar-Token` plus a same-origin `Origin` (blocks other websites posting to the
panel). The panel never sees passwords.
"""

from __future__ import annotations

import json
import re
import secrets
import webbrowser
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .. import __version__, settings
from ..config import Config, load_config
from ..db import Store
from ..pipeline import RunLock, latest_run
from ..queue import queue_records
from . import schedule
from .jobs import ACTIONS, JobBusy, JobRunner

_DIGEST_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")
_PROFILE_MAX = 50_000


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
            extra = ["--max-posts", str(int(payload["max_posts"]))]
        if action in ("podcast", "email") and payload.get("folder"):
            if not _DIGEST_NAME.match(str(payload["folder"])):
                raise HTTPException(400, "bad digest name")
            extra = [str(payload["folder"])]
        try:
            return runner.start(action, extra).summary()
        except JobBusy as e:
            raise HTTPException(409, str(e)) from None

    @app.get("/api/jobs/log")
    def job_log(since: int = 0) -> dict:
        return runner.log(since)

    @app.post("/api/jobs/cancel")
    def cancel_job() -> dict:
        return {"cancelled": runner.cancel()}

    # ---- schedule -------------------------------------------------------------------------
    @app.get("/api/schedule")
    def get_schedule() -> dict:
        return schedule.get()

    @app.post("/api/schedule")
    def set_schedule(payload: dict = Body(...)) -> dict:
        try:
            return schedule.set_schedule(cfg.root, payload.get("time"), payload.get("enabled"))
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

    # ---- queue ----------------------------------------------------------------------------
    @app.get("/api/queue")
    def get_queue(all: bool = False) -> list[dict]:
        store = Store(fresh().db_path)
        try:
            return queue_records(store, include_done=all)
        finally:
            store.close()

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
    import uvicorn

    app = create_app(cfg, port=port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Research Radar control panel: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
