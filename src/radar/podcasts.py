"""Publish each day's podcast episode as a GitHub Release in a public repo.

Only the episode file, a title and the kept items' titles and primary links are published. Every
`gh` call uses an argv list and the allowlisted environment of `radar.builds.actions`. Reasons
written to podcast.json and raised in ActionsError are fixed phrases: never gh output, paths
or tokens.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from radar.builds import actions, scan
from radar.builds.actions import ActionsError

if TYPE_CHECKING:
    from radar.config import Config

TAG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")
REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
UPLOAD_TIMEOUT_S = 900
FAILED = "could not publish the release"
PERSONAL = "release text contains personal information"
README_TEXT = (
    "Daily Research Radar podcast episodes, one release per day. "
    "AI-generated (NotebookLM) discussions of public research news.\n"
)
DESCRIPTION = "Daily Research Radar podcast episodes (AI-generated, NotebookLM)"
BLOCKING = ("personal information", "local path", "secret pattern")
AUDIO_ORDER = (".mp3", ".m4a", ".wav")


def _episode(folder: Path) -> Path | None:
    found = sorted(
        (p for p in folder.glob("episode.*") if p.suffix.lower() in AUDIO_ORDER),
        key=lambda p: AUDIO_ORDER.index(p.suffix.lower()),
    )
    return found[0] if found else None


def _notes(result: dict) -> str:
    lines = []
    for item in result.get("items") or []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())
        link = str(item.get("primary_url") or "").strip()
        if not link.lower().startswith(("http://", "https://")):
            link = ""
        if title:
            lines.append(f"- {title}: {link}" if link else f"- {title}")
    return "\n".join(lines) + "\n"


def _title(tag: str, result: dict) -> str:
    headline = " ".join(str(result.get("headline") or "").split())
    return f"{tag[:10]}: {headline}" if headline else tag[:10]


def _has_personal_info(cfg: Config, title: str, notes: str) -> bool:
    markers = scan.personal_markers(cfg)
    findings = scan.scan_bytes("title.txt", title.encode(), markers)
    findings += scan.scan_bytes("notes.md", notes.encode(), markers)
    return any(f.reason.startswith(BLOCKING) and "high-entropy" not in f.reason for f in findings)


def _write(folder: Path, rec: dict) -> dict:
    path = folder / "podcast.json"
    if rec["status"] != "published":
        try:  # a later skip or failure must not erase a published record
            if json.loads(path.read_text(encoding="utf-8")).get("status") == "published":
                return rec
        except (OSError, ValueError, AttributeError):
            pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return rec


def _skip(folder: Path, tag: str, reason: str) -> dict:
    return _write(folder, {"tag": tag, "status": "skipped", "reason": reason})


def _run(gh: actions.GhFn, args: list[str], **kw) -> subprocess.CompletedProcess[str] | None:
    try:
        proc = gh(args, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc


def publish(cfg: Config, folder: Path, result: dict, gh: actions.GhFn = actions.gh) -> dict:
    """Publish the folder's episode as a release; returns (and writes) podcast.json."""
    repo = cfg.podcasts.repo.strip()
    tag = folder.name
    if not repo:
        return _skip(folder, tag, "no repo set")
    if not TAG_RE.match(tag):
        return _skip(folder, tag, "digest folder name is not a date")
    episode = _episode(folder)
    if episode is None:
        return _skip(folder, tag, "no episode")
    try:
        from radar.podcast import email_ready

        episode = email_ready(episode)
    except Exception:  # noqa: BLE001 - decoder/encoder problems must not leak details
        return _write(
            folder, {"tag": tag, "status": "failed", "reason": "could not prepare episode"}
        )
    title, notes = _title(tag, result), _notes(result)
    if _has_personal_info(cfg, title, notes):
        return _skip(folder, tag, PERSONAL)

    ok = _publish_release(gh, repo, tag, episode, title, notes)
    if not ok:
        return _write(folder, {"tag": tag, "status": "failed", "reason": FAILED})
    return _write(
        folder,
        {
            "tag": tag,
            "url": f"https://github.com/{repo}/releases/download/{tag}/{episode.name}",
            "page": f"https://github.com/{repo}/releases/tag/{tag}",
            "status": "published",
        },
    )


def _publish_release(
    gh: actions.GhFn, repo: str, tag: str, episode: Path, title: str, notes: str
) -> bool:
    view = _run(gh, ["release", "view", tag, "-R", repo, "--json", "assets"])
    if view is not None and view.returncode == 0:
        try:
            names = {a.get("name") for a in json.loads(view.stdout).get("assets") or []}
        except (ValueError, AttributeError):
            return False
        if episode.name in names:
            return True
        proc = _run(
            gh,
            ["release", "upload", tag, str(episode), "-R", repo],
            timeout=UPLOAD_TIMEOUT_S,
        )
    else:
        proc = _run(
            gh,
            [
                "release",
                "create",
                tag,
                str(episode),
                "-R",
                repo,
                "--title",
                title,
                "--notes-file",
                "-",
            ],
            input=notes,
            timeout=UPLOAD_TIMEOUT_S,
        )
    return proc is not None and proc.returncode == 0


def init_repo(cfg: Config, gh: actions.GhFn = actions.gh) -> str:
    """Create the public repo with a short README when missing; returns its URL."""
    repo = cfg.podcasts.repo.strip()
    if not repo or not REPO_RE.match(repo):
        raise ActionsError("no target repo")
    view = _run(gh, ["repo", "view", repo])
    if view is None or view.returncode != 0:
        made = _run(gh, ["repo", "create", repo, "--public", "--description", DESCRIPTION])
        if made is None or made.returncode != 0:
            raise ActionsError("could not create the repo")
    readme = _run(gh, ["api", f"repos/{repo}/contents/README.md"])
    if readme is None or readme.returncode != 0:
        content = base64.b64encode(README_TEXT.encode()).decode()
        put = _run(
            gh,
            [
                "api",
                "-X",
                "PUT",
                f"repos/{repo}/contents/README.md",
                "-f",
                "message=Add README",
                "-f",
                f"content={content}",
            ],
        )
        if put is None or put.returncode != 0:
            raise ActionsError("could not write the README")
    return f"https://github.com/{repo}"
