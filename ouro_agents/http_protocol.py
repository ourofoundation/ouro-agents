"""Uvicorn HTTP protocol that logs what arrived when parsing fails."""

from __future__ import annotations

import httptools
from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

_TLS_RECORD = 0x16
_HTTP2_PREFACE = b"PRI * HTTP/2.0"


def describe_raw_http(data: bytes, *, limit: int = 180) -> str:
    """Best-effort URL / request-line preview from bytes that failed to parse."""
    if not data:
        return "empty"
    if data[0] == _TLS_RECORD:
        return "tls-handshake (HTTPS sent to HTTP port)"
    if data.startswith(_HTTP2_PREFACE):
        return "HTTP/2 preface"

    head, _, _ = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    request_line = _ascii_preview(lines[0], limit)
    host = None
    for line in lines[1:]:
        if line.lower().startswith(b"host:"):
            host = _ascii_preview(line.split(b":", 1)[1].strip(), 120)
            break
    if host:
        return f"{request_line} Host: {host}"
    return request_line


def _ascii_preview(data: bytes, limit: int) -> str:
    clipped = data[:limit]
    try:
        text = clipped.decode("ascii")
    except UnicodeDecodeError:
        return repr(clipped)
    if len(data) > limit:
        text += "…"
    return text.replace("\r", "\\r").replace("\n", "\\n")


def _client_label(client: tuple[str, int] | None) -> str:
    if not client:
        return "unknown"
    return f"{client[0]}:{client[1]}"


def invalid_http_message(
    client: tuple[str, int] | None,
    data: bytes,
    exc: BaseException,
    parsed_url: bytes | str | None = None,
) -> str:
    bits = [f"Invalid HTTP request received from {_client_label(client)}"]
    if parsed_url:
        url = (
            parsed_url.decode("ascii", "replace")
            if isinstance(parsed_url, bytes)
            else parsed_url
        )
        if url:
            bits.append(f"url={url}")
    bits.append(describe_raw_http(data))
    err = str(exc).strip()
    if err:
        bits.append(f"({err})")
    return " ".join(bits)


class LoggedHttpToolsProtocol(HttpToolsProtocol):
    """Like httptools, but the invalid-request warning includes client + URL."""

    def data_received(self, data: bytes) -> None:
        self._unset_keepalive_if_required()

        try:
            self.parser.feed_data(data)
        except httptools.HttpParserError as exc:
            parsed = getattr(self, "url", b"") or None
            self.logger.warning(
                invalid_http_message(self.client, data, exc, parsed)
            )
            self.send_400_response("Invalid HTTP request received.")
            return
        except httptools.HttpParserUpgrade:
            if self._should_upgrade():
                self.handle_websocket_upgrade()
            else:
                self._unsupported_upgrade_warning()
