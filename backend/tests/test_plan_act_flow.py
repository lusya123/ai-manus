from app.domain.models.event import (
    DoneEvent,
    ErrorEvent,
    MessageEvent,
    PlanEvent,
    PlanStatus,
    StepEvent,
    StepStatus,
    WaitEvent,
)
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.models.session import Session, SessionStatus
from app.domain.models.agent_output import FinalResult, StepReport
from app.domain.services.agents.base import StructuredOutputEvent
from app.domain.services.agents.execution import ExecutionAgent
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


class _StatusSessionRepository:
    def __init__(self, session):
        self.session = session
        self.statuses = []

    async def find_by_id(self, _session_id):
        return self.session

    async def update_status(self, _session_id, status):
        self.statuses.append(status)


class _PlanningProbe:
    def __init__(self):
        self.create_calls = 0
        self.rollback_calls = 0

    async def create_plan(self, _message):
        self.create_calls += 1
        yield ErrorEvent(error="planning probe stopped")

    async def roll_back(self, _message):
        self.rollback_calls += 1


class _WaitingResumeExecutor:
    def __init__(self):
        self.execute_calls = 0
        self.rollback_calls = 0

    async def roll_back(self, _message):
        self.rollback_calls += 1

    async def execute_step(self, _plan, _step, _message):
        self.execute_calls += 1
        yield WaitEvent()

    async def compact_memory(self):
        return None


def _status_flow(session, planner, executor):
    flow = object.__new__(PlanActFlow)
    flow._agent_id = "agent"
    flow._session_id = session.id
    flow._session_repository = _StatusSessionRepository(session)
    flow.status = AgentStatus.IDLE
    flow.plan = None
    flow.planner = planner
    flow.executor = executor
    return flow


async def test_explicit_new_turn_plans_even_if_session_projection_is_waiting():
    planner = _PlanningProbe()
    executor = _WaitingResumeExecutor()
    flow = _status_flow(
        Session(
            id="session",
            user_id="user",
            agent_id="agent",
            status=SessionStatus.WAITING,
        ),
        planner,
        executor,
    )

    events = [
        event
        async for event in flow.run(
            Message(message="new independent request"),
            resumes_waiting=False,
        )
    ]

    assert [event.type for event in events] == ["error", "done"]
    assert planner.create_calls == 1
    assert planner.rollback_calls == 0
    assert executor.rollback_calls == 0


async def test_explicit_waiting_resume_executes_existing_plan_despite_running_projection():
    plan = Plan(
        title="Need user input",
        steps=[Step(id="step-1", description="Continue after answer")],
    )
    session = Session(
        id="session",
        user_id="user",
        agent_id="agent",
        status=SessionStatus.RUNNING,
        events=[PlanEvent(status=PlanStatus.CREATED, plan=plan)],
    )
    planner = _PlanningProbe()
    executor = _WaitingResumeExecutor()
    flow = _status_flow(session, planner, executor)

    stream = flow.run(
        Message(message="the requested answer"),
        resumes_waiting=True,
    )
    event = await anext(stream)
    await stream.aclose()

    assert isinstance(event, WaitEvent)
    assert executor.execute_calls == 1
    assert executor.rollback_calls == 1
    assert planner.create_calls == 0
    assert planner.rollback_calls == 1


class _ZeroStepPlanner:
    def __init__(self, message=None):
        self.message = message

    async def create_plan(self, _message):
        yield PlanEvent(
            status=PlanStatus.CREATED,
            plan=Plan(title="Direct answer", steps=[], message=self.message),
        )

    async def roll_back(self, _message):
        return None


class _ZeroStepExecutor:
    def __init__(self, summary=None):
        self.summary = summary
        self.summarize_calls = 0
        self.summarize_context = None

    async def roll_back(self, _message):
        return None

    async def summarize(self, plan, message):
        self.summarize_calls += 1
        self.summarize_context = (plan, message)
        if self.summary is not None:
            yield MessageEvent(message=self.summary)


def _zero_step_flow(planner, executor):
    flow = object.__new__(PlanActFlow)
    flow._agent_id = "agent"
    flow._session_id = "session"
    flow._session_repository = _FakeSessionRepository()
    flow.status = AgentStatus.IDLE
    flow.plan = None
    flow.planner = planner
    flow.executor = executor
    return flow


async def test_zero_step_plan_uses_contextual_summary_not_planner_ack():
    executor = _ZeroStepExecutor(summary="The direct answer is 4.")
    flow = _zero_step_flow(
        _ZeroStepPlanner(message="Thanks, I will answer that."), executor
    )

    events = [
        event async for event in flow.run(Message(message="What is 2 + 2?"))
    ]

    messages = [event.message for event in events if isinstance(event, MessageEvent)]
    assert messages == ["The direct answer is 4."]
    assert "Thanks, I will answer that." not in messages
    assert executor.summarize_calls == 1
    summarized_plan, original_message = executor.summarize_context
    assert summarized_plan.steps == []
    assert original_message.message == "What is 2 + 2?"
    assert isinstance(events[-1], DoneEvent)


async def test_zero_step_plan_emits_error_when_summary_is_empty():
    executor = _ZeroStepExecutor(summary=None)
    flow = _zero_step_flow(_ZeroStepPlanner(), executor)

    events = [event async for event in flow.run(Message(message="hello"))]

    errors = [event.error for event in events if isinstance(event, ErrorEvent)]
    assert errors == ["The agent completed without producing a visible response."]
    assert executor.summarize_calls == 1
    assert isinstance(events[-1], DoneEvent)


async def test_zero_step_hidden_only_direct_message_falls_back_to_summary():
    executor = _ZeroStepExecutor(summary="safe summary")
    flow = _zero_step_flow(
        _ZeroStepPlanner(message="<think>private only</think>"), executor
    )

    events = [event async for event in flow.run(Message(message="hello"))]

    messages = [event.message for event in events if isinstance(event, MessageEvent)]
    assert messages == ["safe summary"]
    assert executor.summarize_calls == 1


class _RecoveringPlanner:
    def __init__(self):
        self.update_calls = 0

    async def create_plan(self, _message):
        yield PlanEvent(
            status=PlanStatus.CREATED,
            plan=Plan(
                title="Recover a failed step",
                steps=[Step(id="initial", description="Initial attempt")],
            ),
        )

    async def update_plan(self, plan, step):
        self.update_calls += 1
        if step.id == "initial":
            plan.steps.append(
                Step(id="recovery", description="Recovery attempt")
            )
        yield PlanEvent(status=PlanStatus.UPDATED, plan=plan)

    async def roll_back(self, _message):
        return None


async def test_recoverable_step_error_does_not_terminate_turn_before_recovery():
    executor = object.__new__(ExecutionAgent)
    calls = []

    async def execute(_request, output_tool=None):
        calls.append(output_tool.name)
        if len(calls) == 1:
            yield ErrorEvent(error="temporary structured-output failure")
        elif output_tool.name == "complete_step":
            yield StructuredOutputEvent(
                output=StepReport(success=True, result="recovered")
            )
        else:
            yield StructuredOutputEvent(
                output=FinalResult(message="Recovered final answer")
            )

    async def compact_memory():
        return None

    executor.execute = execute
    executor.compact_memory = compact_memory
    planner = _RecoveringPlanner()
    flow = _zero_step_flow(planner, executor)

    events = [event async for event in flow.run(Message(message="do it"))]

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert [
        event.status for event in events if isinstance(event, StepEvent)
    ] == [
        StepStatus.STARTED,
        StepStatus.FAILED,
        StepStatus.STARTED,
        StepStatus.COMPLETED,
    ]
    assert planner.update_calls == 2
    assert calls == ["complete_step", "complete_step", "deliver_result"]
    assert [
        event.message for event in events if isinstance(event, MessageEvent)
    ] == ["Recovered final answer"]
    assert isinstance(events[-1], DoneEvent)
    assert flow.plan.steps[0].status == ExecutionStatus.FAILED
    assert flow.plan.steps[1].status == ExecutionStatus.COMPLETED


class _UpdateFailingPlanner:
    async def create_plan(self, _message):
        yield PlanEvent(
            status=PlanStatus.CREATED,
            plan=Plan(
                title="Continue without replan",
                steps=[
                    Step(id="one", description="First"),
                    Step(id="two", description="Second"),
                ],
            ),
        )

    async def update_plan(self, _plan, _step):
        yield ErrorEvent(error="planner update temporarily unavailable")

    async def roll_back(self, _message):
        return None


async def test_plan_update_error_does_not_terminate_remaining_execution():
    executor = object.__new__(ExecutionAgent)
    executed_steps = []

    async def execute(_request, output_tool=None):
        if output_tool.name == "complete_step":
            executed_steps.append(len(executed_steps) + 1)
            yield StructuredOutputEvent(
                output=StepReport(success=True, result="completed")
            )
        else:
            yield StructuredOutputEvent(
                output=FinalResult(message="Both steps completed")
            )

    async def compact_memory():
        return None

    executor.execute = execute
    executor.compact_memory = compact_memory
    flow = _zero_step_flow(_UpdateFailingPlanner(), executor)

    events = [event async for event in flow.run(Message(message="do both"))]

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert executed_steps == [1, 2]
    assert [
        event.message for event in events if isinstance(event, MessageEvent)
    ] == ["Both steps completed"]
    assert isinstance(events[-1], DoneEvent)
