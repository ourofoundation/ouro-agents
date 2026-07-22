"""Serialize stdio MCP tool invocations; leave HTTP MCP unlocked.

Stdio MCP multiplexes JSON-RPC over one process pipe — concurrent calls from
overlapping runs or parallel subagents corrupt the protocol. Streamable-HTTP
servers are naturally concurrent, so their tools skip the lock.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class McpServerLocks:
    """Per-server locks for stdio MCP; HTTP servers are recorded as unlocked."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stdio_locks: dict[str, threading.RLock] = {}
        self._http_servers: set[str] = set()

    def register_stdio(self, server_name: str) -> threading.RLock:
        with self._lock:
            lock = self._stdio_locks.get(server_name)
            if lock is None:
                lock = threading.RLock()
                self._stdio_locks[server_name] = lock
            self._http_servers.discard(server_name)
            return lock

    def register_http(self, server_name: str) -> None:
        with self._lock:
            self._http_servers.add(server_name)
            self._stdio_locks.pop(server_name, None)

    def lock_for(self, server_name: str) -> Optional[threading.RLock]:
        with self._lock:
            if server_name in self._http_servers:
                return None
            return self._stdio_locks.get(server_name)

    def clear(self) -> None:
        with self._lock:
            self._stdio_locks.clear()
            self._http_servers.clear()


def wrap_mcp_tool_with_lock(
    tool: Any,
    *,
    server_name: str,
    locks: McpServerLocks,
) -> Any:
    """Wrap a smolagents MCP tool so stdio invocations take the server lock.

    HTTP-registered servers return the tool unchanged. Mutates the tool's
    ``forward`` (and ``__call__`` if present as a bound method we can replace
    carefully — smolagents tools primarily use ``forward``).
    """
    call_lock = locks.lock_for(server_name)
    if call_lock is None:
        return tool

    original_forward: Callable[..., Any] = tool.forward

    def locked_forward(*args: Any, **kwargs: Any) -> Any:
        with call_lock:
            return original_forward(*args, **kwargs)

    tool.forward = locked_forward  # type: ignore[method-assign]
    return tool
