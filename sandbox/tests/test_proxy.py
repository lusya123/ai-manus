"""Tests for the sandbox loopback web-app proxy."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from starlette.requests import Request

from app.api.v1 import proxy as proxy_module
from app.api.v1.proxy import (
    BLOCKED_PREVIEW_PORTS,
    _is_safe_preview_path,
    _proxy_request,
    proxy_local_web_app,
)


class _TargetHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/large":
            self.send_response(200)
            self.send_header("Content-Length", "16")
            self.end_headers()
            self.wfile.write(b"0123456789abcdef")
            return
        if self.path == "/redirect-control-plane":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:8080/api/v1/shell/exec")
            self.end_headers()
            return
        if self.path == "/failure":
            self.send_response(418)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"teapot")
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(f'{{"path":"{self.path}"}}'.encode())

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        self.send_response(201)
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def target_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TargetHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_proxy_forwards_path_query_and_content_type(target_server: int) -> None:
    status, headers, body = _proxy_request(
        "GET",
        target_server,
        "preview file",
        "theme=dark",
        {"host": "sandbox.invalid", "x-test": "value"},
        b"",
    )

    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert body == b'{"path":"/preview%20file?theme=dark"}'


def test_proxy_forwards_request_body(target_server: int) -> None:
    status, _, body = _proxy_request(
        "POST", target_server, "echo", "", {"content-type": "text/plain"}, b"payload"
    )

    assert status == 201
    assert body == b"payload"


def test_proxy_preserves_http_error_response(target_server: int) -> None:
    status, headers, body = _proxy_request("GET", target_server, "failure", "", {}, b"")

    assert status == 418
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert body == b"teapot"


def test_proxy_does_not_follow_redirects_to_control_plane(target_server: int) -> None:
    status, headers, body = _proxy_request(
        "GET", target_server, "redirect-control-plane", "", {}, b""
    )

    assert status == 302
    assert headers["Location"] == "http://127.0.0.1:8080/api/v1/shell/exec"
    assert body == b""


def test_proxy_returns_bad_gateway_for_unreachable_target() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TargetHandler)
    unused_port = server.server_port
    server.server_close()

    status, headers, _ = _proxy_request("GET", unused_port, "", "", {}, b"")

    assert status == 502
    assert headers["content-type"] == "text/plain; charset=utf-8"


def test_proxy_rejects_oversized_upstream_response(
    target_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proxy_module, "MAX_PREVIEW_RESPONSE_BYTES", 8)

    status, headers, body = _proxy_request("GET", target_server, "large", "", {}, b"")

    assert status == 502
    assert headers["content-type"] == "text/plain; charset=utf-8"
    assert body == b"Preview response is too large"


def test_proxy_rejects_oversized_request_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy_module, "MAX_PREVIEW_REQUEST_BYTES", 8)
    chunks = iter((b"12345", b"6789"))

    async def receive() -> dict[str, object]:
        try:
            body = next(chunks)
            return {"type": "http.request", "body": body, "more_body": True}
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/proxy/3000",
            "raw_path": b"/api/v1/proxy/3000",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8080),
        },
        receive,
    )

    response = asyncio.run(proxy_local_web_app(request, 3000))

    assert response.status_code == 413
    assert response.body == b"Preview request is too large"


def test_proxy_rejects_invalid_port() -> None:
    response = asyncio.run(proxy_local_web_app(None, 0))  # type: ignore[arg-type]

    assert response.status_code == 400
    assert response.body == b"Invalid port"


@pytest.mark.parametrize(
    "path",
    [
        "../file/read",
        "assets/../../file/read",
        "%2e%2e/file/read",
        "%252e%252e/file/read",
        "%2e%2e%2ffile/read",
        "%252e%252e%252ffile/read",
        r"assets\..\file\read",
    ],
)
def test_proxy_rejects_path_normalization_tricks(path: str) -> None:
    assert _is_safe_preview_path(path) is False

    response = asyncio.run(proxy_local_web_app(None, 3000, path))  # type: ignore[arg-type]

    assert response.status_code == 400
    assert response.body == b"Invalid preview path"
    with pytest.raises(ValueError, match="Invalid preview path"):
        _proxy_request("GET", 3000, path, "", {}, b"")


@pytest.mark.parametrize(
    "path",
    ["", "assets/app.js", "release..notes/file", "preview%20file"],
)
def test_proxy_accepts_non_traversing_paths(path: str) -> None:
    assert _is_safe_preview_path(path) is True


@pytest.mark.parametrize("port", sorted(BLOCKED_PREVIEW_PORTS))
def test_proxy_rejects_sandbox_control_plane_ports(port: int) -> None:
    response = asyncio.run(proxy_local_web_app(None, port))  # type: ignore[arg-type]

    assert response.status_code == 403
    assert response.body == b"Preview port is not allowed"
