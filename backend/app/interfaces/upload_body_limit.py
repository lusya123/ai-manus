"""ASGI request-body limits for multipart upload endpoints.

FastAPI resolves ``UploadFile`` parameters before a route handler runs.  A
size check inside the handler therefore protects GridFS, but it is too late to
protect the multipart parser and its temporary-file storage.  This pure ASGI
middleware enforces the HTTP-body boundary while bytes are still arriving.
"""

from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Optional, Pattern

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _UploadBodyTooLarge(Exception):
    """Raised by the guarded receive channel once the byte budget is spent."""


class UploadBodyLimitMiddleware:
    """Reject oversized upload requests before multipart form parsing.

    Both ``Content-Length`` requests and chunked requests are covered.  Only
    the configured exact paths and mutating methods are wrapped, so ordinary
    API streaming responses and WebSockets are unaffected.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        paths: Iterable[str] = (),
        path_patterns: Iterable[str] = (),
        error_detail: str = "Request body is too large",
    ) -> None:
        self.app = app
        self.max_body_bytes = max(1, int(max_body_bytes))
        self.paths = {
            self._normalize_path(path) for path in paths
        }
        self.path_patterns: tuple[Pattern[str], ...] = tuple(
            re.compile(pattern) for pattern in path_patterns
        )
        self.error_detail = error_detail

    @staticmethod
    def _normalize_path(path: str) -> str:
        normalized = path.rstrip("/")
        return normalized or "/"

    def _matches_path(self, path: str) -> bool:
        normalized = self._normalize_path(path)
        return normalized in self.paths or any(
            pattern.fullmatch(normalized) for pattern in self.path_patterns
        )

    async def _send_error(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        detail: str,
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            content={"detail": detail},
        )
        await response(scope, receive, send)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method", "").upper() not in {"POST", "PUT", "PATCH"}
            or not self._matches_path(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", ()))
        raw_content_length = headers.get(b"content-length")
        if raw_content_length is not None:
            try:
                content_length = int(raw_content_length)
            except (TypeError, ValueError):
                await self._send_error(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    detail="Invalid Content-Length header",
                )
                return
            if content_length < 0:
                await self._send_error(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    detail="Invalid Content-Length header",
                )
                return
            if content_length > self.max_body_bytes:
                await self._send_error(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    detail=self.error_detail,
                )
                return

        received_bytes = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    raise _UploadBodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _UploadBodyTooLarge:
            # Upload routes parse the request body before sending response
            # headers.  Keep this guard explicit in case a future endpoint
            # starts a response before consuming its request body.
            if response_started:
                raise
            await self._send_error(
                scope,
                receive,
                send,
                status_code=413,
                detail=self.error_detail,
            )
