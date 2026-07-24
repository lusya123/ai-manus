"""HTTP proxy API for web apps running inside the sandbox."""

import asyncio
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote

from fastapi import APIRouter, Request, Response

router = APIRouter()

# The preview proxy is intentionally able to reach arbitrary application ports
# started by an agent, but it must never expose the sandbox control plane.  A
# signed preview URL can be viewed by someone other than the session owner, so
# forwarding any of these ports would turn that URL into shell/file, browser,
# or desktop access.
BLOCKED_PREVIEW_PORTS = frozenset({
    5900,   # x11vnc
    5901,   # websockify
    8080,   # sandbox FastAPI (shell/file/supervisor)
    8222,   # Chromium CDP
    9222,   # forwarded Chromium CDP
    30150,  # AgentBay -> sandbox FastAPI
    30151,  # AgentBay -> Chromium CDP
    30152,  # AgentBay -> websockify
})

# Never route loopback preview traffic through HTTP(S)_PROXY from the sandbox
# environment. Apart from leaking a local URL, that would make an unreachable
# local port return the upstream proxy's response instead of a deterministic 502.


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    """Return redirects to the caller instead of following them server-side.

    Following a redirect would allow an otherwise permitted application port
    to bounce the proxy into a blocked control-plane port.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_LOOPBACK_OPENER = urllib_request.build_opener(
    urllib_request.ProxyHandler({}),
    _NoRedirectHandler(),
)

HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# Preview responses are intentionally buffered so the proxy can keep using the
# Python standard library without introducing a second HTTP stack. Bound both
# directions so an untrusted generated app cannot exhaust the sandbox process.
MAX_PREVIEW_REQUEST_BYTES = 32 * 1024 * 1024
MAX_PREVIEW_RESPONSE_BYTES = 32 * 1024 * 1024
_PREVIEW_PATH_DECODE_LIMIT = 16


class _PreviewResponseTooLarge(Exception):
    pass


def _is_safe_preview_path(path: str) -> bool:
    """Reject path forms that a later HTTP layer could normalize upward."""

    candidate = path
    for _ in range(_PREVIEW_PATH_DECODE_LIMIT):
        if "\\" in candidate or any(
            ord(character) < 32 or ord(character) == 127
            for character in candidate
        ):
            return False
        if any(segment in {".", ".."} for segment in candidate.split("/")):
            return False
        try:
            decoded = unquote(candidate, errors="strict")
        except UnicodeDecodeError:
            return False
        if decoded == candidate:
            return True
        candidate = decoded
    return False


async def _read_request_body_limited(request: Request) -> bytes:
    """Read a request body while enforcing the preview gateway memory bound."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_PREVIEW_REQUEST_BYTES:
                raise ValueError("too large")
        except ValueError as error:
            raise ValueError("Preview request is too large") from error

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_PREVIEW_REQUEST_BYTES:
            raise ValueError("Preview request is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _read_response_limited(proxied) -> bytes:  # noqa: ANN001
    content_length = proxied.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > MAX_PREVIEW_RESPONSE_BYTES:
                raise _PreviewResponseTooLarge
        except ValueError:
            # A malformed upstream Content-Length must not disable the actual
            # byte-counting guard below.
            pass

    content = proxied.read(MAX_PREVIEW_RESPONSE_BYTES + 1)
    if len(content) > MAX_PREVIEW_RESPONSE_BYTES:
        raise _PreviewResponseTooLarge
    return content


def _proxy_request(
    method: str,
    port: int,
    path: str,
    query: str,
    headers: dict[str, str],
    body: bytes,
) -> tuple[int, dict[str, str], bytes]:
    """Forward one request to a loopback-only web server."""
    if not _is_safe_preview_path(path):
        raise ValueError("Invalid preview path")
    # Starlette provides a decoded path; encode it again for urllib while
    # keeping URL separators and already escaped bytes intact.
    encoded_path = quote(path, safe="/:@!$&'()*+,;=-._~%")
    target_path = f"/{encoded_path}" if encoded_path else "/"
    target_url = f"http://127.0.0.1:{port}{target_path}"
    if query:
        target_url = f"{target_url}?{query}"

    outbound_headers = {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
    }
    outbound_headers["Host"] = f"127.0.0.1:{port}"

    data = body if method.upper() not in {"GET", "HEAD"} else None
    proxied_request = urllib_request.Request(
        target_url,
        data=data,
        headers=outbound_headers,
        method=method.upper(),
    )

    try:
        with _LOOPBACK_OPENER.open(proxied_request, timeout=30) as proxied:
            return proxied.status, dict(proxied.headers.items()), _read_response_limited(proxied)
    except HTTPError as error:
        try:
            content = _read_response_limited(error)
        except _PreviewResponseTooLarge:
            return 502, {"content-type": "text/plain; charset=utf-8"}, b"Preview response is too large"
        return error.code, dict(error.headers.items()), content
    except _PreviewResponseTooLarge:
        return 502, {"content-type": "text/plain; charset=utf-8"}, b"Preview response is too large"
    except (URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        return 502, {"content-type": "text/plain; charset=utf-8"}, str(reason).encode("utf-8")


@router.api_route(
    "/{port}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
@router.api_route(
    "/{port}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_local_web_app(request: Request, port: int, path: str = "") -> Response:
    """Proxy a request to a localhost web server started by an agent task."""
    if port < 1 or port > 65535:
        return Response("Invalid port", status_code=400, media_type="text/plain")
    if port in BLOCKED_PREVIEW_PORTS:
        return Response("Preview port is not allowed", status_code=403, media_type="text/plain")
    if not _is_safe_preview_path(path):
        return Response("Invalid preview path", status_code=400, media_type="text/plain")

    try:
        body = await _read_request_body_limited(request)
    except ValueError:
        return Response("Preview request is too large", status_code=413, media_type="text/plain")

    status, response_headers, content = await asyncio.to_thread(
        _proxy_request,
        request.method,
        port,
        path,
        request.url.query,
        dict(request.headers),
        body,
    )

    headers = {
        key: value
        for key, value in response_headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
    return Response(content=content, status_code=status, headers=headers)
