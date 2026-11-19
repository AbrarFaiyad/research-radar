"""Daily email to the owner: digest in the body, queue, script, and podcast attached.

Sends through Gmail SMTP with an App Password. The password lives in the OS credential store
(Windows Credential Manager via `keyring`), never in the repository or config files. Set it
once with `radar email-setup`.
"""

from __future__ import annotations

import datetime as dt
import mimetypes
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


def pick_attachments(candidates: list[Path], limit: int = MAX_ATTACH_BYTES) -> tuple[list[Path], list[str]]:
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


def build_message(cfg: EmailCfg, subject: str, body_md: str, attachments: list[Path]) -> EmailMessage:
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


def compose_daily(cfg: EmailCfg, digest_folder: Path | None, queue_dir: Path,
                  today: dt.date | None = None) -> EmailMessage:
    today = today or dt.date.today()
    queue_md = queue_dir / "connect_queue.md"
    parts: list[str] = []
    candidates: list[Path] = []
    if digest_folder is not None and (digest_folder / "summary.md").exists():
        parts.append((digest_folder / "summary.md").read_text(encoding="utf-8"))
        candidates += [digest_folder / "episode.mp3", digest_folder / "podcast_script.md",
                       digest_folder / "summary.md"]
        if not (digest_folder / "episode.mp3").exists():
            candidates.insert(1, digest_folder / "episode.wav")
    else:
        parts.append(f"# Research Radar, {today.isoformat()}\n\nNo new saved posts for a digest today.")
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
