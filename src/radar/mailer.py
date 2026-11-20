"""Daily email to the owner: digest in the body, queue, script, and podcast attached.

Sends through Gmail SMTP with an App Password. The password lives in the OS credential store
(Windows Credential Manager via `keyring`), never in the repository or config files. Set it
once with `radar email-setup`.
"""

from __future__ import annotations

import datetime as dt
import json
import mimetypes
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from .config import EmailCfg

KEYRING_SERVICE = "research-radar-smtp"
MAX_ATTACH_BYTES = 18 * 1024 * 1024  # Gmail caps messages at 25 MB after base64 (+33%)


def get_password(username: str) -> str | None:
    import keyring

    return keyring.get_password(KEYRING_SERVICE, username)


def set_password(username: str, password: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, username, password)


def md_to_html(text: str) -> str:
    try:
        import markdown
    except ImportError:  # plain fallback keeps email working without the extra
        import html

        return f"<pre style='white-space:pre-wrap;font-family:sans-serif'>{html.escape(text)}</pre>"
    body = markdown.markdown(text, extensions=["tables"])
    return (
        "<div style='font-family:Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.5;"
        "max-width:760px'>" + body + "</div>"
    )


def pick_attachments(
    candidates: list[Path], limit: int = MAX_ATTACH_BYTES
) -> tuple[list[Path], list[str]]:
    """Attach existing files in order until the size budget runs out. Returns (attached, notes)."""
    attached, notes, total = [], [], 0
    for p in candidates:
        if not p.exists():
            continue
        size = p.stat().st_size
        if total + size > limit:
            notes.append(f"{p.name} ({size / 1e6:.1f} MB) was too large to attach; it is at {p}")
            continue
        attached.append(p)
        total += size
    return attached, notes


def build_message(
    cfg: EmailCfg, subject: str, body_md: str, attachments: list[Path]
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.sender or cfg.to
    msg["To"] = cfg.to
    msg.set_content(body_md)
    msg.add_alternative(md_to_html(body_md), subtype="html")
    for p in attachments:
        ctype, _ = mimetypes.guess_type(p.name)
        if p.suffix == ".md":
            ctype = "text/markdown"
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    return msg


def _build_line(digest_folder: Path, repo: str = "") -> str:
    """One line about the day's build, from build.json only; nothing else in it is used."""
    try:
        data = json.loads((digest_folder / "build.json").read_text(encoding="utf-8"))
        status = data.get("status")
        if status in ("published", "did_not_run"):
            line = f"**Daily build:** {data['package']} → {data['url']}"
            return line + (" (did not run; see the notebook)" if status == "did_not_run" else "")
        if status == "building":
            run_id = data.get("run_id")
            if (
                isinstance(run_id, int)
                and not isinstance(run_id, bool)
                and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            ):
                return f"Daily build still running: https://github.com/{repo}/actions/runs/{run_id}"
            return "Daily build still running"
        if status in ("skipped", "failed", "blocked"):
            return f"Daily build {status}: {data.get('reason', '')}".rstrip(": ")
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    return ""


def _podcast_line(digest_folder: Path) -> str:
    """ "Podcast online: <page>" from podcast.json, only for a published GitHub release page."""
    try:
        data = json.loads((digest_folder / "podcast.json").read_text(encoding="utf-8"))
        page = data.get("page")
        if data.get("status") == "published" and str(page).startswith("https://github.com/"):
            return f"Podcast online: {page}"
    except (OSError, ValueError, AttributeError):
        pass
    return ""


def compose_daily(
    cfg: EmailCfg,
    digest_folder: Path | None,
    queue_dir: Path,
    today: dt.date | None = None,
    build_repo: str = "",
) -> EmailMessage:
    today = today or dt.date.today()
    queue_md = queue_dir / "connect_queue.md"
    parts: list[str] = []
    candidates: list[Path] = []
    if digest_folder is not None and (digest_folder / "summary.md").exists():
        link_file = digest_folder / "notebook_url.txt"
        if link_file.exists():
            parts.append(
                f"**Podcast:** attached, and playable in NotebookLM: "
                f"<{link_file.read_text(encoding='utf-8').strip()}>"
            )
        podcast_line = _podcast_line(digest_folder)
        if podcast_line:
            parts.append(podcast_line)
        build_line = _build_line(digest_folder, build_repo)
        if build_line:
            parts.append(build_line)
        parts.append((digest_folder / "summary.md").read_text(encoding="utf-8"))
        audio = sorted(
            digest_folder.glob("episode.*"),
            key=lambda p: (
                [".mp3", ".m4a", ".wav"].index(p.suffix.lower())
                if p.suffix.lower() in (".mp3", ".m4a", ".wav")
                else 9
            ),
        )
        candidates += audio[:1] + [digest_folder / "summary.md"]
    else:
        parts.append(
            f"# Research Radar, {today.isoformat()}\n\nNo new saved posts for a digest today."
        )
    if queue_md.exists():
        parts.append(queue_md.read_text(encoding="utf-8"))
        candidates += [queue_dir / "connect_queue.csv"]
    attached, notes = pick_attachments(candidates)
    if notes:
        parts.append("\n".join(f"- {n}" for n in notes))
    subject = f"{cfg.subject_prefix} {today.isoformat()}"
    return build_message(cfg, subject, "\n\n---\n\n".join(parts), attached)


def send(cfg: EmailCfg, msg: EmailMessage, smtp_factory=smtplib.SMTP) -> None:
    user = cfg.sender or cfg.to
    password = get_password(user)
    if not password:
        raise RuntimeError(f"No SMTP password stored for {user}. Run `radar email-setup` first.")
    with smtp_factory(cfg.smtp_host, cfg.smtp_port, timeout=60) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, password)
        s.send_message(msg)
