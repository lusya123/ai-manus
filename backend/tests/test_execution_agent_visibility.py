from types import MethodType

from app.domain.models.event import MessageEvent, StepEvent, ToolEvent, ToolStatus
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step
from app.domain.services.agents.execution import ExecutionAgent


async def test_execute_step_hides_notify_tool_and_intermediate_step_result():
    agent = object.__new__(ExecutionAgent)

    async def execute(self, _message):
        yield ToolEvent(
            tool_call_id="tool-1",
            tool_name="message",
            function_name="message_notify_user",
            function_args={"text": "internal progress text"},
            status=ToolStatus.CALLING,
        )
        yield ToolEvent(
            tool_call_id="tool-1",
            tool_name="message",
            function_name="message_notify_user",
            function_args={"text": "internal progress text"},
            status=ToolStatus.CALLED,
        )
        yield MessageEvent(
            message='{"success": true, "result": "internal step result", "attachments": []}'
        )

    async def parse_json(self, value):
        return {
            "success": True,
            "result": "internal step result",
            "attachments": [],
        }

    agent.execute = MethodType(execute, agent)
    agent._parse_json = MethodType(parse_json, agent)

    events = [
        event
        async for event in agent.execute_step(
            Plan(language="zh"),
            Step(id="1", description="Do something"),
            Message(message="hello"),
        )
    ]

    assert [event.type for event in events] == ["step", "step"]
    assert all(isinstance(event, StepEvent) for event in events)
    assert not any(isinstance(event, ToolEvent) for event in events)
    assert not any(
        isinstance(event, MessageEvent) and event.message == "internal step result"
        for event in events
    )
