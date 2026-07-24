import inspect
import asyncio
import io
import time
import jwt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile
from starlette.websockets import WebSocketDisconnect

from app.domain.models.user import User, UserRole
from app.application.services.auth_service import AuthService
from app.application.services.token_service import TokenService
from app.core.config import get_settings
from app.interfaces.api.claw_routes import (
    _resolve_ws_user,
    claw_ws,
    upload_claw_file,
)


class _RejectedWebSocket:
    def __init__(self, first_message: dict):
        self.first_message = first_message
        self.accepted = False
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        return self.first_message

    async def close(self, code: int, reason: str):
        self.closed = (code, reason)


class _ScriptedWebSocket:
    def __init__(self, messages: list[dict], *, block_when_empty: bool = False):
        self.messages = list(messages)
        self.block_when_empty = block_when_empty
        self.accepted = False
        self.closed = None
        self.sent: list[dict] = []
        self._blocked = asyncio.Event()

    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        if self.messages:
            return self.messages.pop(0)
        if self.block_when_empty:
            await self._blocked.wait()
            raise AssertionError("blocked receive should be cancelled")
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
        get_pending_thinking_content=Mock(return_value=None),
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
        claw_chat_max_message_bytes=64,
        claw_chat_max_attachments=2,
        claw_chat_max_attachment_bytes=8,
        claw_chat_max_total_attachment_bytes=16,
    )


@pytest.mark.asyncio
async def test_claw_websocket_requires_auth_as_first_frame():
    websocket = _RejectedWebSocket({"type": "chat", "message": "too early"})

    await claw_ws(websocket)  # type: ignore[arg-type]

    assert websocket.accepted is True
    assert websocket.closed == (4001, "Unauthorized")
    assert "token" not in inspect.signature(claw_ws).parameters


@pytest.mark.asyncio
async def test_claw_websocket_rejects_inactive_user():
    inactive = User(
        id="disabled-user",
        fullname="Disabled",
        email="disabled@example.com",
        role=UserRole.USER,
        is_active=False,
    )
    auth_service = SimpleNamespace(verify_token=AsyncMock(return_value=inactive))

    with (
        patch(
            "app.interfaces.api.claw_routes.get_settings",
            return_value=SimpleNamespace(auth_provider="password"),
        ),
        patch("app.interfaces.dependencies.get_auth_service", return_value=auth_service),
    ):
        with pytest.raises(ValueError, match="Authentication failed"):
            await _resolve_ws_user("primary-token")


@pytest.mark.asyncio
async def test_claw_websocket_marks_already_expired_signed_token_refreshable():
    secret = "expired-claw-ws-test-secret-at-least-32-bytes"
    expired_token = jwt.encode(
        {
            "sub": "user-1",
            "type": "access",
            "exp": int(time.time()) - 1,
        },
        secret,
        algorithm="HS256",
    )
    websocket = _RejectedWebSocket({
        "type": "auth",
        "token": expired_token,
    })
    auth_service = SimpleNamespace(
        token_service=SimpleNamespace(
            settings=SimpleNamespace(
                jwt_secret_key=secret,
                jwt_algorithm="HS256",
            )
        ),
        verify_token=AsyncMock(),
    )

    with (
        patch(
            "app.interfaces.api.claw_routes.get_settings",
            return_value=SimpleNamespace(auth_provider="password"),
        ),
        patch(
            "app.interfaces.dependencies.get_auth_service",
            return_value=auth_service,
        ),
    ):
        await claw_ws(websocket)  # type: ignore[arg-type]

    assert websocket.closed == (4002, "Authentication expired")
    auth_service.verify_token.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked_or_disabled", [None, "disabled"])
async def test_claw_websocket_rechecks_revocation_and_disabled_state_per_chat(
    revoked_or_disabled,
):
    active = _active_user()
    disabled = active.model_copy(update={"is_active": False})
    next_result = disabled if revoked_or_disabled == "disabled" else None
    auth_service = SimpleNamespace(
        verify_token=AsyncMock(side_effect=[active, next_result]),
        token_service=SimpleNamespace(
            verify_token=Mock(
                return_value={"type": "access", "exp": time.time() + 60}
            )
        ),
    )
    service = _ws_service()
    websocket = _ScriptedWebSocket(
        [
            {"type": "auth", "token": "primary-token"},
            {"type": "chat", "message": "must not run"},
        ]
    )

    with (
        patch(
            "app.interfaces.api.claw_routes.get_settings",
            return_value=SimpleNamespace(auth_provider="password"),
        ),
        patch(
            "app.interfaces.dependencies.get_auth_service",
            return_value=auth_service,
        ),
        patch(
            "app.interfaces.api.claw_routes.get_claw_service",
            return_value=service,
        ),
        patch(
            "app.interfaces.api.claw_routes.get_file_service",
            return_value=SimpleNamespace(),
        ),
    ):
        await asyncio.wait_for(claw_ws(websocket), timeout=1)  # type: ignore[arg-type]

    assert websocket.sent == [{"type": "auth_ack"}]
    assert websocket.closed == (4001, "Unauthorized")
    service.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_claw_websocket_closes_when_short_lived_token_expires():
    active = _active_user()
    auth_service = SimpleNamespace(
        verify_token=AsyncMock(return_value=active),
        token_service=SimpleNamespace(
            verify_token=Mock(
                return_value={"type": "access", "exp": time.time() + 0.05}
            )
        ),
    )
    service = _ws_service()
    websocket = _ScriptedWebSocket(
        [{"type": "auth", "token": "short-lived-token"}],
        block_when_empty=True,
    )

    with (
        patch(
            "app.interfaces.api.claw_routes.get_settings",
            return_value=SimpleNamespace(auth_provider="password"),
        ),
        patch(
            "app.interfaces.dependencies.get_auth_service",
            return_value=auth_service,
        ),
        patch(
            "app.interfaces.api.claw_routes.get_claw_service",
            return_value=service,
        ),
        patch(
            "app.interfaces.api.claw_routes.get_file_service",
            return_value=SimpleNamespace(),
        ),
    ):
        await asyncio.wait_for(claw_ws(websocket), timeout=1)  # type: ignore[arg-type]

    assert websocket.sent == [{"type": "auth_ack"}]
    assert websocket.closed == (4002, "Authentication expired")


@pytest.mark.asyncio
async def test_claw_websocket_reconnect_reconciles_completed_turn_to_idle():
    active = _active_user()
    auth_service = SimpleNamespace(
        verify_token=AsyncMock(return_value=active),
        token_service=SimpleNamespace(
            verify_token=Mock(
                return_value={"type": "access", "exp": time.time() + 0.05}
            )
        ),
    )
    service = _ws_service()
    service.is_processing = Mock(return_value=False)
    websocket = _ScriptedWebSocket(
        [{"type": "auth", "token": "reconnect-token"}],
        block_when_empty=True,
    )

    with (
        patch(
            "app.interfaces.api.claw_routes.get_settings",
            return_value=SimpleNamespace(auth_provider="password"),
        ),
        patch(
            "app.interfaces.dependencies.get_auth_service",
            return_value=auth_service,
        ),
        patch(
            "app.interfaces.api.claw_routes.get_claw_service",
            return_value=service,
        ),
        patch(
            "app.interfaces.api.claw_routes.get_file_service",
            return_value=SimpleNamespace(),
        ),
    ):
        await asyncio.wait_for(claw_ws(websocket), timeout=1)  # type: ignore[arg-type]

    assert websocket.sent[:2] == [
        {"type": "auth_ack"},
        {"type": "done", "stop_reason": "idle"},
    ]
    assert websocket.closed == (4002, "Authentication expired")


@pytest.mark.asyncio
async def test_claw_websocket_reconnect_uses_terminal_latch_after_missed_fanout():
    active = _active_user()
    auth_service = SimpleNamespace(
        verify_token=AsyncMock(return_value=active),
        token_service=SimpleNamespace(
            verify_token=Mock(
                return_value={"type": "access", "exp": time.time() + 0.05}
            )
        ),
    )
    service = _ws_service()
    service.get_pending_content = Mock(return_value="final answer")
    service.get_terminal_event = Mock(return_value={
        "type": "done",
        "stop_reason": "end_turn",
    })
    service.is_processing = Mock(return_value=True)
    websocket = _ScriptedWebSocket(
        [{"type": "auth", "token": "reconnect-token"}],
        block_when_empty=True,
    )

    with (
        patch(
            "app.interfaces.api.claw_routes.get_settings",
            return_value=SimpleNamespace(auth_provider="password"),
        ),
        patch(
            "app.interfaces.dependencies.get_auth_service",
            return_value=auth_service,
        ),
        patch(
            "app.interfaces.api.claw_routes.get_claw_service",
            return_value=service,
        ),
        patch(
            "app.interfaces.api.claw_routes.get_file_service",
            return_value=SimpleNamespace(),
        ),
    ):
        await asyncio.wait_for(claw_ws(websocket), timeout=1)  # type: ignore[arg-type]

    assert websocket.sent[:3] == [
        {"type": "auth_ack"},
        {"type": "catchup", "content": "final answer"},
        {"type": "done", "stop_reason": "end_turn"},
    ]
    assert websocket.closed == (4002, "Authentication expired")


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
    auth_service = SimpleNamespace(
        verify_token=AsyncMock(return_value=active),
        token_service=SimpleNamespace(
            verify_token=Mock(
                return_value={"type": "access", "exp": time.time() + 60}
            )
        ),
    )
    service = _ws_service()
    websocket = _ScriptedWebSocket(
        [
            {"type": "auth", "token": "primary-token"},
            chat_frame,
        ]
    )

    with (
        patch(
            "app.interfaces.api.claw_routes.get_settings",
            return_value=_limited_ws_settings(),
        ),
        patch(
            "app.interfaces.dependencies.get_auth_service",
            return_value=auth_service,
        ),
        patch(
            "app.interfaces.api.claw_routes.get_claw_service",
            return_value=service,
        ),
        patch(
            "app.interfaces.api.claw_routes.get_file_service",
            return_value=SimpleNamespace(),
        ),
    ):
        await asyncio.wait_for(claw_ws(websocket), timeout=1)  # type: ignore[arg-type]

    assert websocket.sent[0] == {"type": "auth_ack"}
    assert expected_error in websocket.sent[1]["error"]
    service.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_claw_websocket_rejects_large_attachment_before_reading_it():
    active = _active_user()
    auth_service = SimpleNamespace(
        verify_token=AsyncMock(return_value=active),
        token_service=SimpleNamespace(
            verify_token=Mock(
                return_value={"type": "access", "exp": time.time() + 60}
            )
        ),
    )
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
        [
            {"type": "auth", "token": "primary-token"},
            {"type": "chat", "message": "inspect", "file_ids": ["file-1"]},
        ]
    )

    with (
        patch(
            "app.interfaces.api.claw_routes.get_settings",
            return_value=_limited_ws_settings(),
        ),
        patch(
            "app.interfaces.dependencies.get_auth_service",
            return_value=auth_service,
        ),
        patch(
            "app.interfaces.api.claw_routes.get_claw_service",
            return_value=service,
        ),
        patch(
            "app.interfaces.api.claw_routes.get_file_service",
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
    auth_service = SimpleNamespace(
        verify_token=AsyncMock(return_value=active),
        token_service=SimpleNamespace(
            verify_token=Mock(
                return_value={"type": "access", "exp": time.time() + 60}
            )
        ),
    )
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
        [
            {"type": "auth", "token": "primary-token"},
            {"type": "chat", "message": "inspect", "file_ids": ["file-1"]},
        ]
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
            "app.interfaces.api.claw_routes.get_settings",
            return_value=settings,
        ),
        patch(
            "app.interfaces.dependencies.get_auth_service",
            return_value=auth_service,
        ),
        patch(
            "app.interfaces.api.claw_routes.get_claw_service",
            return_value=service,
        ),
        patch(
            "app.interfaces.api.claw_routes.get_file_service",
            return_value=file_service,
        ),
        patch(
            "app.interfaces.api.claw_routes.httpx.AsyncClient",
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


@pytest.mark.asyncio
async def test_password_logout_revokes_token_used_by_open_websocket(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "password")
    monkeypatch.setenv("API_KEY", "test-model-key")
    monkeypatch.setenv(
        "JWT_SECRET_KEY", "claw-ws-revocation-test-secret-at-least-32-bytes"
    )
    get_settings.cache_clear()

    user = _active_user()

    class _UserRepository:
        async def get_user_by_id(self, user_id: str):
            return user if user_id == user.id else None

    class _Redis:
        def __init__(self):
            self.values: dict[str, str] = {}

        async def set(self, key, value, ex=None):
            self.values[key] = value
            return True

        async def exists(self, key):
            return int(key in self.values)

        async def get(self, key):
            return self.values.get(key)

    redis = _Redis()
    monkeypatch.setattr(
        "app.infrastructure.storage.redis.get_redis",
        lambda: SimpleNamespace(client=redis),
    )
    token_service = TokenService()
    auth_service = AuthService(_UserRepository(), token_service)
    token = token_service.create_access_token(user)

    assert await auth_service.verify_token(token) == user
    assert await auth_service.logout(token) is True
    assert await auth_service.verify_token(token) is None
