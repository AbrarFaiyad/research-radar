"""Playwright MCP client with a hard tool allowlist.

The scanner only reads. It can navigate, run read-only JavaScript, wait, and close the browser.
No click, type, or form tool is reachable from this client, so the agent cannot send a
connection request, like, comment, or message even if a prompt or page told it to.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ALLOWED_TOOLS = frozenset(
    {"browser_navigate", "browser_evaluate", "browser_wait_for", "browser_snapshot", "browser_close"}
)


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
    ):
        self.command = command
        self.args = list(args) + [
            "--user-data-dir", str(profile_dir),
            "--output-dir", str(output_dir),
        ]
        if headless:
            self.args.append("--headless")
        self._stack: AsyncExitStack | None = None
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
        if name not in ALLOWED_TOOLS:
            raise ToolNotAllowed(f"tool {name!r} is not on the read-only allowlist")
        assert self.session is not None, "BrowserMCP used outside its context"
        result = await self.session.call_tool(name, arguments)
        text = "\n".join(getattr(c, "text", "") for c in result.content)
        if getattr(result, "is_error", None) or getattr(result, "isError", None):
            raise RuntimeError(f"{name} failed: {text[:500]}")
        return text

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
