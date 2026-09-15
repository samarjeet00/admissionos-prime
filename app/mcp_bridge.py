"""Connection to the PUAP admissions MCP server.

The MCP client transports are anyio-based and must be entered and exited from the
same task, so the connection lives inside one long-running background task that
serves tool calls from a queue. Reconnects with backoff if the link drops.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from contextlib import AsyncExitStack
from typing import Any

import httpx2 as httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, PaginatedRequestParams

from . import config

log = logging.getLogger("admissionos.mcp")


class MCPBridge:
    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []          # [{name, description, input_schema}]
        self.status = "disconnected"                    # not_configured | connecting | connected | error | disconnected
        self.error: str | None = None
        self.connected_at: float | None = None
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    @property
    def configured(self) -> bool:
        return bool(config.PUAP_MCP_URL or config.PUAP_MCP_COMMAND)

    # --- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        if not self.configured:
            self.status = "not_configured"
            self.error = "Set PUAP_MCP_URL (or PUAP_MCP_COMMAND) in .env to connect the admissions portal."
            log.warning(self.error)
            return
        self._queue = asyncio.Queue()
        self._task = asyncio.create_task(self._run(), name="mcp-bridge")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.status = "disconnected"

    async def _run(self) -> None:
        backoff = 2.0
        while True:
            self.status = "connecting"
            try:
                async with AsyncExitStack() as stack:
                    if config.PUAP_MCP_URL:
                        headers = {"Authorization": f"Bearer {config.PUAP_MCP_TOKEN}"} if config.PUAP_MCP_TOKEN else {}
                        http_client = await stack.enter_async_context(
                            httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30.0, read=config.TOOL_CALL_TIMEOUT))
                        )
                        read, write = await stack.enter_async_context(
                            streamable_http_client(config.PUAP_MCP_URL, http_client=http_client)
                        )
                    else:
                        parts = shlex.split(config.PUAP_MCP_COMMAND or "")
                        read, write = await stack.enter_async_context(
                            stdio_client(StdioServerParameters(command=parts[0], args=parts[1:]))
                        )
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
                    self.tools = await self._fetch_tools(session)
                    self.status, self.error, self.connected_at = "connected", None, time.time()
                    backoff = 2.0
                    log.info("PUAP MCP connected: %d tools", len(self.tools))
                    await self._serve(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the bridge alive whatever happens
                self.status, self.error = "error", f"{type(exc).__name__}: {exc}"
                log.error("PUAP MCP link failed (%s); retrying in %.0fs", self.error, backoff)
                await self._fail_pending(exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _fetch_tools(self, session: ClientSession) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            result = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
            tools.extend(
                {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                for t in result.tools
            )
            cursor = result.next_cursor
            if not cursor:
                break
        tools.sort(key=lambda t: t["name"])  # deterministic order keeps the prompt cache stable
        return tools

    async def _serve(self, session: ClientSession) -> None:
        assert self._queue is not None
        while True:
            name, args, fut = await self._queue.get()
            if fut.cancelled():
                continue
            try:
                result = await session.call_tool(name, args, read_timeout_seconds=config.TOOL_CALL_TIMEOUT)
                fut.set_result(result)
            except Exception as exc:  # noqa: BLE001
                fut.set_exception(exc)
                if isinstance(exc, (httpx.HTTPError, ConnectionError, OSError)):
                    raise  # transport is gone - let _run reconnect

    async def _fail_pending(self, exc: Exception) -> None:
        if not self._queue:
            return
        while not self._queue.empty():
            _, _, fut = self._queue.get_nowait()
            if not fut.done():
                fut.set_exception(exc)

    # --- calls ---------------------------------------------------------------
    async def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Returns (text_for_model, is_error)."""
        if self.status != "connected" or self._queue is None:
            return (f"Admissions portal not connected ({self.status}: {self.error or 'no detail'}). "
                    "Tell the user the data link is down and answer from what you already have.", True)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put((name, args, fut))
        try:
            result = await asyncio.wait_for(fut, timeout=config.TOOL_CALL_TIMEOUT + 5)
        except asyncio.TimeoutError:
            fut.cancel()
            return (f"Tool `{name}` timed out after {config.TOOL_CALL_TIMEOUT:.0f}s. Narrow the request "
                    "(smaller date range, a count report instead of a row pull) and retry.", True)
        except Exception as exc:  # noqa: BLE001
            return (f"Tool `{name}` failed: {type(exc).__name__}: {exc}", True)
        return self._render(name, result)

    @staticmethod
    def _render(name: str, result: Any) -> tuple[str, bool]:
        parts: list[str] = []
        is_error = False
        if isinstance(result, CallToolResult):
            is_error = bool(result.is_error)
            for block in result.content:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
            if result.structured_content and not parts:
                parts.append(json.dumps(result.structured_content, ensure_ascii=False))
        else:
            parts.append(str(result))
        text = "\n".join(parts).strip() or "(empty result)"
        limit = config.TOOL_RESULT_MAX_CHARS
        if len(text) > limit:
            text = (text[:limit] + f"\n\n[TRUNCATED: result was {len(text):,} characters; only the first {limit:,} are shown. "
                    "Re-query with tighter filters, a count report, or group_by instead of raw rows.]")
        return text, is_error


bridge = MCPBridge()
