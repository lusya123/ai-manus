"""
HTTP proxy API for web apps running inside the sandbox.
"""
import asyncio
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from fastapi import APIRouter, Request, Response

router = APIRouter()

HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
}


def _proxy_request(method: str, port: int, path: str, query: str, headers: dict[str, str], body: bytes):
    target_path = f"/{path}" if path else "/"
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
    req = urllib_request.Request(
        target_url,
        data=data,
        headers=outbound_headers,
        method=method.upper(),
    )

    try:
        with urllib_request.urlopen(req, timeout=30) as proxied:
            return proxied.status, dict(proxied.headers.items()), proxied.read()
    except HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()
    except URLError as error:
        return 502, {"content-type": "text/plain; charset=utf-8"}, str(error.reason).encode("utf-8")


@router.api_route("/{port}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@router.api_route("/{port}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_local_web_app(request: Request, port: int, path: str = "") -> Response:
    """
    Proxy localhost web servers started by agent tasks inside the sandbox.
    """
    if port < 1 or port > 65535:
        return Response("Invalid port", status_code=400, media_type="text/plain")

    body = await request.body()
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
    content_type = headers.pop("content-type", None)

    return Response(
        content=content,
        status_code=status,
        headers=headers,
        media_type=content_type.split(";")[0] if content_type else None,
    )
