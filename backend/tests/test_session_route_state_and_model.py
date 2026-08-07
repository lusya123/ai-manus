import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect

from app.application.errors.exceptions import ServiceUnavailableError, UnauthorizedError
from app.domain.models.agent import Agent
from app.domain.models.file import FileInfo
from app.domain.models.session import Session, SessionStatus, SessionSummary
from app.domain.models.turn_submission import TurnSubmissionState
from app.interfaces.api import session_routes, ws_routes
from app.interfaces.schemas.session import ListSessionItem


def test_session_summary_naive_mongo_timestamp_is_interpreted_as_utc():
    summary = SessionSummary(
        id="session-1",
        user_id="owner",
        latest_message_at=datetime(2026, 7, 16, 18, 30, 0),
    )

    item = ListSessionItem.from_domain(summary)

    assert item.latest_message_at == int(
        datetime(2026, 7, 16, 18, 30, 0, tzinfo=UTC).timestamp()
    )


class _AgentServiceStub:
    def __init__(self, *, session, agent, active_turns):
        self.session = session
        self.agent = agent
        self.active_turns = active_turns
        self.active_turn_lookup = None

    async def get_session(self, session_id, user_id):
        assert session_id == self.session.id
        assert user_id == self.session.user_id
        return self.session

    async def get_agent(self, agent_id):
        assert agent_id == self.session.agent_id
        return self.agent

    async def get_active_turns(self, session_id, user_id):
        self.active_turn_lookup = (session_id, user_id)
        return self.active_turns


def _session(status: SessionStatus) -> Session:
    return Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        status=status,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persisted_status", "active_states", "expected_status"),
    [
        (
            SessionStatus.COMPLETED,
            [TurnSubmissionState.ENQUEUED],
            SessionStatus.PENDING,
        ),
        (
            SessionStatus.COMPLETED,
            [TurnSubmissionState.PENDING, TurnSubmissionState.RUNNING],
            SessionStatus.RUNNING,
        ),
        (SessionStatus.WAITING, [], SessionStatus.WAITING),
    ],
)
async def test_get_session_projects_durable_active_turn_status(
    persisted_status, active_states, expected_status
):
    session = _session(persisted_status)
    service = _AgentServiceStub(
        session=session,
        agent=None,
        active_turns=[SimpleNamespace(state=state) for state in active_states],
    )

    response = await session_routes.get_session(
        session.id,
        current_user=SimpleNamespace(id=session.user_id),
        agent_service=service,
    )

    assert response.data.status == expected_status
    assert service.active_turn_lookup == (session.id, session.user_id)


@pytest.mark.asyncio
async def test_get_session_prefers_persisted_model_id(monkeypatch):
    session = _session(SessionStatus.COMPLETED)
    agent = Agent(
        id=session.agent_id,
        model_id="persisted-catalog-id",
        model_name="claude-sonnet-4-6",
        model_provider="anthropic",
        api_base=None,
    )
    service = _AgentServiceStub(session=session, agent=agent, active_turns=[])
    monkeypatch.setattr(
        session_routes,
        "_model_id_for_agent",
        lambda *_args: pytest.fail("legacy model-id inference was unexpectedly used"),
    )

    response = await session_routes.get_session(
        session.id,
        current_user=SimpleNamespace(id=session.user_id),
        agent_service=service,
    )

    assert response.data.agent_model_config.model_id == "persisted-catalog-id"


@pytest.mark.asyncio
async def test_get_session_falls_back_to_inferred_model_id_for_legacy_agent(
    monkeypatch,
):
    session = _session(SessionStatus.COMPLETED)
    agent = Agent(
        id=session.agent_id,
        model_id=None,
        model_name="legacy-model",
        model_provider="openai",
        api_base="https://legacy.example/v1",
    )
    service = _AgentServiceStub(session=session, agent=agent, active_turns=[])
    monkeypatch.setattr(
        session_routes,
        "_model_id_for_agent",
        lambda *_args: "legacy-derived-id",
    )

    response = await session_routes.get_session(
        session.id,
        current_user=SimpleNamespace(id=session.user_id),
        agent_service=service,
    )

    assert response.data.agent_model_config.model_id == "legacy-derived-id"


class _ScriptedChatWebSocket:
    def __init__(self, messages, *, on_send=None):
        self.messages = list(messages)
        self.accepted = False
        self.sent = []
        self.closed = None
        self.headers = {"authorization": "Bearer opaque-session"}
        self.cookies = {}
        self.on_send = on_send

    async def accept(self):
        self.accepted = True

    async def receive(self):
        if self.messages:
            message = self.messages.pop(0)
            text = message if isinstance(message, str) else json.dumps(message)
            return {"type": "websocket.receive", "text": text}
        # Give the stream task created by the final frame a chance to start.
        await asyncio.sleep(0.01)
        raise WebSocketDisconnect()

    async def send_json(self, payload):
        self.sent.append(payload)
        if self.on_send:
            self.on_send(payload)

    async def close(self, code, reason=None):
        self.closed = (code, reason)


class _ChatWsServiceStub:
    def __init__(self):
        self.chat_calls = []
        self.accept_calls = []
        self.accepted_submission = SimpleNamespace(submission_id="accepted")
        self.session = _session(SessionStatus.COMPLETED)

    async def get_session(self, session_id, user_id):
        assert session_id == self.session.id
        assert user_id == self.session.user_id
        return self.session

    async def chat(self, **kwargs):
        self.chat_calls.append(kwargs)
        if False:
            yield None

    async def accept_chat_submission(self, **kwargs):
        self.accept_calls.append(kwargs)
        return self.accepted_submission


@pytest.mark.asyncio
async def test_chat_ws_accepts_attachment_only_submission(monkeypatch):
    service = _ChatWsServiceStub()
    submission_id = "77777777-7777-4777-8777-777777777777"
    websocket = _ScriptedChatWebSocket(
        [
            {
                "id": "join-1",
                "version": 2,
                "type": "join_session",
                "session_id": service.session.id,
            },
            {
                "id": submission_id,
                "version": 2,
                "type": "chat",
                "session_id": service.session.id,
                "message": "",
                "attachments": [
                    {"file_id": "owned-file", "filename": "report.pdf"}
                ],
            },
        ]
    )
    monkeypatch.setattr(
        ws_routes,
        "resolve_ws_user",
        lambda _websocket: _async_value(
            SimpleNamespace(id=service.session.user_id, is_active=True)
        ),
    )
    monkeypatch.setattr(ws_routes, "get_agent_service", lambda: service)

    await ws_routes.chat_ws(websocket)

    assert service.chat_calls == [
        {
            "session_id": service.session.id,
            "user_id": service.session.user_id,
            "submission_id": submission_id,
            "message": None,
            "timestamp": None,
            "event_id": None,
            "attachments": [
                FileInfo(file_id="owned-file", filename="report.pdf")
            ],
            "accepted_submission": service.accepted_submission,
        }
    ]
    assert service.accept_calls == [
        {
            "session_id": service.session.id,
            "user_id": service.session.user_id,
            "submission_id": submission_id,
            "message": "",
            "timestamp": None,
            "attachments": [
                FileInfo(file_id="owned-file", filename="report.pdf")
            ],
        }
    ]
    assert any(
        frame.get("type") == "ack"
        and frame.get("submission_id") == submission_id
        for frame in websocket.sent
    )


@pytest.mark.asyncio
async def test_chat_ws_join_reconnect_does_not_create_a_new_submission(monkeypatch):
    service = _ChatWsServiceStub()
    websocket = _ScriptedChatWebSocket(
        [
            {
                "id": "join-reconnect",
                "version": 2,
                "type": "join_session",
                "session_id": service.session.id,
                "last_event_id": "durable-cursor",
            }
        ]
    )
    monkeypatch.setattr(
        ws_routes,
        "resolve_ws_user",
        lambda _websocket: _async_value(
            SimpleNamespace(id=service.session.user_id, is_active=True)
        ),
    )
    monkeypatch.setattr(ws_routes, "get_agent_service", lambda: service)

    await ws_routes.chat_ws(websocket)

    assert service.chat_calls == []
    assert any(frame.get("type") == "joined" for frame in websocket.sent)


@pytest.mark.asyncio
async def test_chat_ws_ack_follows_durable_acceptance_and_precedes_stream(monkeypatch):
    trace = []
    service = _ChatWsServiceStub()

    async def accept_chat_submission(**kwargs):
        trace.append("accepted")
        service.accept_calls.append(kwargs)
        return service.accepted_submission

    async def chat(**kwargs):
        trace.append("streamed")
        service.chat_calls.append(kwargs)
        if False:
            yield None

    service.accept_chat_submission = accept_chat_submission
    service.chat = chat
    websocket = _ScriptedChatWebSocket(
        [
            {
                "id": "join-1",
                "version": 2,
                "type": "join_session",
                "session_id": service.session.id,
            },
            {
                "id": "legacy-short-id",
                "version": 2,
                "type": "chat",
                "session_id": service.session.id,
                "message": "hello",
            },
        ],
        on_send=lambda frame: trace.append("ack")
        if frame.get("type") == "ack"
        else None,
    )
    active = SimpleNamespace(id=service.session.user_id, is_active=True)
    monkeypatch.setattr(
        ws_routes,
        "resolve_ws_user",
        AsyncMock(return_value=active),
    )
    monkeypatch.setattr(ws_routes, "get_agent_service", lambda: service)

    await ws_routes.chat_ws(websocket)

    assert trace == ["accepted", "ack", "streamed"]
    ack = next(frame for frame in websocket.sent if frame.get("type") == "ack")
    assert ack["request_id"] == "legacy-short-id"
    assert ack["submission_id"] == ws_routes._chat_submission_id(
        "legacy-short-id",
        user_id=service.session.user_id,
        session_id=service.session.id,
    )
    assert service.chat_calls[0]["accepted_submission"] is service.accepted_submission


@pytest.mark.asyncio
async def test_chat_ws_does_not_ack_when_durable_acceptance_fails(monkeypatch):
    service = _ChatWsServiceStub()

    async def fail_acceptance(**_kwargs):
        raise ServiceUnavailableError("storage details must not leak")

    service.accept_chat_submission = fail_acceptance
    websocket = _ScriptedChatWebSocket(
        [
            {
                "id": "join-1",
                "version": 2,
                "type": "join_session",
                "session_id": service.session.id,
            },
            {
                "id": "chat-1",
                "version": 2,
                "type": "chat",
                "session_id": service.session.id,
                "message": "hello",
            },
        ]
    )
    active = SimpleNamespace(id=service.session.user_id, is_active=True)
    monkeypatch.setattr(ws_routes, "resolve_ws_user", AsyncMock(return_value=active))
    monkeypatch.setattr(ws_routes, "get_agent_service", lambda: service)

    await ws_routes.chat_ws(websocket)

    assert not any(frame.get("type") == "ack" for frame in websocket.sent)
    error = next(frame for frame in websocket.sent if frame.get("type") == "error")
    assert error["code"] == 503
    assert "storage details" not in error["error"]
    assert service.chat_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["chat", "stop_session"])
async def test_chat_ws_rechecks_opaque_session_before_each_mutation(
    monkeypatch, command
):
    service = _ChatWsServiceStub()
    service.stop_session = AsyncMock()
    frame = {
        "id": "mutation-1",
        "version": 2,
        "type": command,
        "session_id": service.session.id,
    }
    if command == "chat":
        frame["message"] = "must not run"
    websocket = _ScriptedChatWebSocket(
        [
            {
                "id": "join-1",
                "version": 2,
                "type": "join_session",
                "session_id": service.session.id,
            },
            frame,
        ]
    )
    resolve_user = AsyncMock(
        side_effect=[
            SimpleNamespace(id=service.session.user_id, is_active=True),
            SimpleNamespace(id=service.session.user_id, is_active=True),
            UnauthorizedError("revoked"),
        ]
    )
    monkeypatch.setattr(ws_routes, "resolve_ws_user", resolve_user)
    monkeypatch.setattr(ws_routes, "get_agent_service", lambda: service)

    await ws_routes.chat_ws(websocket)

    assert resolve_user.await_count == 3
    assert websocket.closed == (4001, "Unauthorized")
    assert service.accept_calls == []
    service.stop_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"message": "x" * (64 * 1024 + 1)},
        {
            "message": "hello",
            "attachments": [
                {"file_id": f"file-{index}", "filename": "a.txt"}
                for index in range(11)
            ],
        },
        {"message": "hello", "attachments": [{"file_id": "x" * 257}]},
        {
            "message": "hello",
            "attachments": [{"file_id": "file-1", "filename": "x" * 513}],
        },
        {"message": "hello", "last_event_id": "x" * 129},
        {"id": "x" * 129, "message": "hello"},
    ],
)
async def test_chat_ws_enforces_chat_request_input_bounds(monkeypatch, invalid_fields):
    service = _ChatWsServiceStub()
    chat_frame = {
        "id": "chat-1",
        "version": 2,
        "type": "chat",
        "session_id": service.session.id,
        **invalid_fields,
    }
    websocket = _ScriptedChatWebSocket(
        [
            {
                "id": "join-1",
                "version": 2,
                "type": "join_session",
                "session_id": service.session.id,
            },
            chat_frame,
        ]
    )
    active = SimpleNamespace(id=service.session.user_id, is_active=True)
    monkeypatch.setattr(ws_routes, "resolve_ws_user", AsyncMock(return_value=active))
    monkeypatch.setattr(ws_routes, "get_agent_service", lambda: service)

    await ws_routes.chat_ws(websocket)

    assert service.accept_calls == []
    assert not any(frame.get("type") == "ack" for frame in websocket.sent)
    assert any(
        frame.get("type") == "error" and frame.get("code") == ws_routes.ERR_BAD_REQUEST
        for frame in websocket.sent
    )


@pytest.mark.asyncio
async def test_chat_ws_rejects_oversized_frame_before_json_decode(monkeypatch):
    service = _ChatWsServiceStub()
    websocket = _ScriptedChatWebSocket(["{" + ("x" * 64)])
    active = SimpleNamespace(id=service.session.user_id, is_active=True)
    monkeypatch.setattr(ws_routes, "resolve_ws_user", AsyncMock(return_value=active))
    monkeypatch.setattr(ws_routes, "get_agent_service", lambda: service)
    monkeypatch.setattr(
        ws_routes,
        "get_settings",
        lambda: SimpleNamespace(chat_max_body_bytes=32),
    )

    await ws_routes.chat_ws(websocket)

    assert websocket.closed == (1009, "Message too large")
    assert service.accept_calls == []


def test_legacy_chat_request_id_maps_to_stable_uuid():
    first = ws_routes._chat_submission_id(
        "legacy-short-id", user_id="user-1", session_id="session-1"
    )
    second = ws_routes._chat_submission_id(
        "legacy-short-id", user_id="user-1", session_id="session-1"
    )

    assert first == second
    assert str(uuid.UUID(first)) == first


async def _async_value(value):
    return value
