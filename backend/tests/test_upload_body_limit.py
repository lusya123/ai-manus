import pytest

from app.interfaces.upload_body_limit import UploadBodyLimitMiddleware


def _http_scope(path: str, headers=()):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": list(headers),
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }


async def _run_request(app, scope, messages):
    incoming = list(messages)
    outgoing = []

    async def receive():
        if not incoming:
            return {"type": "http.disconnect"}
        return incoming.pop(0)

    async def send(message):
        outgoing.append(message)

    await app(scope, receive, send)
    return outgoing


def _status(messages):
    return next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )


@pytest.mark.asyncio
async def test_upload_body_limit_rejects_declared_size_before_reading_body():
    inner_called = False

    async def inner(scope, receive, send):
        nonlocal inner_called
        inner_called = True

    app = UploadBodyLimitMiddleware(
        inner,
        max_body_bytes=10,
        paths={"/api/v1/claw/upload"},
    )
    response = await _run_request(
        app,
        _http_scope(
            "/api/v1/claw/upload",
            headers=[(b"content-length", b"11")],
        ),
        [{"type": "http.request", "body": b"not consumed"}],
    )

    assert _status(response) == 413
    assert inner_called is False


@pytest.mark.asyncio
async def test_upload_body_limit_counts_chunked_body_before_parser_finishes():
    parsed_bytes = 0

    async def multipart_parser(scope, receive, send):
        nonlocal parsed_bytes
        while True:
            message = await receive()
            parsed_bytes += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = UploadBodyLimitMiddleware(
        multipart_parser,
        max_body_bytes=10,
        paths={"/api/v1/claw/upload"},
    )
    response = await _run_request(
        app,
        _http_scope("/api/v1/claw/upload"),
        [
            {"type": "http.request", "body": b"123456", "more_body": True},
            {"type": "http.request", "body": b"789012", "more_body": False},
        ],
    )

    assert _status(response) == 413
    # The second chunk crossed the boundary and never reached the parser.
    assert parsed_bytes == 6


@pytest.mark.asyncio
async def test_upload_body_limit_does_not_apply_to_unlisted_routes():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = UploadBodyLimitMiddleware(
        inner,
        max_body_bytes=10,
        paths={"/api/v1/claw/upload"},
    )
    response = await _run_request(
        app,
        _http_scope(
            "/api/v1/sessions",
            headers=[(b"content-length", b"100000")],
        ),
        [],
    )

    assert _status(response) == 204


@pytest.mark.asyncio
async def test_chat_body_limit_matches_dynamic_session_path_and_chunked_body():
    parsed_bytes = 0

    async def json_parser(scope, receive, send):
        nonlocal parsed_bytes
        while True:
            message = await receive()
            parsed_bytes += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = UploadBodyLimitMiddleware(
        json_parser,
        max_body_bytes=10,
        path_patterns={r"/api/v1/sessions/[^/]+/chat"},
        error_detail="Chat request body is too large",
    )
    response = await _run_request(
        app,
        _http_scope("/api/v1/sessions/session-1/chat"),
        [
            {"type": "http.request", "body": b"123456", "more_body": True},
            {"type": "http.request", "body": b"789012", "more_body": False},
        ],
    )

    assert _status(response) == 413
    assert parsed_bytes == 6
