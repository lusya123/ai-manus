import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile
from starlette.websockets import WebSocketDisconnect

from app.application.errors.exceptions import UnauthorizedError
from app.domain.models.user import User, UserRole
from app.interfaces.api.claw_routes import upload_claw_file
from app.interfaces.api.ws_routes import claw_ws
from app.interfaces.dependencies import resolve_ws_user


class _ScriptedWebSocket:
    def __init__(
        self,
        messages: list[dict | str | bytes] | None = None,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        block_when_empty: bool = False,
    ):
        self.messages = list(messages or [])
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.block_when_empty = block_when_empty
        self.accepted = False
        self.closed = None
        self.sent: list[dict] = []
        self._blocked = asyncio.Event()
        self.receive_cancelled = False

    async def accept(self):
        self.accepted = True

    async def receive(self):
        if self.messages:
            message = self.messages.pop(0)
            if isinstance(message, bytes):
                return {"type": "websocket.receive", "bytes": message}
            text = message if isinstance(message, str) else json.dumps(message)
            return {"type": "websocket.receive", "text": text}
        if self.block_when_empty:
            try:
                await self._blocked.wait()
                raise AssertionError("blocked receive should be cancelled")
            except asyncio.CancelledError:
                self.receive_cancelled = True
                raise
        raise WebSocketDisconnect()

    async def send_json(self, payload: dict):
        self.sent.append(payload)

    async def close(self, code: int, reason: str):
        self.closed = (code, reason)


class _EventBus:
    def __init__(self):
        self.queue = asyncio.Queue()

    def subscribe(self, user_id: str):
        return self.queue

    def unsubscribe(self, user_id: str, queue):
        pass


def _ws_service():
    return SimpleNamespace(
        event_bus=_EventBus(),
        get_pending_content=Mock(return_value=None),
        send_message=AsyncMock(),
        validate_claw_for_chat=AsyncMock(
            return_value=SimpleNamespace(http_base_url="http://resolved-claw")
        ),
        claw_repository=SimpleNamespace(
            append_message=AsyncMock(),
            get_by_user_id=AsyncMock(return_value=None),
        ),
    )


def _active_user() -> User:
    return User(
        id="user-1",
        fullname="Active",
        email="active@example.com",
        role=UserRole.USER,
        is_active=True,
    )


def _limited_ws_settings():
    return SimpleNamespace(
        auth_provider="password",
        session_cookie_name="session_id",
        claw_chat_max_message_bytes=64,
        claw_chat_max_attachments=2,
        claw_chat_max_attachment_bytes=8,
        claw_chat_max_total_attachment_bytes=16,
    )


@pytest.mark.asyncio
async def test_claw_websocket_rejects_connection_without_cookie_or_bearer():
    websocket = _ScriptedWebSocket()
    auth_service = SimpleNamespace(
        resolve_credentials=AsyncMock(return_value=None),
        user_from_resolved=AsyncMock(),
    )

    with (
        patch(
            "app.interfaces.dependencies.get_settings",
            return_value=_limited_ws_settings(),
        ),
        patch(
            "app.interfaces.dependencies.get_auth_service",
            return_value=auth_service,
        ),
    ):
        await claw_ws(websocket)  # type: ignore[arg-type]

    assert websocket.accepted is False
    assert websocket.closed == (4001, "Unauthorized")
    auth_service.resolve_credentials.assert_awaited_once_with(
        bearer_token=None,
        cookie_session_id=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_source", ["bearer", "cookie"])
async def test_resolve_ws_user_accepts_cookie_or_bearer_session(
    credential_source,
):
    session_id = f"{credential_source}-session"
    websocket = _ScriptedWebSocket(
        headers=(
            {"authorization": f"Bearer {session_id}"}
            if credential_source == "bearer"
            else None
        ),
        cookies=(
            {"session_id": session_id}
            if credential_source == "cookie"
            else None
        ),
    )
    user = _active_user()
    resolved = object()
    auth_service = SimpleNamespace(
        resolve_credentials=AsyncMock(return_value=resolved),
        user_from_resolved=AsyncMock(return_value=user),
    )

    with patch(
        "app.interfaces.dependencies.get_settings",
        return_value=_limited_ws_settings(),
    ):
        result = await resolve_ws_user(websocket, auth_service)  # type: ignore[arg-type]

    assert result == user
    auth_service.resolve_credentials.assert_awaited_once_with(
        bearer_token=session_id if credential_source == "bearer" else None,
        cookie_session_id=session_id if credential_source == "cookie" else None,
    )
    auth_service.user_from_resolved.assert_awaited_once_with(resolved)


@pytest.mark.asyncio
async def test_claw_websocket_rejects_inactive_user():
    inactive = User(
        id="disabled-user",
        fullname="Disabled",
        email="disabled@example.com",
        role=UserRole.USER,
        is_active=False,
    )
    websocket = _ScriptedWebSocket(
        headers={"authorization": "Bearer disabled-session"}
    )
    resolved = object()
    auth_service = SimpleNamespace(
        resolve_credentials=AsyncMock(return_value=resolved),
        user_from_resolved=AsyncMock(return_value=inactive),
    )

    with patch(
        "app.interfaces.dependencies.get_settings",
        return_value=_limited_ws_settings(),
    ):
        with pytest.raises(UnauthorizedError, match="Invalid token"):
            await resolve_ws_user(websocket, auth_service)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked_or_disabled", [None, "disabled"])
async def test_claw_websocket_rechecks_revocation_and_disabled_state_per_chat(
    revoked_or_disabled,
):
    active = _active_user()
    disabled = active.model_copy(update={"is_active": False})
    next_result = (
        disabled
        if revoked_or_disabled == "disabled"
        else UnauthorizedError("Authentication required")
    )
    resolve_user = AsyncMock(side_effect=[active, next_result])
    service = _ws_service()
    websocket = _ScriptedWebSocket(
        [{"type": "chat", "message": "must not run"}],
        headers={"authorization": "Bearer opaque-session"},
    )

    with (
        patch(
            "app.interfaces.api.ws_routes.resolve_ws_user",
            resolve_user,
        ),
        patch(
            "app.interfaces.api.ws_routes.get_settings",
            return_value=_limited_ws_settings(),
        ),
        patch(
            "app.interfaces.api.ws_routes.get_claw_service",
            return_value=service,
        ),
        patch(
            "app.interfaces.api.ws_routes.get_file_service",
            return_value=SimpleNamespace(),
        ),
    ):
        await asyncio.wait_for(claw_ws(websocket), timeout=1)  # type: ignore[arg-type]

    assert websocket.accepted is True
    assert websocket.closed == (4001, "Unauthorized")
    service.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_claw_idle_watchdog_rechecks_revoked_session_and_cancels_io():
    active = _active_user()
    resolve_user = AsyncMock(
        side_effect=[active, UnauthorizedError("revoked details")]
    )
    service = _ws_service()
    websocket = _ScriptedWebSocket(
        headers={"authorization": "Bearer opaque-session"},
        block_when_empty=True,
    )

    with (
        patch("app.interfaces.api.ws_routes.resolve_ws_user", resolve_user),
        patch(
            "app.interfaces.api.ws_routes.get_settings",
            return_value=_limited_ws_settings(),
        ),
        patch(
            "app.interfaces.api.ws_routes.get_claw_service",
            return_value=service,
        ),
        patch(
            "app.interfaces.api.ws_routes.get_file_service",
            return_value=SimpleNamespace(),
        ),
        patch("app.interfaces.api.ws_routes.WS_AUTH_WATCHDOG_SECONDS", 0),
    ):
        await asyncio.wait_for(claw_ws(websocket), timeout=1)  # type: ignore[arg-type]

    assert resolve_user.await_count == 2
    assert websocket.closed == (4001, "Unauthorized")
    assert websocket.receive_cancelled is True
    service.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_frame", "expected_close"),
    [
        pytest.param(
            json.dumps({"type": "noop", "unknown": "x" * (129 * 1024)}),
            (1009, "Message too large"),
            id="oversized-unknown-field",
        ),
        pytest.param(
            "[" * 100 + "0" + "]" * 100,
            (1003, "Invalid JSON"),
            id="excessive-json-depth",
        ),
    ],
)
async def test_claw_rejects_oversized_or_deep_json_before_dispatch(
    raw_frame, expected_close
):
    active = _active_user()
    service = _ws_service()
    websocket = _ScriptedWebSocket(
        [raw_frame],
        headers={"authorization": "Bearer opaque-session"},
    )

    with (
        patch(
            "app.interfaces.api.ws_routes.resolve_ws_user",
            new=AsyncMock(return_value=active),
        ),
        patch(
            "app.interfaces.api.ws_routes.get_settings",
            return_value=_limited_ws_settings(),
        ),
        patch(
            "app.interfaces.api.ws_routes.get_claw_service",
            return_value=service,
        ),
        patch(
            "app.interfaces.api.ws_routes.get_file_service",
            return_value=SimpleNamespace(),
        ),
    ):
        await asyncio.wait_for(claw_ws(websocket), timeout=1)  # type: ignore[arg-type]

    assert websocket.closed == expected_close
    service.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chat_frame, expected_error",
    [
        (
            {"type": "chat", "message": "x" * 65},
            "message exceeds",
        ),
        (
            {"type": "chat", "message": 123},
            "Invalid Claw message",
        ),
        (
            {"type": "chat", "message": "hi", "file_ids": {"id": 1}},
            "Invalid or excessive",
        ),
        (
            {
                "type": "chat",
                "message": "hi",
                "file_ids": ["one", "two", "three"],
            },
            "Invalid or excessive",
        ),
    ],
)
async def test_claw_websocket_rejects_unbounded_chat_inputs(
    chat_frame,
    expected_error,
):
    active = _active_user()
    service = _ws_service()
    websocket = _ScriptedWebSocket(
        [chat_frame],
        headers={"authorization": "Bearer opaque-session"},
    )

    with (
        patch(
            "app.interfaces.api.ws_routes.resolve_ws_user",
            new=AsyncMock(return_value=active),
        ),
        patch(
            "app.interfaces.api.ws_routes.get_settings",
            return_value=_limited_ws_settings(),
        ),
        patch(
            "app.interfaces.api.ws_routes.get_claw_service",
            return_value=service,
        ),
        patch(
            "app.interfaces.api.ws_routes.get_file_service",
            return_value=SimpleNamespace(),
        ),
    ):
        await asyncio.wait_for(claw_ws(websocket), timeout=1)  # type: ignore[arg-type]

    assert expected_error in websocket.sent[0]["error"]
    service.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_claw_websocket_rejects_large_attachment_before_reading_it():
    active = _active_user()
    stream = SimpleNamespace(read=Mock(side_effect=AssertionError("must not read")))
    file_service = SimpleNamespace(
        download_file=AsyncMock(
            return_value=(
                stream,
                SimpleNamespace(
                    content_type="application/octet-stream",
                    filename="large.bin",
                    size=9,
                ),
            )
        )
    )
    service = _ws_service()
    websocket = _ScriptedWebSocket(
        [{"type": "chat", "message": "inspect", "file_ids": ["file-1"]}],
        headers={"authorization": "Bearer opaque-session"},
    )

    with (
        patch(
            "app.interfaces.api.ws_routes.resolve_ws_user",
            new=AsyncMock(return_value=active),
        ),
        patch(
            "app.interfaces.api.ws_routes.get_settings",
            return_value=_limited_ws_settings(),
        ),
        patch(
            "app.interfaces.api.ws_routes.get_claw_service",
            return_value=service,
        ),
        patch(
            "app.interfaces.api.ws_routes.get_file_service",
            return_value=file_service,
        ),
    ):
        await asyncio.wait_for(claw_ws(websocket), timeout=1)  # type: ignore[arg-type]

    stream.read.assert_not_called()
    service.send_message.assert_not_awaited()
    assert "per-file size limit" in websocket.sent[-1]["error"]


@pytest.mark.asyncio
async def test_claw_attachment_uses_owner_resolved_runtime_address():
    active = _active_user()
    file_service = SimpleNamespace(
        download_file=AsyncMock(
            return_value=(
                io.BytesIO(b"data"),
                SimpleNamespace(
                    content_type="text/plain",
                    filename="note.txt",
                    size=4,
                ),
            )
        )
    )
    service = _ws_service()
    service.claw_repository.get_by_user_id.return_value = SimpleNamespace(
        http_base_url="http://stale-claw"
    )
    websocket = _ScriptedWebSocket(
        [{"type": "chat", "message": "inspect", "file_ids": ["file-1"]}],
        headers={"authorization": "Bearer opaque-session"},
    )
    posted_urls: list[str] = []
    settings = _limited_ws_settings()
    settings.claw_chat_max_message_bytes = 1024

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"path": "/home/ubuntu/note.txt"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            posted_urls.append(url)
            return Response()

    with (
        patch(
            "app.interfaces.api.ws_routes.resolve_ws_user",
            new=AsyncMock(return_value=active),
        ),
        patch(
            "app.interfaces.api.ws_routes.get_settings",
            return_value=settings,
        ),
        patch(
            "app.interfaces.api.ws_routes.get_claw_service",
            return_value=service,
        ),
        patch(
            "app.interfaces.api.ws_routes.get_file_service",
            return_value=file_service,
        ),
        patch(
            "app.interfaces.api.ws_routes.httpx.AsyncClient",
            return_value=Client(),
        ),
    ):
        await asyncio.wait_for(claw_ws(websocket), timeout=1)  # type: ignore[arg-type]

    service.validate_claw_for_chat.assert_awaited_once_with("user-1")
    service.claw_repository.get_by_user_id.assert_not_awaited()
    assert posted_urls == ["http://resolved-claw/workspace"]
    service.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_claw_capability_upload_rejects_oversized_file():
    file = UploadFile(
        file=io.BytesIO(b""),
        filename="oversized.bin",
        size=9,
    )
    claw_service = SimpleNamespace(
        verify_api_key=AsyncMock(return_value="user-1")
    )
    file_service = SimpleNamespace(upload_file=AsyncMock())

    with patch(
        "app.interfaces.api.claw_routes.get_settings",
        return_value=SimpleNamespace(claw_upload_max_bytes=8),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await upload_claw_file(
                file=file,
                x_claw_api_key="runtime-key",
                claw_service=claw_service,
                file_service=file_service,
            )

    assert exc_info.value.status_code == 413
    file_service.upload_file.assert_not_awaited()
