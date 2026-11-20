"""SQLite store for posts, authors, and digests."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from .connections import profile_slug
from .models import RawPost, Verdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS authors (
    url         TEXT PRIMARY KEY,
    name        TEXT,
    kind        TEXT,
    headline    TEXT,
    degree      TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'todo'   -- todo | sent | skip | connected
);
CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY,
    card_key    TEXT,
    text_hash   TEXT UNIQUE NOT NULL,
    author_url  TEXT,
    author_name TEXT,
    text        TEXT NOT NULL,
    links       TEXT NOT NULL DEFAULT '[]',    -- JSON list of {url, kind}
    tier        TEXT NOT NULL,
    kind        TEXT,
    topics      TEXT NOT NULL DEFAULT '[]',
    summary     TEXT,
    reason      TEXT,
    seen_at     TEXT NOT NULL,
    digest_id   INTEGER REFERENCES digests(id)
);
CREATE TABLE IF NOT EXISTS digests (
    id          INTEGER PRIMARY KEY,
    day         TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    folder      TEXT NOT NULL,
    item_count  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS connections (
    id          INTEGER PRIMARY KEY,
    slug        TEXT UNIQUE,                   -- linkedin.com/in/<slug>; NULL if the export had no URL
    name        TEXT,
    url         TEXT,
    status      TEXT NOT NULL,                 -- connected | pending
    source      TEXT NOT NULL,                 -- export | feed | panel
    since       TEXT,
    updated     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_tier ON posts(tier);
CREATE INDEX IF NOT EXISTS posts_author ON posts(author_url);
"""


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("slug", 1, profile_slug, deterministic=True)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # posts -------------------------------------------------------------
    def has_post(self, post: RawPost) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM posts WHERE text_hash = ?", (post.text_hash,)
        ).fetchone()
        return row is not None

    def add_post(self, post: RawPost, verdict: Verdict, links: list[dict]) -> int | None:
        """Insert a classified post. Returns the row id, or None if it was already stored."""
        now = _now()
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO posts
               (card_key, text_hash, author_url, author_name, text, links, tier, kind, topics,
                summary, reason, seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                post.key,
                post.text_hash,
                post.author_url,
                post.author,
                post.text,
                json.dumps(links),
                verdict.tier,
                verdict.kind,
                json.dumps(verdict.topics),
                verdict.summary,
                verdict.reason,
                now,
            ),
        )
        self.upsert_author(post, commit=False)
        self.conn.commit()
        return cur.lastrowid if cur.rowcount else None

    def upsert_author(self, post: RawPost, commit: bool = True) -> None:
        """Insert or refresh the post's author. A 1st-degree sighting marks them connected.
        Called for duplicate posts too, so degree changes are picked up on re-sightings."""
        if not post.author_url:
            return
        now = _now()
        self.conn.execute(
            """INSERT INTO authors (url, name, kind, headline, degree, first_seen, last_seen, status)
               VALUES (?,?,?,?,?,?,?, CASE WHEN ? = '1st' THEN 'connected' ELSE 'todo' END)
               ON CONFLICT(url) DO UPDATE SET
                 name = COALESCE(excluded.name, authors.name),
                 kind = CASE WHEN excluded.kind <> 'unknown' THEN excluded.kind ELSE authors.kind END,
                 headline = COALESCE(excluded.headline, authors.headline),
                 degree = COALESCE(excluded.degree, authors.degree),
                 last_seen = excluded.last_seen,
                 status = CASE WHEN excluded.degree = '1st' THEN 'connected'
                               ELSE authors.status END""",
            (
                post.author_url,
                post.author,
                post.author_kind,
                post.headline,
                post.degree,
                now,
                now,
                post.degree,
            ),
        )
        if post.degree == "1st":
            self.record_connection(post.author_url, "connected", "feed", name=post.author)
        self.sync_connections()
        if commit:
            self.conn.commit()

    def update_verdict(
        self, post_id: int, verdict: Verdict, links: list[dict] | None = None
    ) -> None:
        sets = "tier=?, kind=?, topics=?, summary=?, reason=?"
        vals: list = [
            verdict.tier,
            verdict.kind,
            json.dumps(verdict.topics),
            verdict.summary,
            verdict.reason,
        ]
        if links is not None:
            sets += ", links=?"
            vals.append(json.dumps(links))
        self.conn.execute(f"UPDATE posts SET {sets} WHERE id=?", [*vals, post_id])
        self.conn.commit()

    def saved_posts(
        self, undigested_only: bool = False, since_days: float | None = None
    ) -> list[sqlite3.Row]:
        q = "SELECT * FROM posts WHERE tier IN ('core','adjacent')"
        params: list[str] = []
        if undigested_only:
            q += " AND digest_id IS NULL"
        if since_days is not None:
            cutoff = dt.datetime.now() - dt.timedelta(days=since_days)
            q += " AND seen_at >= ?"
            params.append(cutoff.isoformat(timespec="seconds"))
        q += " ORDER BY CASE tier WHEN 'core' THEN 0 ELSE 1 END, seen_at DESC"
        return self.conn.execute(q, params).fetchall()

    def stats(self) -> dict:
        rows = self.conn.execute("SELECT tier, COUNT(*) n FROM posts GROUP BY tier").fetchall()
        return {r["tier"]: r["n"] for r in rows}

    # authors / queue ---------------------------------------------------
    def queue_rows(self, include_done: bool = False) -> list[sqlite3.Row]:
        """People (not companies) behind saved posts who are not connections. With
        include_done, people you marked yourself (sent, skipped, already connected) are listed
        too, so they can be undone; connections known from the export or the feed are not."""
        status_filter = (
            """AND NOT EXISTS (SELECT 1 FROM connections c WHERE c.slug = slug(a.url)
                 AND c.status = 'connected' AND c.source <> 'panel')"""
            if include_done
            else "AND a.status = 'todo'"
        )
        return self.conn.execute(
            f"""
            SELECT a.url, a.name, a.headline, a.degree, a.status, a.first_seen, a.last_seen,
                   COUNT(p.id) AS saved_posts,
                   SUM(CASE WHEN p.tier='core' THEN 1 ELSE 0 END) AS core_posts,
                   (SELECT p2.summary FROM posts p2 WHERE p2.author_url = a.url
                      AND p2.tier IN ('core','adjacent') ORDER BY p2.seen_at DESC LIMIT 1) AS latest_summary,
                   (SELECT p2.topics FROM posts p2 WHERE p2.author_url = a.url
                      AND p2.tier IN ('core','adjacent') ORDER BY p2.seen_at DESC LIMIT 1) AS latest_topics
            FROM authors a
            JOIN posts p ON p.author_url = a.url AND p.tier IN ('core','adjacent')
            WHERE a.kind = 'person' AND COALESCE(a.degree,'') <> '1st' {status_filter}
            GROUP BY a.url
            ORDER BY core_posts DESC, saved_posts DESC, a.last_seen DESC
            """
        ).fetchall()

    def set_author_status(self, url_or_name: str, status: str) -> int:
        """Mark a person in the queue. Sent records a pending invitation and connected records
        a connection, so the person stays out of the queue even if a later scan sees them
        again; todo (Undo) forgets both."""
        match = "url = ? OR url = ? OR lower(name) = lower(?)"
        args = (url_or_name, url_or_name.rstrip("/") + "/", url_or_name)
        rows = self.conn.execute(f"SELECT url, name FROM authors WHERE {match}", args).fetchall()
        cur = self.conn.execute(f"UPDATE authors SET status = ? WHERE {match}", (status, *args))
        for r in rows:
            if status == "sent":
                self.record_connection(r["url"], "pending", "panel", name=r["name"])
            elif status == "connected":
                self.record_connection(r["url"], "connected", "panel", name=r["name"])
            elif status == "todo" and profile_slug(r["url"]):
                self.conn.execute(
                    "DELETE FROM connections WHERE slug = ?", (profile_slug(r["url"]),)
                )
        self.conn.commit()
        return cur.rowcount

    # connections -------------------------------------------------------
    def record_connection(
        self, url: str | None, status: str, source: str, name: str | None = None, since: str = ""
    ) -> None:
        """Add or update one person. A connection is never downgraded to pending."""
        slug = profile_slug(url)
        if slug is None:
            if status == "connected" and name and source == "export":
                self.conn.execute(
                    "INSERT INTO connections (slug, name, url, status, source, since, updated) "
                    "VALUES (NULL, ?, ?, ?, ?, ?, ?)",
                    (name, url or "", status, source, since, _now()),
                )
            return
        self.conn.execute(
            """INSERT INTO connections (slug, name, url, status, source, since, updated)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET
                 name = COALESCE(NULLIF(excluded.name, ''), connections.name),
                 url = excluded.url,
                 status = CASE WHEN connections.status = 'connected' THEN 'connected'
                               ELSE excluded.status END,
                 source = CASE WHEN connections.status = 'connected'
                                AND excluded.status = 'pending' THEN connections.source
                               ELSE excluded.source END,
                 since = COALESCE(NULLIF(excluded.since, ''), connections.since),
                 updated = excluded.updated""",
            (slug, name or "", url, status, source, since, _now()),
        )

    def import_connections(self, connections: list[dict], pending: list[dict]) -> dict:
        """Load LinkedIn's export. Returns counts, including queue people it removed."""
        before = len(self.queue_rows())
        # rows without a profile URL are matched by name; replace the old set wholesale
        self.conn.execute("DELETE FROM connections WHERE slug IS NULL AND source = 'export'")
        for c in connections:
            self.record_connection(c["url"], "connected", "export", c["name"], c["since"])
        for p in pending:
            self.record_connection(p["url"], "pending", "export", p["name"], p["since"])
        self.sync_connections()
        self.conn.commit()
        return {
            "connections": len(connections),
            "pending": len(pending),
            "removed_from_queue": before - len(self.queue_rows()),
        }

    def sync_connections(self) -> None:
        """Apply the connections table to authors still in the queue."""
        self.conn.execute(
            """UPDATE authors SET status = 'connected'
               WHERE status IN ('todo', 'sent') AND kind <> 'company' AND (
                 slug(url) IN (SELECT slug FROM connections
                               WHERE status = 'connected' AND slug IS NOT NULL)
                 OR lower(name) IN (SELECT lower(name) FROM connections
                                    WHERE status = 'connected' AND slug IS NULL AND name <> ''))"""
        )
        self.conn.execute(
            """UPDATE authors SET status = 'sent'
               WHERE status = 'todo' AND slug(url) IN
                 (SELECT slug FROM connections WHERE status = 'pending')"""
        )

    def connection_summary(self) -> dict:
        counts = dict(
            self.conn.execute("SELECT status, COUNT(*) FROM connections GROUP BY status").fetchall()
        )
        last = self.conn.execute(
            "SELECT MAX(updated) FROM connections WHERE source = 'export'"
        ).fetchone()[0]
        return {
            "connected": counts.get("connected", 0),
            "pending": counts.get("pending", 0),
            "last_import": last,
        }

    # digests -----------------------------------------------------------
    def record_digest(self, day: str, folder: str, post_ids: list[int]) -> int:
        cur = self.conn.execute(
            "INSERT INTO digests (day, created_at, folder, item_count) VALUES (?,?,?,?)",
            (day, _now(), folder, len(post_ids)),
        )
        digest_id = cur.lastrowid
        self.conn.executemany(
            "UPDATE posts SET digest_id = ? WHERE id = ?", [(digest_id, pid) for pid in post_ids]
        )
        self.conn.commit()
        return digest_id
