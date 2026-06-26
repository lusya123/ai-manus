from app.domain.models.event import DoneEvent, ErrorEvent
from app.domain.models.message import Message
from app.domain.models.session import Session, SessionStatus
from app.domain.services.flows.plan_act import AgentStatus, PlanActFlow


class _FakeSessionRepository:
    async def find_by_id(self, _session_id):
        return Session(user_id="user", agent_id="agent", status=SessionStatus.PENDING)

    async def update_status(self, _session_id, _status):
        return None


class _FailingPlanner:
    async def create_plan(self, _message):
        yield ErrorEvent(error="planner failed")

    async def roll_back(self, _message):
        return None


class _DummyExecutor:
    async def roll_back(self, _message):
        return None


async def test_plan_act_flow_finishes_when_planner_emits_error_without_plan():
    flow = object.__new__(PlanActFlow)
    flow._agent_id = "agent"
    flow._session_id = "session"
    flow._session_repository = _FakeSessionRepository()
    flow.status = AgentStatus.IDLE
    flow.plan = None
    flow.planner = _FailingPlanner()
    flow.executor = _DummyExecutor()

    events = [event async for event in flow.run(Message(message="hello"))]

    assert [event.type for event in events] == ["error", "done"]
    assert isinstance(events[0], ErrorEvent)
    assert isinstance(events[1], DoneEvent)
    assert flow.status == AgentStatus.IDLE
