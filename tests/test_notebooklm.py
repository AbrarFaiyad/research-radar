import asyncio

import pytest

from radar.browser.mcp_client import NOTEBOOKLM_TOOLS, BrowserMCP, ToolNotAllowed
from radar.notebooklm import audio_instructions, audio_state, find_ref, item_more_ref

# Trimmed from a real Playwright MCP snapshot of the notebook page (2026-11-19).
GENERATING = '''
            - button "Infographic" [ref=e778] [cursor=pointer]:
            - button "Generating Audio Overview… Come back in a few minutes" [disabled]
'''
READY = '''
          - generic [ref=e179]:
            - button "Audio Overview" [ref=e411] [cursor=pointer]:
            - button "Infographic" [ref=e709] [cursor=pointer]:
          - generic [ref=e782] [cursor=pointer]:
            - button "Unread Fixing Baseline Energy in MLIP Fine Tuning 22:56 · Deep dive · 2 sources" [ref=e787]
            - generic [ref=e790]:
                - button "Interactive mode" [ref=e793]:
                - button "Play" [ref=e797]:
              - button "More" [ref=e801]:
  - generic [ref=e208]: Audio Overview 'Fixing Baseline Energy in MLIP Fine Tuning' is ready.
'''
SOURCES = '''
              - button "More" [ref=e318]
              - radio "Deep dive A lively conversation between two hosts" [checked] [ref=e556]
              - radio "Default" [checked] [ref=e632] [cursor=pointer]
              - textbox "What should the AI hosts focus on in this episode?" [ref=e661]
'''


def test_find_ref_exact_then_loose():
    assert find_ref(READY, "button", "Audio Overview") == "e411"
    assert find_ref(SOURCES, "radio", "Deep dive") == "e556"
    assert find_ref(SOURCES, "radio", "Default") == "e632"
    assert find_ref(SOURCES, "textbox", "What should the AI hosts focus on") == "e661"
    with pytest.raises(LookupError):
        find_ref(SOURCES, "button", "Generate now")


def test_audio_state_and_menu():
    assert audio_state(GENERATING) == "generating"
    assert audio_state(READY) == "ready"
    assert audio_state("nothing here") == "unknown"
    # The More button of the audio item, not the earlier source-list More buttons.
    assert item_more_ref(SOURCES + READY) == "e801"


def test_instructions_carry_focus_and_fit_limit():
    text = audio_instructions({"podcast_focus": "Lead with LoRA." + "x" * 5000})
    assert "how the work was done and why it matters" in text and "Lead with LoRA." in text
    assert len(text) <= 1900


def test_interactive_client_requires_host_guard(tmp_path):
    with pytest.raises(ValueError):
        BrowserMCP("npx", [], tmp_path, tmp_path, allowed_tools=NOTEBOOKLM_TOOLS)


def test_upload_needs_preceding_click(tmp_path):
    b = BrowserMCP("npx", [], tmp_path, tmp_path, allowed_tools=NOTEBOOKLM_TOOLS,
                   allowed_hosts=frozenset({"notebook.google.com"}))
    b.session = object()  # never reached: the guard refuses first
    with pytest.raises(ToolNotAllowed):
        asyncio.run(b.call("browser_file_upload", {"paths": ["x"]}))


class _FakeSession:
    def __init__(self, href):
        self.href, self.calls = href, []

    async def call_tool(self, name, args):
        self.calls.append(name)

        class C:
            text = f'### Result\n"{self.href}"\n'

        class R:
            content = (C(),)
            is_error = False
        return R()


@pytest.mark.parametrize("href,ok", [
    ("https://notebook.google.com/notebook/abc", True),
    ("https://www.linkedin.com/feed/", False),
    ("https://accounts.google.com/signin", False),
])
def test_clicks_only_on_allowed_hosts(tmp_path, href, ok):
    b = BrowserMCP("npx", [], tmp_path, tmp_path, allowed_tools=NOTEBOOKLM_TOOLS,
                   allowed_hosts=frozenset({"notebook.google.com"}))
    b.session = _FakeSession(href)
    if ok:
        asyncio.run(b.call("browser_click", {"target": "e1"}))
        assert b.session.calls == ["browser_evaluate", "browser_click"]
    else:
        with pytest.raises(ToolNotAllowed):
            asyncio.run(b.call("browser_click", {"target": "e1"}))
        assert b.session.calls == ["browser_evaluate"]
