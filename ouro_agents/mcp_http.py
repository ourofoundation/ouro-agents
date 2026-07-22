"""Managed streamable-http MCP subprocess lifecycle."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass
class ManagedMcpProcess:
    """A streamable-http MCP server spawned and owned by the agent."""

    name: str
    url: str
    process: subprocess.Popen
    ready: bool = False
    log_path: Optional[Path] = None

    def stop(self, timeout: float = 5.0) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                logger.warning("MCP server %s did not exit after kill", self.name)


def _parse_host_port(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"streamable-http url must include host and port: {url!r}")
    return parsed.hostname, parsed.port


def _port_pids(host: str, port: int) -> set[int]:
    """Best-effort set of PIDs listening on host:port (Linux /proc)."""
    pids: set[int] = set()
    # Prefer ss when available.
    try:
        out = subprocess.check_output(
            ["ss", "-tlnp", f"sport = :{port}"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        import re

        for match in re.finditer(r"pid=(\d+)", out):
            pids.add(int(match.group(1)))
        if pids:
            return pids
    except Exception:
        pass
    # Fallback: try connecting; we cannot map PID without ss.
    return pids


def _tcp_accepts(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_mcp_endpoint(url: str, timeout: float = 1.5) -> bool:
    """Return True if the URL looks like an MCP streamable-http endpoint.

    A bare TCP accept is not enough — another service may already own the port.
    We POST a minimal JSON-RPC initialize and accept either a JSON-RPC body or
    an Accept/session-related 4xx that still proves the MCP route exists
    (vs a generic OpenAPI 404).
    """
    payload = (
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
        b'{"protocolVersion":"2024-11-05","capabilities":{},'
        b'"clientInfo":{"name":"ouro-agents-probe","version":"0"}}}'
    )
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(512)
            # Any 2xx with body from /mcp is good enough.
            return bool(body) or 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        # 404 = wrong service / path. Other 4xx (406 Accept, 400 bad session)
        # often mean the MCP route is mounted.
        if e.code == 404:
            return False
        return e.code < 500
    except Exception:
        return False


def wait_for_mcp_url(
    url: str,
    *,
    timeout_s: float = 30.0,
    interval_s: float = 0.25,
    process: Optional[subprocess.Popen] = None,
) -> bool:
    host, port = _parse_host_port(url)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            logger.error(
                "Managed MCP process exited early (code=%s) before %s was ready",
                process.returncode,
                url,
            )
            return False
        if process is not None:
            owners = _port_pids(host, port)
            # If we can see listeners and our pid isn't among them, keep waiting
            # (or another process owns the port — don't declare ready).
            if owners and process.pid not in owners:
                time.sleep(interval_s)
                continue
        if _tcp_accepts(host, port) and _probe_mcp_endpoint(url):
            if process is not None:
                owners = _port_pids(host, port)
                if owners and process.pid not in owners:
                    logger.error(
                        "Port %s:%s is listening but not owned by managed MCP "
                        "pid=%s (owners=%s) — refusing to connect",
                        host,
                        port,
                        process.pid,
                        sorted(owners),
                    )
                    return False
            return True
        time.sleep(interval_s)
    return False


def spawn_managed_mcp_http(
    *,
    name: str,
    command: str,
    args: Optional[list[str]],
    env: Optional[dict[str, str]],
    url: str,
    ready_timeout_s: float = 30.0,
    log_dir: Optional[Path] = None,
) -> ManagedMcpProcess:
    """Start an MCP HTTP server subprocess and wait until ``url`` responds."""
    host, port = _parse_host_port(url)

    # Fail fast if something else already owns the port.
    existing = _port_pids(host, port)
    if existing or _tcp_accepts(host, port):
        raise RuntimeError(
            f"Cannot start managed MCP server {name!r}: {host}:{port} is already "
            f"in use"
            + (f" by pid(s) {sorted(existing)}" if existing else "")
            + f". Choose a free port in the streamable-http url ({url})."
        )

    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    cmd = [command, *(args or [])]
    if log_dir is None:
        log_dir = Path(os.environ.get("TMPDIR", "/tmp"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"ouro-mcp-{name}-{port}.log"
    log_file = open(log_path, "ab", buffering=0)

    logger.info(
        "Starting managed MCP HTTP server %s: %s -> %s (log=%s)",
        name,
        cmd,
        url,
        log_path,
    )
    proc = subprocess.Popen(
        cmd,
        env=full_env,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    managed = ManagedMcpProcess(
        name=name, url=url, process=proc, log_path=log_path
    )
    try:
        if not wait_for_mcp_url(url, timeout_s=ready_timeout_s, process=proc):
            tail = ""
            try:
                tail = log_path.read_text(errors="replace")[-2000:]
            except Exception:
                pass
            managed.stop()
            raise RuntimeError(
                f"Managed MCP server {name!r} did not become ready at {url}"
                + (f"\n--- log ---\n{tail}" if tail else "")
            )
    finally:
        try:
            log_file.close()
        except Exception:
            pass

    managed.ready = True
    logger.info("Managed MCP HTTP server %s ready at %s", name, url)
    return managed
