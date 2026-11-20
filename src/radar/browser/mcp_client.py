"""Playwright MCP client with a hard tool allowlist and an optional host guard.

The LinkedIn scanner uses the default read-only allowlist: navigate, run fixed JavaScript,
wait, and close. No click, type, or form tool is reachable from it, so the agent cannot send a
connection request, like, comment, or message even if a prompt or page told it to.

The NotebookLM client gets interaction tools, but only behind a host guard: every interactive
call first checks that the page is on an allowed host (notebook.google.com) and refuses
otherwise.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ALLOWED_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_evaluate",
        "browser_wait_for",
        "browser_snapshot",
        "browser_close",
    }
)


INTERACTIVE_TOOLS = frozenset(
    {
        "browser_click",
        "browser_type",
        "browser_press_key",
        "browser_file_upload",
        "browser_fill_form",
        "browser_select_option",
        "browser_hover",
    }
)
NOTEBOOKLM_TOOLS = ALLOWED_TOOLS | {
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_file_upload",
    "browser_hover",
    "browser_tabs",
}


class ToolNotAllowed(RuntimeError):
    pass


class BrowserMCP:
    """Async context manager around a Playwright MCP stdio server."""

    def __init__(
        self,
        command: str,
        args: list[str],
        profile_dir: Path,
        output_dir: Path,
        headless: bool = False,
        allowed_tools: frozenset[str] = ALLOWED_TOOLS,
        allowed_hosts: frozenset[str] | None = None,
    ):
        self.allowed_tools = allowed_tools
        self.allowed_hosts = allowed_hosts
        if allowed_tools & INTERACTIVE_TOOLS and not allowed_hosts:
            raise ValueError("interactive browser tools require an allowed_hosts guard")
        self.command = command
        self.args = list(args) + [
            "--user-data-dir",
            str(profile_dir),
            "--output-dir",
            str(output_dir),
        ]
        if headless:
            self.args.append("--headless")
        self._stack: AsyncExitStack | None = None
        self._last_call: str | None = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> BrowserMCP:  # noqa: PYI034
        self._stack = AsyncExitStack()
        params = StdioServerParameters(command=self.command, args=self.args)
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        tools = {t.name for t in (await self.session.list_tools()).tools}
        missing = {"browser_navigate", "browser_evaluate"} - tools
        if missing:
            raise RuntimeError(f"Playwright MCP server lacks required tools: {sorted(missing)}")
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self.session is not None:
                await self.call("browser_close", {})
        except Exception:  # noqa: BLE001, S110 - best-effort close on shutdown
            pass
        if self._stack is not None:
            await self._stack.aclose()

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self.allowed_tools:
            raise ToolNotAllowed(f"tool {name!r} is not on this client's allowlist")
        assert self.session is not None, "BrowserMCP used outside its context"
        if name == "browser_file_upload":
            # A file chooser blocks page scripts, so the host cannot be read now. Uploads are
            # allowed only straight after a host-checked click, the click that opened it.
            if self._last_call != "browser_click":
                raise ToolNotAllowed("file upload refused: no host-checked click opened a chooser")
        elif name in INTERACTIVE_TOOLS:
            await self._check_host(name)
        result = await self.session.call_tool(name, arguments)
        self._last_call = name
        text = "\n".join(getattr(c, "text", "") for c in result.content)
        if getattr(result, "is_error", None) or getattr(result, "isError", None):
            raise RuntimeError(f"{name} failed: {text[:500]}")
        return text

    async def _check_host(self, tool: str) -> None:
        from urllib.parse import urlparse

        result = await self.session.call_tool(
            "browser_evaluate", {"function": "() => location.href"}
        )
        try:
            href = parse_evaluate_result("\n".join(getattr(c, "text", "") for c in result.content))
        except ValueError as e:
            raise ToolNotAllowed(f"{tool} refused: could not read the page host") from e
        host = urlparse(str(href)).hostname or ""
        if host not in (self.allowed_hosts or frozenset()):
            raise ToolNotAllowed(f"{tool} refused: page host {host!r} is not allowed")

    async def navigate(self, url: str) -> str:
        return await self.call("browser_navigate", {"url": url})

    async def evaluate(self, function_src: str) -> Any:
        """Run a JS function expression in the page and return its JSON result."""
        text = await self.call("browser_evaluate", {"function": function_src})
        return parse_evaluate_result(text)

    async def wait(self, seconds: float) -> None:
        await self.call("browser_wait_for", {"time": seconds})


def parse_evaluate_result(text: str) -> Any:
    """Playwright MCP wraps evaluate output in markdown. Pull out the JSON value.

    Handles three shapes: bare JSON, a "### Result" section followed by JSON, and a fenced
    ```json block.
    """
    body = text
    if "### Result" in body:
        body = body.split("### Result", 1)[1]
        for stop in ("\n### ", "\n\n### "):
            if stop in body:
                body = body.split(stop, 1)[0]
    if "```" in body:
        parts = body.split("```")
        if len(parts) >= 2:
            block = parts[1]
            body = block.split("\n", 1)[1] if block.startswith(("json", "js", "\n")) else block
    body = body.strip()
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # Some versions double-encode strings.
        start = min((i for i in (body.find("["), body.find("{")) if i >= 0), default=-1)
        if start >= 0:
            return json.loads(body[start:])
        raise
