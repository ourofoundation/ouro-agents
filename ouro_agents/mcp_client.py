"""Expose MCP server tools as synchronous smolagents tools.

smolagents calls tools synchronously, so each connection keeps its MCP client
session alive on a private event loop in a background thread and forwards
tool calls to it.
"""

from __future__ import annotations

import asyncio
import base64
import keyword
import logging
import re
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Future
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from io import BytesIO
from typing import Any

import jsonref
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import (
    CallToolResult,
    ImageContent,
    PaginatedRequestParams,
    TextContent,
)
from mcp.types import Tool as MCPToolSpec
from smolagents import Tool

logger = logging.getLogger(__name__)

Transport = Callable[[], AbstractAsyncContextManager[tuple[Any, ...]]]


@asynccontextmanager
async def streamable_http_transport(
    url: str, headers: dict[str, str] | None = None
) -> AsyncIterator[tuple[Any, ...]]:
    async with (
        create_mcp_http_client(headers=headers) as http_client,
        streamable_http_client(url, http_client=http_client) as streams,
    ):
        yield streams


class MCPConnection:
    """One MCP client session, opened with ``open()`` and ended with ``close()``."""

    def __init__(self, transport: Transport, *, connect_timeout: float = 30.0):
        self._transport = transport
        self._connect_timeout = connect_timeout
        self._loop = asyncio.new_event_loop()
        self._ready: Future[list[MCPToolSpec]] = Future()
        self._session: ClientSession | None = None
        self._task = self._loop.create_task(self._serve())
        self._thread = threading.Thread(target=self._run, daemon=True)

    def open(self) -> list[Tool]:
        self._thread.start()
        try:
            specs = self._ready.result(timeout=self._connect_timeout)
        except BaseException:
            self.close()
            raise
        return [MCPTool(spec, self._call) for spec in specs]

    def close(self) -> None:
        if self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._task.cancel)
            self._thread.join()
        self._loop.close()

    def _run(self) -> None:
        try:
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("MCP session ended unexpectedly")

    async def _serve(self) -> None:
        try:
            async with (
                self._transport() as (read, write, *_),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                specs = await _list_tools(session)
                self._session = session
                self._ready.set_result(specs)
                await asyncio.Event().wait()
        except Exception as exc:
            if self._ready.done():
                raise
            self._ready.set_exception(exc)

    def _call(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        assert self._session is not None
        return asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, arguments), self._loop
        ).result()


class MCPTool(Tool):
    output_type = "object"
    skip_forward_signature_validation = True

    def __init__(
        self,
        spec: MCPToolSpec,
        call: Callable[[str, dict[str, Any]], CallToolResult],
    ):
        self.name = _python_identifier(spec.name)
        self.description = spec.description or ""
        self.inputs = _tool_inputs(spec.input_schema)
        self.is_initialized = True
        self._mcp_name = spec.name
        self._call = call

    def forward(self, *args, **kwargs) -> Any:
        if args:
            if len(args) != 1 or not isinstance(args[0], dict) or kwargs:
                raise ValueError(
                    f"tool {self.name} takes keyword arguments or a single dict"
                )
            kwargs = args[0]
        return _tool_output(self.name, self._call(self._mcp_name, kwargs))


async def _list_tools(session: ClientSession) -> list[MCPToolSpec]:
    specs: list[MCPToolSpec] = []
    cursor: str | None = None
    while True:
        params = PaginatedRequestParams(cursor=cursor) if cursor else None
        page = await session.list_tools(params=params)
        specs.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            return specs


def _tool_inputs(input_schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """smolagents requires a ``type`` and ``description`` on every input."""
    schema = jsonref.replace_refs(input_schema, proxies=False)
    return {
        name: {"type": "string", "description": "see tool description", **prop}
        for name, prop in schema.get("properties", {}).items()
    }


def _tool_output(tool_name: str, result: CallToolResult) -> Any:
    texts = [item.text for item in result.content if isinstance(item, TextContent)]
    if texts:
        return "\n".join(texts)
    images = [item for item in result.content if isinstance(item, ImageContent)]
    if images:
        from PIL import Image

        return Image.open(BytesIO(base64.b64decode(images[0].data)))
    if not result.content:
        raise ValueError(f"tool {tool_name} returned no content")
    raise ValueError(
        f"tool {tool_name} returned unsupported content: {type(result.content[0]).__name__}"
    )


def _python_identifier(name: str) -> str:
    name = re.sub(r"\W", "", name.replace("-", "_"))
    if name[0].isdigit():
        name = f"_{name}"
    return f"{name}_" if keyword.iskeyword(name) else name
