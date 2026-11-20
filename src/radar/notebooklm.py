"""NotebookLM Audio Overview: the podcast step.

NotebookLM has no consumer API, so the agent drives notebooklm.google.com in its own browser
profile (separate from the LinkedIn one). Interactive tools work only while the page is on
notebook.google.com (NotebookLM's new home) or notebooklm.google.com; see BrowserMCP's host guard. The user signs in to Google once with
`radar notebooklm-login`; the agent never sees the password.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from pathlib import Path

from .browser.mcp_client import NOTEBOOKLM_TOOLS, BrowserMCP
from .config import Config

HOME = "https://notebook.google.com/"
HOSTS = frozenset(
    {"notebook.google.com", "notebooklm.google.com"}
)  # "Gemini Notebook" since Nov 2026

STATE_JS = """() => ({
  url: location.href,
  host: location.hostname,
  title: document.title,
  signedIn: ['notebook.google.com', 'notebooklm.google.com'].includes(location.hostname) &&
            /create new|new notebook|create notebook/i.test(document.body ? document.body.innerText : '')
})"""


def browser(cfg: Config, headless: bool = False) -> BrowserMCP:
    args = [a for a in cfg.browser.mcp_args if a != "--headless"]
    return BrowserMCP(
        cfg.browser.mcp_command,
        args,
        cfg.data_dir / "browser-profile-google",
        output_dir=cfg.data_dir / "mcp-output-notebooklm",
        headless=headless,
        allowed_tools=NOTEBOOKLM_TOOLS,
        allowed_hosts=HOSTS,
    )


async def login_flow(
    cfg: Config, log: Callable[[str], None] = print, timeout_min: float = 10
) -> bool:
    async with browser(cfg) as b:
        await b.navigate(HOME)
        log(
            "A browser window opened. Sign in to Google there (the agent never sees your password)."
        )
        deadline = time.monotonic() + timeout_min * 60
        while time.monotonic() < deadline:
            await b.wait(5)
            try:
                state = await b.evaluate(STATE_JS)
            except RuntimeError:
                continue
            if isinstance(state, dict) and state.get("signedIn"):
                await b.wait(3)
                log("NotebookLM is open. Google sign-in saved in the agent profile.")
                return True
    log("Timed out waiting for sign-in.")
    return False


def find_ref(snapshot: str, role: str, name: str) -> str:
    """Ref of the first `role "name"` node in a Playwright MCP snapshot; exact name first,
    then a case-insensitive substring match."""
    exact = re.compile(rf'- {re.escape(role)} "{re.escape(name)}"[^\n]*?\[ref=(e\d+)\]')
    loose = re.compile(
        rf'- {re.escape(role)} "[^"\n]*{re.escape(name)}[^"\n]*"[^\n]*?\[ref=(e\d+)\]',
        re.IGNORECASE,
    )
    m = exact.search(snapshot) or loose.search(snapshot)
    if not m:
        raise LookupError(f'{role} "{name}" not found on the NotebookLM page')
    return m.group(1)


def audio_instructions(result: dict, reader: str = "a researcher in this field") -> str:
    focus = (result.get("podcast_focus") or "").strip()
    return (
        f"Audience: {reader.strip()}. Cover every item in the briefing document, most important "
        "first. For each, explain how the work was done and why it matters, and say what it means "
        "for the listener's research. Use the linked sources for detail. " + focus
    )[:1900]


LENGTH_LABEL = {
    "shorter": "Short",
    "short": "Short",
    "default": "Default",
    "longer": "Long",
    "long": "Long",
}


def studio_section(snapshot: str) -> str:
    """The part of the snapshot after the Studio tiles, where generated items appear."""
    i = snapshot.rfind('button "Infographic"')
    return snapshot[i:] if i >= 0 else snapshot


def audio_state(snapshot: str) -> str:
    """'generating', 'ready', or 'unknown' for the newest Audio Overview item."""
    studio = studio_section(snapshot)
    if "Generating Audio Overview" in studio:
        return "generating"
    if re.search(r'button "Play" \[ref=e\d+\]', studio) or "is ready." in snapshot:
        return "ready"
    return "unknown"


def item_more_ref(snapshot: str) -> str:
    """Ref of the More button that follows the first Play button in the Studio list."""
    studio = studio_section(snapshot)
    play = re.search(r'button "Play" \[ref=e\d+\]', studio)
    m = re.search(r'button "More" \[ref=(e\d+)\]', studio[play.end() :] if play else studio)
    if not m:
        raise LookupError("Audio Overview menu button not found")
    return m.group(1)


async def _snap(b: BrowserMCP) -> str:
    return await b.call("browser_snapshot", {})


async def _click(b: BrowserMCP, role: str, name: str, snap: str | None = None) -> None:
    ref = find_ref(snap or await _snap(b), role, name)
    await b.call("browser_click", {"element": name, "target": ref})


def radio_checked(snapshot: str, name: str) -> bool:
    m = re.search(rf'- radio "{re.escape(name)}[^"\n]*"([^\n]*)', snapshot, re.IGNORECASE)
    return bool(m and "[checked]" in m.group(1))


def radio_container_ref(snapshot: str, name: str) -> str | None:
    """Ref of the clickable wrapper just above a radio (Material radios hide the input)."""
    lines = snapshot.splitlines()
    for i, ln in enumerate(lines):
        if re.search(rf'- radio "{re.escape(name)}', ln, re.IGNORECASE):
            for prev in reversed(lines[max(0, i - 3) : i]):
                m = re.search(r"\[ref=(e\d+)\] \[cursor=pointer\]", prev)
                if m:
                    return m.group(1)
    return None


async def _choose_radio(b: BrowserMCP, name: str) -> None:
    snap = await _snap(b)
    if radio_checked(snap, name):
        return
    try:
        await b.call("browser_click", {"element": name, "target": find_ref(snap, "radio", name)})
    except RuntimeError:
        wrapper = radio_container_ref(snap, name)
        if wrapper is None:
            raise
        await b.call("browser_click", {"element": name, "target": wrapper})


async def _wait_for_download(out_dir: Path, since: float, timeout_s: float = 180) -> Path:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        files = [
            p
            for p in out_dir.glob("*")
            if p.suffix.lower() in (".m4a", ".mp3", ".wav")
            and p.stat().st_mtime >= since
            and p.stat().st_size > 0
        ]
        if files:
            newest = max(files, key=lambda p: p.stat().st_mtime)
            size = newest.stat().st_size
            await asyncio.sleep(2)
            if newest.stat().st_size == size:  # finished writing
                return newest
        await asyncio.sleep(2)
    raise TimeoutError("Audio Overview download did not arrive")


async def generate_audio(
    cfg: Config, folder: Path, result: dict, log: Callable[[str], None] = print
) -> Path:
    """Create a notebook from the digest, generate a Deep dive Audio Overview, download it."""
    summary = folder / "summary.md"
    urls = (
        (folder / "sources.txt").read_text(encoding="utf-8").split()
        if (folder / "sources.txt").exists()
        else []
    )
    out_dir = cfg.data_dir / "mcp-output-notebooklm"
    out_dir.mkdir(parents=True, exist_ok=True)
    nlm = cfg.notebooklm

    url_file = folder / "notebook_url.txt"
    async with browser(cfg) as b:
        if url_file.exists():  # retry: reuse the notebook and its sources
            await b.navigate(url_file.read_text(encoding="utf-8").strip())
            await b.wait(6)
        else:
            await _build_notebook(b, summary, urls, url_file, nlm.add_web_sources, log)
        since = await _generate_and_download(b, cfg, result, log)
        downloaded = await _wait_for_download(out_dir, since)

    target = folder / f"episode{downloaded.suffix.lower()}"
    downloaded.replace(target)
    log(f"audio: {target.name} ({target.stat().st_size / 1e6:.1f} MB)")
    return target


async def _build_notebook(
    b: BrowserMCP,
    summary: Path,
    urls: list[str],
    url_file: Path,
    add_web: bool,
    log: Callable[[str], None],
) -> None:
    await b.navigate(HOME)
    await b.wait(5)
    await _click(b, "button", "New notebook")
    await b.wait(6)
    url = await b.evaluate("() => location.href")
    url_file.write_text(str(url), encoding="utf-8")
    log(f"notebook: {url}")

    # Source 1: the Codex digest. The upload must directly follow the click that opens
    # the file chooser (see BrowserMCP host guard).
    snap = await _snap(b)
    if 'button "Upload files"' not in snap:
        await _click(b, "button", "Add source", snap)
        await b.wait(2)
        snap = await _snap(b)
    await _click(b, "button", "Upload files", snap)
    await b.call("browser_file_upload", {"paths": [str(summary.resolve())]})
    await b.wait(8)

    # Sources 2..n: primary papers and coverage Codex found. Paywalled pages fail
    # individually and NotebookLM skips them.
    if add_web and urls:
        await _click(b, "button", "Add source")
        await b.wait(2)
        await _click(b, "button", "Websites")
        await b.wait(2)
        await b.call(
            "browser_type",
            {
                "element": "URL box",
                "target": '[placeholder="Paste any links"]',
                "text": "\n".join(urls),
            },
        )
        await b.wait(1)
        await _click(b, "button", "Insert")
        await b.wait(25)


async def _generate_and_download(
    b: BrowserMCP, cfg: Config, result: dict, log: Callable[[str], None]
) -> float:
    """Generate (unless a finished episode is already there), then start the download.
    Returns the timestamp the download must be newer than."""
    nlm = cfg.notebooklm
    snap = await _snap(b)
    if audio_state(snap) != "ready":
        await _click(b, "button", "Audio Overview", snap)
        await b.wait(3)
        await _choose_radio(b, "Deep dive")
        await _choose_radio(b, LENGTH_LABEL.get(nlm.length, "Default"))
        focus = find_ref(await _snap(b), "textbox", "What should the AI hosts focus on")
        await b.call(
            "browser_type",
            {
                "element": "focus box",
                "target": focus,
                "text": audio_instructions(result, cfg.profile.reader),
            },
        )
        await _click(b, "button", "Generate now")
        log("NotebookLM is generating the Audio Overview (usually 5 to 15 minutes)")
        deadline = time.monotonic() + nlm.timeout_min * 60
        while time.monotonic() < deadline:
            await b.wait(30)
            snap = await _snap(b)
            if audio_state(snap) == "ready":
                break
        else:
            raise TimeoutError(f"Audio Overview not ready after {nlm.timeout_min} minutes")

    since = time.time() - 1
    await b.call("browser_click", {"element": "Audio Overview menu", "target": item_more_ref(snap)})
    await b.wait(1)
    await _click(b, "menuitem", "Download")
    return since


def make_audio(cfg: Config, folder: Path, result: dict) -> Path:
    from .podcast import email_ready

    audio = asyncio.run(generate_audio(cfg, folder, result))
    return email_ready(audio)
