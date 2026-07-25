from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.domain.models.agent import Agent
from app.domain.models.file import FileInfo
from app.domain.models.session import Session, SessionStatus, SessionSummary
from app.domain.models.turn_submission import TurnSubmissionState
from app.interfaces.api import session_routes
from app.interfaces.schemas.session import ChatRequest, ListSessionItem


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


class _ChatRouteServiceStub:
    def __init__(self):
        self.accept_calls = []

    async def accept_chat_submission(self, **kwargs):
        self.accept_calls.append(kwargs)
        return SimpleNamespace(submission_id=kwargs["submission_id"])

    async def chat(self, **_kwargs):
        if False:
            yield None


@pytest.mark.asyncio
async def test_chat_route_accepts_attachment_only_before_sse_headers():
    service = _ChatRouteServiceStub()
    submission_id = "77777777-7777-4777-8777-777777777777"

    await session_routes.chat(
        "session-attachment",
        ChatRequest(
            message="",
            submission_id=submission_id,
            attachments=[{"file_id": "owned-file", "filename": "report.pdf"}],
        ),
        current_user=SimpleNamespace(id="owner"),
        agent_service=service,
    )

    assert service.accept_calls == [
        {
            "session_id": "session-attachment",
            "user_id": "owner",
            "submission_id": submission_id,
            "message": "",
            "timestamp": None,
            "attachments": [
                FileInfo(file_id="owned-file", filename="report.pdf")
            ],
        }
    ]


@pytest.mark.asyncio
async def test_chat_route_does_not_accept_empty_reconnect_as_new_submission():
    service = _ChatRouteServiceStub()

    await session_routes.chat(
        "session-attachment",
        ChatRequest(
            message="",
            event_id="durable-cursor",
            submission_id="88888888-8888-4888-8888-888888888888",
            attachments=[],
        ),
        current_user=SimpleNamespace(id="owner"),
        agent_service=service,
    )

    assert service.accept_calls == []
