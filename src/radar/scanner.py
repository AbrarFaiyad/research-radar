"""Feed scan: human-paced scrolling through Playwright MCP, then classify and store."""

from __future__ import annotations

import json
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .classify import Classifier
from .config import Config, ScanCfg
from .db import Store
from .links import resolve_all
from .models import RawPost

# Scrolls whichever element actually scrolls the feed. On the 2026 LinkedIn layout that is
# <main id="workspace">, not the window.
SCROLL_JS = """() => {
  const px = %d;
  const main = document.querySelector('main#workspace');
  const target = (main && main.scrollHeight > main.clientHeight + 50) ? main : document.scrollingElement;
  const before = target.scrollTop;
  target.scrollBy({top: px, behavior: 'smooth'});
  return {before, height: target.scrollHeight};
}"""

LOGIN_CHECK_JS = """() => ({
  url: location.href,
  feed: !!document.querySelector('div[role="listitem"][componentkey^="update-card-focus"]'),
  login: !!document.querySelector('input[name="session_key"], form.login__form, #username')
})"""


def extractor_js() -> str:
    return resources.files("radar.browser").joinpath("extract_feed.js").read_text(encoding="utf-8")


@dataclass
class ScanReport:
    seen: int = 0
    new: int = 0
    saved: int = 0
    skipped: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    saved_titles: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"seen {self.seen}, new {self.new}, saved {self.saved}, skipped {self.skipped}, "
            f"duplicates {self.duplicates}, errors {len(self.errors)}"
        )


class Collector:
    """Dedupes raw cards across scroll steps."""

    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.hashes: set[str] = set()
        self.posts: list[RawPost] = []

    def add(self, records: Iterable[dict]) -> int:
        added = 0
        for rec in records or []:
            post = RawPost.from_dict(rec)
            if (post.key and post.key in self.keys) or post.text_hash in self.hashes:
                continue
            if post.key:
                self.keys.add(post.key)
            self.hashes.add(post.text_hash)
            self.posts.append(post)
            added += 1
        return added


async def collect_feed(
    evaluate: Callable[[str], Awaitable[object]],
    wait: Callable[[float], Awaitable[None]],
    cfg: ScanCfg,
    rng: random.Random | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[RawPost]:
    """Scroll and extract until a stop condition. `evaluate` and `wait` come from BrowserMCP
    (or a fake in tests)."""
    rng = rng or random.Random()
    js = extractor_js()
    col = Collector()
    start = clock()
    stale = 0
    while True:
        added = col.add(await evaluate(js))  # type: ignore[arg-type]
        stale = 0 if added else stale + 1
        if len(col.posts) >= cfg.max_posts:
            break
        if stale >= cfg.stale_steps_stop:
            break
        if (clock() - start) / 60 >= cfg.max_minutes:
            break
        await evaluate(SCROLL_JS % rng.randint(cfg.scroll_px_min, cfg.scroll_px_max))
        await wait(round(rng.uniform(cfg.pause_s_min, cfg.pause_s_max), 1))
    return col.posts[: cfg.max_posts]


def process_posts(
    posts: list[RawPost],
    store: Store,
    classify: Classifier,
    resolve: Callable[[list[str], str], list[dict]] = resolve_all,
    log: Callable[[str], None] = print,
) -> ScanReport:
    rep = ScanReport(seen=len(posts))
    for post in posts:
        if store.has_post(post):
            rep.duplicates += 1
            store.upsert_author(post)  # pick up degree changes on re-sightings
            continue
        rep.new += 1
        try:
            verdict = classify(post)
        except Exception as e:  # noqa: BLE001 - model hiccup: record and keep going
            rep.errors.append(f"classify {post.author}: {e}")
            continue
        links = (
            resolve(post.links, post.text)
            if verdict.saved
            else [{"url": u, "kind": "unresolved"} for u in post.links]
        )
        store.add_post(post, verdict, links)
        if verdict.saved:
            rep.saved += 1
            rep.saved_titles.append(f"[{verdict.tier}] {post.author}: {verdict.summary}")
            log(f"  saved [{verdict.tier}] {post.author}: {verdict.summary}")
        else:
            rep.skipped += 1
    return rep


async def scan_live(
    cfg: Config, store: Store, classify: Classifier, log: Callable[[str], None] = print
) -> ScanReport:
    from .browser.mcp_client import BrowserMCP

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    async with BrowserMCP(
        cfg.browser.mcp_command,
        cfg.browser.mcp_args,
        cfg.profile_dir,
        output_dir=cfg.data_dir / "mcp-output",
        headless=cfg.browser.headless,
    ) as browser:
        await browser.navigate(cfg.browser.feed_url)
        await browser.wait(4)
        state = await browser.evaluate(LOGIN_CHECK_JS)
        if not (isinstance(state, dict) and state.get("feed")):
            raise RuntimeError(
                "LinkedIn feed not visible (not logged in?). Run `radar login` once, then retry. "
                f"Page state: {state}"
            )
        posts = await collect_feed(browser.evaluate, browser.wait, cfg.scan)
    log(f"collected {len(posts)} cards; classifying with {cfg.llm.model}")
    return process_posts(posts, store, classify, log=log)


def scan_file(
    path: Path, store: Store, classify: Classifier, log: Callable[[str], None] = print
) -> ScanReport:
    """Offline mode: process extractor-shaped JSON (used by tests and `radar demo`)."""
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    col = Collector()
    col.add(records)
    return process_posts(
        col.posts,
        store,
        classify,
        resolve=lambda urls, text: [{"url": u, "kind": "fixture"} for u in urls],
        log=log,
    )


async def login_flow(
    cfg: Config, log: Callable[[str], None] = print, timeout_min: float = 10
) -> bool:
    """Open a headed browser on the agent profile and wait for the user to log in by hand."""
    from .browser.mcp_client import BrowserMCP

    args = [a for a in cfg.browser.mcp_args if a != "--headless"]
    async with BrowserMCP(
        cfg.browser.mcp_command,
        args,
        cfg.profile_dir,
        output_dir=cfg.data_dir / "mcp-output",
        headless=False,
    ) as browser:
        await browser.navigate(cfg.browser.feed_url)
        log(
            "A browser window opened. Log in to LinkedIn there (the agent never sees your password)."
        )
        deadline = time.monotonic() + timeout_min * 60
        while time.monotonic() < deadline:
            await browser.wait(5)
            try:
                state = await browser.evaluate(LOGIN_CHECK_JS)
            except RuntimeError:  # page mid-navigation during login redirects
                continue
            if isinstance(state, dict) and state.get("feed"):
                log("Feed visible. Login saved in the agent profile.")
                return True
    log("Timed out waiting for login.")
    return False


def reclassify_stored(
    store: Store,
    classify: Classifier,
    resolve: Callable[[list[str], str], list[dict]] = resolve_all,
    log: Callable[[str], None] = print,
) -> tuple[int, int]:
    """Re-run classification on every stored post, e.g. after editing the field profile.
    Returns (checked, changed). Newly saved posts get their links resolved."""
    rows = store.conn.execute(
        "SELECT p.*, a.kind AS akind, a.headline AS ahead, a.degree AS adeg FROM posts p "
        "LEFT JOIN authors a ON a.url = p.author_url ORDER BY p.id"
    ).fetchall()
    changed = 0
    for r in rows:
        if r["kind"] == "ad":
            continue
        old_links = json.loads(r["links"] or "[]")
        post = RawPost(
            key=r["card_key"] or "",
            author=r["author_name"],
            author_url=r["author_url"],
            author_kind=r["akind"] or "unknown",
            degree=r["adeg"],
            headline=r["ahead"],
            text=r["text"],
            links=[x["url"] for x in old_links],
        )
        verdict = classify(post)
        if verdict.tier == r["tier"]:
            continue
        changed += 1
        needs_links = verdict.saved and any(x.get("kind") == "unresolved" for x in old_links)
        links = resolve(post.links, post.text) if needs_links else None
        store.update_verdict(r["id"], verdict, links)
        log(f"  {r['tier']} -> {verdict.tier}: {post.author}: {verdict.summary}")
    return len(rows), changed
