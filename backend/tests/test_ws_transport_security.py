from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.errors.exceptions import UnauthorizedError
from app.interfaces.api import ws_routes


class _HandshakeWebSocket:
    def __init__(self, *, origin=None, cookie=True, bearer=False):
        self.headers = {}
        if origin is not None:
            self.headers["origin"] = origin
        if bearer:
            self.headers["authorization"] = "Bearer opaque-session"
        self.cookies = {"session_id": "opaque-session"} if cookie else {}
        self.closed = None

    async def close(self, code, reason=None):
        self.closed = (code, reason)


def _origin_settings():
    return SimpleNamespace(
        session_cookie_name="session_id",
        get_cors_allowed_origins=lambda: ["https://app.example.com"],
    )


async def _call_route(route, websocket):
    if route is ws_routes.vnc_ws:
        await route(websocket, "session-1")
    else:
        await route(websocket)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route",
    [
        ws_routes.sessions_list_ws,
        ws_routes.chat_ws,
        ws_routes.claw_ws,
        ws_routes.vnc_ws,
    ],
)
async def test_all_cookie_websockets_reject_untrusted_origin_before_auth(
    monkeypatch, route
):
    websocket = _HandshakeWebSocket(origin="https://evil.example")
    resolve_user = AsyncMock()
    monkeypatch.setattr(ws_routes, "get_settings", _origin_settings)
    monkeypatch.setattr(ws_routes, "resolve_ws_user", resolve_user)

    await _call_route(route, websocket)

    assert websocket.closed == (4003, "Forbidden")
    resolve_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route",
    [
        ws_routes.sessions_list_ws,
        ws_routes.chat_ws,
        ws_routes.claw_ws,
        ws_routes.vnc_ws,
    ],
)
async def test_all_cookie_websockets_allow_exact_configured_origin(
    monkeypatch, route
):
    websocket = _HandshakeWebSocket(origin="https://app.example.com")
    resolve_user = AsyncMock(side_effect=UnauthorizedError("not authenticated"))
    monkeypatch.setattr(ws_routes, "get_settings", _origin_settings)
    monkeypatch.setattr(ws_routes, "resolve_ws_user", resolve_user)

    await _call_route(route, websocket)

    resolve_user.assert_awaited_once_with(websocket)
    assert websocket.closed == (4001, "Unauthorized")


@pytest.mark.asyncio
async def test_cookie_websocket_requires_origin_but_native_bearer_may_omit_it(
    monkeypatch
):
    monkeypatch.setattr(ws_routes, "get_settings", _origin_settings)
    cookie_socket = _HandshakeWebSocket(origin=None, cookie=True)
    bearer_socket = _HandshakeWebSocket(origin=None, cookie=False, bearer=True)

    assert await ws_routes._enforce_ws_origin(cookie_socket) is False
    assert cookie_socket.closed == (4003, "Forbidden")
    assert await ws_routes._enforce_ws_origin(bearer_socket) is True
    assert bearer_socket.closed is None


class _SessionListWebSocket:
    def __init__(self):
        self.headers = {"authorization": "Bearer opaque-session"}
        self.cookies = {}
        self.accepted = False
        self.closed = None
        self.sent = []

    async def accept(self):
        self.accepted = True

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code, reason=None):
        self.closed = (code, reason)


@pytest.mark.asyncio
async def test_session_list_idle_loop_rechecks_revoked_session(monkeypatch):
    active = SimpleNamespace(id="user-1", is_active=True)
    resolve_user = AsyncMock(
        side_effect=[active, UnauthorizedError("revoked details")]
    )

    class PubSub:
        async def subscribe(self, _channel):
            return None

        async def get_message(self, **_kwargs):
            return None

        async def unsubscribe(self, _channel):
            return None

        async def aclose(self):
            return None

    pubsub = PubSub()
    redis = SimpleNamespace(
        initialize=AsyncMock(),
        client=SimpleNamespace(pubsub=lambda: pubsub),
    )
    agent_service = SimpleNamespace(get_all_sessions=AsyncMock(return_value=[]))
    websocket = _SessionListWebSocket()
    monkeypatch.setattr(ws_routes, "resolve_ws_user", resolve_user)
    monkeypatch.setattr(ws_routes, "get_agent_service", lambda: agent_service)
    monkeypatch.setattr(ws_routes, "get_redis", lambda: redis)

    await ws_routes.sessions_list_ws(websocket)

    assert websocket.accepted is True
    assert websocket.closed == (4001, "Unauthorized")
    assert websocket.sent == [{"op": "snapshot", "sessions": []}]
    assert resolve_user.await_count == 2
