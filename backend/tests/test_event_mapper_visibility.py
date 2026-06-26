from app.domain.models.event import MessageEvent, PlanEvent, PlanStatus, StepEvent, StepStatus, ToolEvent, ToolStatus
from app.domain.models.plan import Plan, Step
from app.interfaces.schemas.event import EventMapper


async def test_event_mapper_filters_message_notify_user_tool_events():
    event = ToolEvent(
        tool_call_id="tool-1",
        tool_name="message",
        function_name="message_notify_user",
        function_args={"text": "internal progress text"},
        status=ToolStatus.CALLING,
    )

    assert await EventMapper.event_to_sse_event(event) is None
    assert await EventMapper.events_to_sse_events([event]) == []


async def test_event_mapper_filters_internal_plan_messages_and_preserves_step_results():
    step = Step(
        id="1",
        description="Internal execution step",
        result="internal step result",
    )
    plan = Plan(
        message="internal planner message",
        steps=[step],
    )
    events = [
        MessageEvent(role="user", message="hello"),
        PlanEvent(status=PlanStatus.CREATED, plan=plan),
        StepEvent(status=StepStatus.STARTED, step=step),
        MessageEvent(message="internal planner message"),
        MessageEvent(message="internal step result"),
        MessageEvent(message="visible final answer"),
    ]

    mapped = await EventMapper.events_to_sse_events(events)

    assert [event.event for event in mapped] == ["message", "message", "message"]
    assert mapped[0].data.content == "hello"
    assert mapped[1].data.content == "internal step result"
    assert mapped[2].data.content == "visible final answer"
