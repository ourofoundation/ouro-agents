from ouro_agents.http_protocol import describe_raw_http, invalid_http_message


def test_describe_extracts_request_line_and_host():
    payload = (
        b"GET /chronos/events HTTP/1.1\r\n"
        b"Host: agents.ouro.foundation\r\n"
        b"\r\n"
    )
    assert describe_raw_http(payload) == (
        "GET /chronos/events HTTP/1.1 Host: agents.ouro.foundation"
    )


def test_describe_tls_handshake():
    assert "tls-handshake" in describe_raw_http(b"\x16\x03\x01\x00\x01")


def test_describe_http2_preface():
    assert describe_raw_http(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n") == "HTTP/2 preface"


def test_invalid_http_message_includes_client_and_url():
    msg = invalid_http_message(
        ("127.0.0.1", 54321),
        b"GET /chronos/events HTTP/1.1\r\nHost: agents.ouro.foundation\r\n\r\n",
        RuntimeError("invalid HTTP method"),
        parsed_url=b"/chronos/events",
    )
    assert "127.0.0.1:54321" in msg
    assert "url=/chronos/events" in msg
    assert "Host: agents.ouro.foundation" in msg
    assert "(invalid HTTP method)" in msg
