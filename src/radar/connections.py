"""Who you are already connected to, so the queue never suggests them.

The starting point is LinkedIn's own data export (Settings > Data privacy > Get a copy of your
data): `Connections.csv` lists every connection and `Invitations.csv` lists invitations you
sent that are still pending. After the import the list stays current on its own: marking
someone Sent in the panel records a pending invitation, "Already connected" records a
connection, and any feed post that shows a 1st-degree badge records one too.

Only names, profile URLs, and dates are kept. Email addresses in the export are ignored, and
the uploaded file is read in memory and never saved.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from urllib.parse import unquote, urlparse

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ENTRY_BYTES = 20 * 1024 * 1024
EXPORT_URL = "https://www.linkedin.com/mypreferences/d/download-my-data"

_SLUG = re.compile(r"^/in/([^/]+)")


def profile_slug(url: str | None) -> str | None:
    """The stable key of a personal profile URL: `linkedin.com/in/<slug>`, decoded and
    lower-cased. Company, school, and showcase pages return None."""
    if not url:
        return None
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    m = _SLUG.match(urlparse(url).path)
    if not m:
        return None
    return unquote(m.group(1)).strip().lower() or None


def _rows_after_header(text: str, first_column: str) -> list[dict]:
    """LinkedIn puts a few "Notes:" lines above the header; skip to the real header."""
    lines = text.lstrip("﻿").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(first_column):
            return list(csv.DictReader(lines[i:]))
    raise ValueError(f"no header starting with {first_column!r}")


def parse_connections(text: str) -> list[dict]:
    out = []
    for row in _rows_after_header(text, "First Name"):
        name = " ".join(f"{row.get('First Name', '')} {row.get('Last Name', '')}".split())
        url = (row.get("URL") or "").strip()
        if not name and not url:
            continue
        out.append(
            {
                "name": name,
                "url": url,
                "slug": profile_slug(url),
                "since": (row.get("Connected On") or "").strip(),
            }
        )
    return out


def parse_invitations(text: str) -> list[dict]:
    """Outgoing invitations only (the ones you sent and are waiting on)."""
    out = []
    for row in _rows_after_header(text, "From"):
        if (row.get("Direction") or "").strip().upper() != "OUTGOING":
            continue
        url = (row.get("inviteeProfileUrl") or "").strip()
        out.append(
            {
                "name": (row.get("To") or "").strip(),
                "url": url,
                "slug": profile_slug(url),
                "since": (row.get("Sent At") or "").strip(),
            }
        )
    return out


def read_export(filename: str, data: bytes) -> tuple[list[dict], list[dict]]:
    """Accept LinkedIn's ZIP archive, or Connections.csv / Invitations.csv on their own.
    Returns (connections, pending invitations)."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(
            "file is larger than 25 MB; request only Connections and Invitations from LinkedIn"
        )
    files: dict[str, str] = {}
    if filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for info in z.infolist():
                    base = info.filename.rsplit("/", 1)[-1].lower()
                    if base in ("connections.csv", "invitations.csv"):
                        if info.file_size > MAX_ENTRY_BYTES:
                            raise ValueError(f"{base} in the archive is too large")
                        files[base] = z.read(info).decode("utf-8-sig", errors="replace")
        except zipfile.BadZipFile:
            raise ValueError("not a valid ZIP file") from None
    elif filename.lower().endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        head = text[:4000]
        if "First Name" in head and "Connected On" in head:
            files["connections.csv"] = text
        elif "inviteeProfileUrl" in head:
            files["invitations.csv"] = text
        else:
            raise ValueError("this CSV is not LinkedIn's Connections.csv or Invitations.csv")
    else:
        raise ValueError("upload LinkedIn's ZIP archive or its Connections.csv")
    if "connections.csv" not in files and "invitations.csv" not in files:
        raise ValueError("the archive has no Connections.csv or Invitations.csv")
    conns = parse_connections(files["connections.csv"]) if "connections.csv" in files else []
    pending = parse_invitations(files["invitations.csv"]) if "invitations.csv" in files else []
    return conns, pending
