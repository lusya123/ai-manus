import asyncio
from types import SimpleNamespace
from typing import Optional

import pytest

from app.domain.models.agent_output import FinalResult
from app.domain.models.event import (
    DoneEvent,
    MessageDeltaEvent,
    MessageEvent,
    PlanEvent,
    PlanStatus,
    StepEvent,
    StepStatus,
    TitleEvent,
    ToolEvent,
    WaitEvent,
)
from app.domain.models.memory import Memory
from app.domain.models.message import LLMMessage, Message, Role, SkillContext, ToolCall
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.models.session import SessionStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.manus import ManusAgent
from app.domain.services.agents.base import StructuredOutputEvent
from app.domain.services.flows.agent_loop import AgentLoopFlow
from app.domain.services.tools.base import OutputTool
from app.domain.services.tools.file import FileToolkit
from app.domain.services.tools.message import MessageToolkit
from app.domain.services.tools.plan import PlanToolkit
from app.domain.utils.streaming import LLMStreamChunk

from tests.harness import (
    FakeAgentRepository,
    FakeSandbox,
    FakeSession,
    ScriptedLLM,
    StubAgent,
    build_agent_loop_flow,
)

DELIVER_RESULT = OutputTool(
    name="deliver_result",
    description="Deliver the final result.",
    schema=FinalResult,
)


def _work_response(call_id: str = "work-1") -> LLMMessage:
    return LLMMessage.assistant(tool_calls=[ToolCall(
        id=call_id, name="file_read", args={"file": "/home/ubuntu/task.txt"},
    )])

def _flow(
    llm: ScriptedLLM,
    *,
    session: Optional[FakeSession] = None,
    agent_repository: Optional[FakeAgentRepository] = None,
) -> AgentLoopFlow:
    return build_agent_loop_flow(
        llm, session=session, agent_repository=agent_repository
    )


@pytest.mark.asyncio
async def test_agent_loop_flow_create_plan_then_manus_deliver():
    llm = ScriptedLLM([
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="plan-1",
                name="create_plan",
                args={
                    "message": "I will do the work.",
                    "language": "en",
                    "title": "Do the work",
                    "goal": "Finish the requested work",
                    "steps": [{"id": "1", "description": "Do the work"}],
                },
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="notify-1",
                name="message_notify_user",
                args={"text": "Starting now."},
            ),
        ]),
        _work_response(),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="report-1",
                name="plan_report",
                args={
                    "steps": [{
                        "id": "1",
                        "status": "completed",
                        "reflection": "Work finished",
                    }],
                    "reflection": "All work complete",
                },
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="result-1",
                name="deliver_result",
                args={"message": "Finished in one loop", "attachments": []},
            ),
        ]),
    ])
    flow = _flow(llm)

    events = []
    created_step_status = None
    async for event in flow.run(Message(message="Do it")):
        if isinstance(event, PlanEvent) and event.status == PlanStatus.CREATED:
            created_step_status = event.plan.steps[0].status
        events.append(event)

    plan_events = [event for event in events if isinstance(event, PlanEvent)]
    assert [event.status for event in plan_events] == [
        PlanStatus.CREATED,
        PlanStatus.UPDATED,
        PlanStatus.COMPLETED,
    ]
    assert created_step_status == ExecutionStatus.RUNNING
    assert any(
        isinstance(event, StepEvent)
        and event.status == StepStatus.STARTED
        and event.step.id == "1"
        for event in events
    )
    assert plan_events[1].plan.steps[0].result == "Work finished"
    assert any(
        isinstance(event, TitleEvent) and event.title == "Do the work"
        for event in events
    )
    assert any(
        isinstance(event, MessageEvent)
        and event.message == "Finished in one loop"
        for event in events
    )
    assert not any(
        isinstance(event, ToolEvent)
        and event.function_name in {"plan_report", "replan"}
        for event in events
    )
    assert isinstance(events[-1], DoneEvent)
    assert flow.is_done()
    assert llm.responses == []


@pytest.mark.asyncio
async def test_agent_loop_blocks_work_tools_after_plan_finished():
    """Once every plan step is done, further shell/file work must be rejected."""
    sandbox = FakeSandbox()
    llm = ScriptedLLM([
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="plan-1",
                name="create_plan",
                args={
                    "message": "I will do the work.",
                    "language": "en",
                    "title": "Do the work",
                    "goal": "Finish",
                    "steps": [{"id": "1", "description": "Do the work"}],
                },
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="notify-1",
                name="message_notify_user",
                args={"text": "Starting."},
            ),
        ]),
        _work_response(),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="report-1",
                name="plan_report",
                args={"steps": [{"id": "1", "status": "completed"}]},
            ),
        ]),
        # Model wrongly keeps working after the plan is finished.
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="shell-1",
                name="shell_exec",
                args={
                    "id": "main",
                    "exec_dir": "/home/ubuntu",
                    "command": "echo should-not-run",
                },
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="result-1",
                name="deliver_result",
                args={"message": "Done", "attachments": []},
            ),
        ]),
    ])
    flow = build_agent_loop_flow(llm, sandbox=sandbox)

    events = [event async for event in flow.run(Message(message="Do it"))]

    assert sandbox.shell_exec_calls == 0
    # Hint must be visible to the model after plan_report.
    plan_report_tool_msgs = [
        msg
        for call in llm.calls
        for msg in call
        if msg.role == Role.TOOL and msg.name == "plan_report"
    ]
    assert plan_report_tool_msgs
    assert any(
        "deliver_result" in (msg.content or "")
        for msg in plan_report_tool_msgs
    )
    assert any(
        isinstance(event, MessageEvent) and event.message == "Done"
        for event in events
    )
    assert isinstance(events[-1], DoneEvent)
    assert llm.responses == []


@pytest.mark.asyncio
async def test_agent_loop_flow_empty_plan_requires_manus_delivery():
    llm = ScriptedLLM([
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="plan-1",
                name="create_plan",
                args={
                    "message": "This needs no tool work.",
                    "language": "en",
                    "title": "Direct answer",
                    "goal": "",
                    "steps": [],
                },
            ),
        ]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="result-1", name="deliver_result",
            args={"message": "This needs no tool work.", "attachments": []},
        )]),
    ])
    flow = _flow(llm)

    events = [event async for event in flow.run(Message(message="Answer it"))]

    assert any(
        isinstance(event, MessageEvent)
        and event.message == "This needs no tool work."
        for event in events
    )
    assert [event.status for event in events if isinstance(event, PlanEvent)] == [
        PlanStatus.CREATED,
        PlanStatus.COMPLETED,
    ]
    assert not any(isinstance(event, ToolEvent) for event in events)
    assert isinstance(events[-1], DoneEvent)
    assert llm.responses == []


@pytest.mark.asyncio
async def test_agent_loop_flow_replan_is_intercepted_and_uses_planner():
    llm = ScriptedLLM([
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="plan-1",
                name="create_plan",
                args={
                    "message": "I will investigate.",
                    "language": "en",
                    "title": "Investigate",
                    "goal": "Resolve the issue",
                    "steps": [{"id": "1", "description": "Try the original route"}],
                },
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="notify-1",
                name="message_notify_user",
                args={"text": "Starting the investigation."},
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="replan-1",
                name="replan",
                args={"reason": "The original route is unavailable"},
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="update-1",
                name="update_plan",
                args={
                    "steps": [
                        {"id": "2", "description": "Use the fallback route"},
                    ],
                },
            ),
        ]),
        _work_response(),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="report-1", name="plan_report",
            args={"steps": [{"id": "2", "status": "completed"}]},
        )]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="result-1",
                name="deliver_result",
                args={"message": "Resolved with fallback", "attachments": []},
            ),
        ]),
    ])
    flow = _flow(llm)

    events = [event async for event in flow.run(Message(message="Investigate"))]

    updated = next(
        event for event in events
        if isinstance(event, PlanEvent) and event.status == PlanStatus.UPDATED
    )
    assert updated.plan.steps[0].id == "2"
    assert not any(
        isinstance(event, ToolEvent) and event.function_name == "replan"
        for event in events
    )
    replan_tool_reply = next(
        message for message in llm.calls[-1]
        if message.role == Role.TOOL and message.name == "replan"
    )
    assert "Use the fallback route" in replan_tool_reply.content
    assert isinstance(events[-1], DoneEvent)
    assert llm.responses == []


@pytest.mark.asyncio
async def test_agent_loop_flow_invalid_plan_report_allows_model_repair():
    llm = ScriptedLLM([
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="plan-1",
                name="create_plan",
                args={
                    "message": "I will do the work.",
                    "language": "en",
                    "title": "Repair report",
                    "goal": "Finish safely",
                    "steps": [{"id": "1", "description": "Do the work"}],
                },
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="notify-1",
                name="message_notify_user",
                args={"text": "Starting."},
            ),
        ]),
        _work_response(),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="bad-report",
                name="plan_report",
                args={"steps": [{"id": "1", "status": "not-a-status"}]},
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="good-report",
                name="plan_report",
                args={"steps": [{"id": "1", "status": "completed"}]},
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="result-1",
                name="deliver_result",
                args={"message": "Recovered", "attachments": []},
            ),
        ]),
    ])
    flow = _flow(llm)
    flow.agent.max_retries = 0

    events = [event async for event in flow.run(Message(message="Do it"))]

    assert any(
        isinstance(event, PlanEvent) and event.status == PlanStatus.UPDATED
        for event in events
    )
    assert isinstance(events[-1], DoneEvent)
    assert llm.responses == []


@pytest.mark.asyncio
async def test_agent_loop_flow_invalid_replan_allows_model_repair():
    llm = ScriptedLLM([
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="plan-1",
                name="create_plan",
                args={
                    "message": "I will do the work.",
                    "language": "en",
                    "title": "Repair replan",
                    "goal": "Finish safely",
                    "steps": [{"id": "1", "description": "Do the work"}],
                },
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="notify-1",
                name="message_notify_user",
                args={"text": "Starting."},
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(id="bad-replan", name="replan", args={"reason": ""}),
        ]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="good-replan", name="replan", args={"reason": "Need a better route"},
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="update-1", name="update_plan",
            args={"steps": [{"id": "2", "description": "Use the better route"}]},
        )]),
        _work_response(),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="report-1", name="plan_report",
            args={"steps": [{"id": "2", "status": "completed"}]},
        )]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="result-1",
                name="deliver_result",
                args={"message": "Recovered", "attachments": []},
            ),
        ]),
    ])
    flow = _flow(llm)
    flow.agent.max_retries = 0

    events = [event async for event in flow.run(Message(message="Do it"))]

    assert any(
        isinstance(event, MessageEvent) and event.message == "Recovered"
        for event in events
    )
    assert isinstance(events[-1], DoneEvent)
    assert llm.responses == []


@pytest.mark.asyncio
async def test_agent_loop_flow_replan_after_all_steps_completed_keeps_new_steps():
    llm = ScriptedLLM([
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="plan-1",
                name="create_plan",
                args={
                    "message": "I will investigate.",
                    "language": "en",
                    "title": "Extend work",
                    "goal": "Complete all required work",
                    "steps": [{"id": "1", "description": "Initial check"}],
                },
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="notify-1",
                name="message_notify_user",
                args={"text": "Starting."},
            ),
        ]),
        _work_response(),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="report-1",
                name="plan_report",
                args={"steps": [{"id": "1", "status": "completed"}]},
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="replan-1",
                name="replan",
                args={"reason": "A follow-up is required"},
            ),
        ]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="update-1",
                name="update_plan",
                args={"steps": [{"id": "2", "description": "Follow up"}]},
            ),
        ]),
        _work_response("work-2"),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="report-2", name="plan_report",
            args={"steps": [
                {"id": "1", "status": "completed"},
                {"id": "2", "status": "completed"},
            ]},
        )]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="result-1",
                name="deliver_result",
                args={"message": "Follow-up complete", "attachments": []},
            ),
        ]),
    ])
    flow = _flow(llm)

    events = [event async for event in flow.run(Message(message="Investigate"))]

    replanned = [
        event for event in events
        if isinstance(event, PlanEvent) and event.status == PlanStatus.UPDATED
    ][-1]
    assert [step.id for step in replanned.plan.steps] == ["1", "2"]
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_agent_loop_flow_wait_does_not_emit_done():
    flow = _flow(ScriptedLLM([
        LLMMessage.assistant(tool_calls=[
            ToolCall(
                id="plan-1",
                name="create_plan",
                args={
                    "message": "I need one choice.",
                    "language": "en",
                    "title": "Choose",
                    "goal": "Use the selected option",
                    "steps": [{"id": "1", "description": "Use the option"}],
                },
            ),
        ]),
        LLMMessage.assistant(
            tool_calls=[
                ToolCall(
                    id="ask-1",
                    name="message_ask_user",
                    args={"text": "Which option?"},
                ),
            ]
        ),
    ]))

    events = [event async for event in flow.run(Message(message="Do it"))]

    assert any(isinstance(event, WaitEvent) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert not any(
        isinstance(event, PlanEvent) and event.status == PlanStatus.COMPLETED
        for event in events
    )
    assert not flow.is_done()


@pytest.mark.asyncio
@pytest.mark.parametrize(("session_status", "resumes_waiting"), [
    (SessionStatus.WAITING, None),
    (SessionStatus.RUNNING, True),
])
async def test_agent_loop_waiting_resume_skips_create_plan(session_status, resumes_waiting):
    agent_repository = FakeAgentRepository()
    memory = Memory(messages=[
        LLMMessage.system("old system prompt"),
        LLMMessage.assistant(
            tool_calls=[
                ToolCall(
                    id="ask-1",
                    name="message_ask_user",
                    args={"text": "Which option?"},
                ),
            ]
        ),
    ])
    await agent_repository.save_memory("agent-1", ManusAgent.name, memory)
    plan = Plan(
        title="Choose an option",
        steps=[
            Step(
                id="choose",
                description="Choose the preferred option",
                status=ExecutionStatus.RUNNING,
            ),
        ],
    )
    llm = ScriptedLLM([
            _work_response(),
            LLMMessage.assistant(tool_calls=[ToolCall(
                id="report-1", name="plan_report",
                args={"steps": [{"id": "choose", "status": "completed"}]},
            )]),
            LLMMessage.assistant(
                tool_calls=[
                    ToolCall(
                        id="result-1",
                        name="deliver_result",
                        args={"message": "Used option B", "attachments": []},
                    ),
                ]
            ),
        ])
    flow = _flow(
        llm,
        session=FakeSession(status=session_status, plan=plan),
        agent_repository=agent_repository,
    )

    events = [
        event async for event in flow.run(
            Message(message="Use option B"), resumes_waiting=resumes_waiting
        )
    ]

    assert any(
        isinstance(event, MessageEvent) and event.message == "Used option B"
        for event in events
    )
    completed_plan = next(
        event for event in events
        if isinstance(event, PlanEvent) and event.status == PlanStatus.COMPLETED
    )
    assert completed_plan.plan.steps[0].id == "choose"
    assert completed_plan.plan.steps[0].status == ExecutionStatus.COMPLETED
    assert not any(
        message.role == Role.USER
        for message in memory.get_messages()
    )
    assert any(
        message.role == Role.TOOL and message.content == "Use option B"
        for message in memory.get_messages()
    )
    assert isinstance(events[-1], DoneEvent)
    assert llm.responses == []
@pytest.mark.asyncio
async def test_continue_execute_does_not_append_user_message():
    """Continuing after a tool reply must not insert another user turn."""
    repository = FakeAgentRepository()
    memory = Memory(messages=[
        LLMMessage.system("test system prompt"),
        LLMMessage.user("original request"),
        LLMMessage.assistant(
            tool_calls=[
                ToolCall(id="ask-1", name="message_ask_user", args={"text": "Which?"}),
            ]
        ),
        LLMMessage.tool(
            tool_call_id="ask-1",
            name="message_ask_user",
            content="Use option B",
        ),
    ])
    await repository.save_memory("agent-1", StubAgent.name, memory)
    llm = ScriptedLLM([
        LLMMessage.assistant(
            tool_calls=[
                ToolCall(
                    id="result-1",
                    name="deliver_result",
                    args={"message": "Done with option B", "attachments": []},
                ),
            ]
        ),
    ])
    agent = StubAgent(
        agent_id="agent-1",
        agent_repository=repository,
        llm=llm,
    )
    user_messages_before = sum(
        message.role == Role.USER for message in memory.get_messages()
    )

    events = [
        event async for event in agent.continue_execute(output_tool=DELIVER_RESULT)
    ]

    user_messages_after = sum(
        message.role == Role.USER for message in memory.get_messages()
    )
    assert user_messages_after == user_messages_before
    assert any(isinstance(event, StructuredOutputEvent) for event in events)


@pytest.mark.asyncio
async def test_manus_run_todo_md_does_not_emit_plan():
    """todo.md is a normal file write — no checklist parsing / Plan projection."""
    repository = FakeAgentRepository()
    llm = ScriptedLLM([
        LLMMessage.assistant(
            tool_calls=[
                ToolCall(
                    id="n1",
                    name="message_notify_user",
                    args={"text": "I'll research this."},
                ),
                ToolCall(
                    id="todo-md",
                    name="file_write",
                    args={
                        "file": "/home/ubuntu/todo.md",
                        "content": "# Plan\n- [~] Research\n- [ ] Deliver\n",
                    },
                ),
            ]
        ),
        LLMMessage.assistant(
            tool_calls=[
                ToolCall(
                    id="result-1",
                    name="deliver_result",
                    args={"message": "Research complete", "attachments": []},
                ),
            ]
        ),
    ])
    agent = ManusAgent(
        agent_id="agent-1",
        agent_repository=repository,
        llm=llm,
        tools=[FileToolkit(FakeSandbox()), MessageToolkit()],
    )

    events = [event async for event in agent.run(Message(message="Research this"))]

    assert not any(isinstance(event, PlanEvent) for event in events)
    assert any(
        isinstance(event, TitleEvent) and event.title == "Research this"
        for event in events
    )
    assert any(
        isinstance(event, MessageEvent)
        and event.message == "Research complete"
        for event in events
    )
    assert any(
        isinstance(event, ToolEvent) and event.function_name == "file_write"
        for event in events
    )


@pytest.mark.asyncio
async def test_manus_ask_user_yields_wait_and_stops():
    repository = FakeAgentRepository()
    llm = ScriptedLLM([
        LLMMessage.assistant(
            tool_calls=[
                ToolCall(
                    id="ask-1",
                    name="message_ask_user",
                    args={"text": "Which option should I use?"},
                ),
            ]
        ),
    ])
    agent = ManusAgent(
        agent_id="agent-1",
        agent_repository=repository,
        llm=llm,
        tools=[MessageToolkit()],
    )

    events = [event async for event in agent.run(Message(message="Do the task"))]

    assert any(
        isinstance(event, MessageEvent)
        and event.message == "Which option should I use?"
        for event in events
    )
    assert any(isinstance(event, WaitEvent) for event in events)


@pytest.mark.asyncio
async def test_manus_emits_fallback_title_when_delivering_without_todos():
    repository = FakeAgentRepository()
    llm = ScriptedLLM([
        LLMMessage.assistant(
            tool_calls=[
                ToolCall(
                    id="result-1",
                    name="deliver_result",
                    args={"message": "Done", "attachments": []},
                ),
            ]
        ),
    ])
    agent = ManusAgent(
        agent_id="agent-1",
        agent_repository=repository,
        llm=llm,
        tools=[MessageToolkit()],
    )

    events = [event async for event in agent.run(Message(message="Quick task"))]

    assert any(
        isinstance(event, TitleEvent) and event.title == "Quick task"
        for event in events
    )


@pytest.mark.asyncio
async def test_manus_plan_report_yields_tool_event_after_notify():
    """Manus yields plan_report ToolEvents for Flow interception; does not swallow."""
    repository = FakeAgentRepository()
    llm = ScriptedLLM([
        LLMMessage.assistant(
            tool_calls=[
                ToolCall(
                    id="n1",
                    name="message_notify_user",
                    args={"text": "Starting the planned work."},
                ),
                ToolCall(
                    id="report-1",
                    name="plan_report",
                    args={
                        "steps": [{"id": "1", "status": "running"}],
                        "reflection": "",
                    },
                ),
            ]
        ),
        LLMMessage.assistant(
            tool_calls=[
                ToolCall(
                    id="result-1",
                    name="deliver_result",
                    args={"message": "Done", "attachments": []},
                ),
            ]
        ),
    ])
    agent = ManusAgent(
        agent_id="agent-1",
        agent_repository=repository,
        llm=llm,
        tools=[MessageToolkit(), PlanToolkit()],
    )

    events = [event async for event in agent.run(Message(message="Do the plan"))]

    plan_report_events = [
        event
        for event in events
        if isinstance(event, ToolEvent) and event.function_name == "plan_report"
    ]
    assert len(plan_report_events) >= 1
    assert not any(isinstance(event, PlanEvent) for event in events)


@pytest.mark.asyncio
async def test_manus_blocks_plan_report_until_notify():
    repository = FakeAgentRepository()
    plan_toolkit = PlanToolkit()
    agent = ManusAgent(
        agent_id="agent-1",
        agent_repository=repository,
        llm=ScriptedLLM([]),
        tools=[plan_toolkit],
    )

    blocked = await agent.invoke_tool(
        plan_toolkit.get_tool("plan_report"),
        ToolCall(
            id="report-1",
            name="plan_report",
            args={"steps": [{"id": "1", "status": "running"}]},
        ),
    )
    assert "message_notify_user" in blocked.content


@pytest.mark.asyncio
async def test_manus_blocks_work_tools_until_notify():
    repository = FakeAgentRepository()
    agent = ManusAgent(
        agent_id="agent-1",
        agent_repository=repository,
        llm=ScriptedLLM([]),
        tools=[FileToolkit(FakeSandbox())],
    )

    class StubWorkTool:
        name = "file_write"
        toolkit = SimpleNamespace(name="file")
        called = False

        async def invoke(self, args):
            self.called = True
            return ToolResult(success=True, message="written")

    work = StubWorkTool()
    blocked = await agent.invoke_tool(
        work,
        ToolCall(id="fw-1", name="file_write", args={"file": "/tmp/x", "content": "x"}),
    )
    assert work.called is False
    assert "message_notify_user" in blocked.content

    agent._user_notified = True
    allowed = await agent.invoke_tool(
        work,
        ToolCall(id="fw-2", name="file_write", args={"file": "/tmp/x", "content": "x"}),
    )
    assert work.called is True
    assert "written" in allowed.content


@pytest.mark.asyncio
async def test_manus_notify_surfaces_as_message_event():
    repository = FakeAgentRepository()
    llm = ScriptedLLM([
        LLMMessage.assistant(
            tool_calls=[
                ToolCall(
                    id="n1",
                    name="message_notify_user",
                    args={"text": "好的，我来写一个 Python 示例。"},
                ),
            ]
        ),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="r1", name="deliver_result",
            args={"message": "完成", "attachments": []},
        )]),
    ])
    agent = ManusAgent(
        agent_id="agent-1",
        agent_repository=repository,
        llm=llm,
        tools=[MessageToolkit()],
    )

    events = [event async for event in agent.run(Message(message="写一个 python 示例"))]

    assert any(
        isinstance(event, MessageEvent)
        and event.message == "好的，我来写一个 Python 示例。"
        for event in events
    )
    assert agent._user_notified is True
    assert not any(
        isinstance(event, ToolEvent) and event.function_name == "message_notify_user"
        for event in events
    )


@pytest.mark.asyncio
async def test_single_loop_rejects_early_delivery_until_plan_is_reported():
    llm = ScriptedLLM([
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="plan-1", name="create_plan", args={
                "message": "I'll check the file.", "language": "en",
                "title": "Check file", "goal": "Check the requested file",
                "steps": [{"id": "1", "description": "Read the file"}],
            },
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="notify-1", name="message_notify_user", args={"text": "Checking."},
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="early", name="deliver_result",
            args={"message": "Unverified", "attachments": []},
        )]),
        _work_response(),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="report-1", name="plan_report",
            args={"steps": [{"id": "1", "status": "completed"}]},
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="final", name="deliver_result",
            args={"message": "Verified", "attachments": []},
        )]),
    ])
    flow = _flow(llm)

    events = [event async for event in flow.run(Message(message="Check it"))]

    assert not any(isinstance(event, MessageEvent) and event.message == "Unverified" for event in events)
    assert any(isinstance(event, MessageEvent) and event.message == "Verified" for event in events)
    assert any(
        message.role == Role.TOOL and message.name == "deliver_result"
        and "Complete or fail all authoritative plan steps" in message.content
        for call in llm.calls for message in call
    )
    assert isinstance(events[-1], DoneEvent)
    assert llm.responses == []


@pytest.mark.asyncio
async def test_single_loop_rejects_completed_report_without_successful_work():
    llm = ScriptedLLM([
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="plan-1", name="create_plan", args={
                "message": "I'll do the task.", "language": "en",
                "title": "Do task", "goal": "Finish task",
                "steps": [{"id": "1", "description": "Read the file"}],
            },
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="notify-1", name="message_notify_user", args={"text": "Starting."},
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="premature-report", name="plan_report",
            args={"steps": [{"id": "1", "status": "completed"}]},
        )]),
        _work_response(),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="valid-report", name="plan_report",
            args={"steps": [{"id": "1", "status": "completed"}]},
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="final", name="deliver_result",
            args={"message": "Finished after reading", "attachments": []},
        )]),
    ])
    flow = _flow(llm)

    events = [event async for event in flow.run(Message(message="Do it"))]

    assert any(
        message.role == Role.TOOL and message.name == "plan_report"
        and "perform successful work" in message.content
        for call in llm.calls for message in call
    )
    assert [event.status for event in events if isinstance(event, PlanEvent)] == [
        PlanStatus.CREATED, PlanStatus.UPDATED, PlanStatus.COMPLETED,
    ]
    assert isinstance(events[-1], DoneEvent)
    assert llm.responses == []


@pytest.mark.asyncio
async def test_single_loop_loads_enabled_skill_before_reporting_completion():
    llm = ScriptedLLM([
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="plan-1", name="create_plan", args={
                "message": "I'll load the skill.", "language": "en",
                "title": "Use skill", "goal": "Follow the skill",
                "steps": [{"id": "1", "description": "Research the task"}],
            },
        )]),
    ])
    flow = _flow(llm)
    flow.set_enabled_skills(
        [("demo-skill", "Demo instructions")],
        {"demo-skill": "# Demo Skill\nFollow the verified steps."},
    )

    events = []
    async for event in flow.run(Message(
        message="Use the skill",
        skill=SkillContext(skill_id="skill_demo_skill", name="demo-skill", body=""),
    )):
        events.append(event)
        if isinstance(event, PlanEvent) and event.status == PlanStatus.CREATED:
            skill_step, research_step = event.plan.steps
            assert skill_step.description == "Load demo-skill skill"
            llm.responses.extend([
                LLMMessage.assistant(tool_calls=[ToolCall(
                    id="notify-1", name="message_notify_user", args={"text": "Loading skill."},
                )]),
                LLMMessage.assistant(tool_calls=[ToolCall(
                    id="skill-1", name="load_skill", args={"name": "demo-skill"},
                )]),
                LLMMessage.assistant(tool_calls=[ToolCall(
                    id="skill-report", name="plan_report", args={"steps": [
                        {"id": skill_step.id, "status": "completed"},
                        {"id": research_step.id, "status": "running"},
                    ]},
                )]),
                _work_response(),
                LLMMessage.assistant(tool_calls=[ToolCall(
                    id="research-report", name="plan_report", args={"steps": [
                        {"id": skill_step.id, "status": "completed"},
                        {"id": research_step.id, "status": "completed"},
                    ]},
                )]),
                LLMMessage.assistant(tool_calls=[ToolCall(
                    id="final", name="deliver_result",
                    args={"message": "Skill used and research finished", "attachments": []},
                )]),
            ])

    assert any(
        message.role == Role.TOOL and message.name == "load_skill"
        and "Follow the verified steps" in message.content
        for call in llm.calls for message in call
    )
    assert any(isinstance(event, ToolEvent) and event.function_name == "load_skill" for event in events)
    assert isinstance(events[-1], DoneEvent)
    assert llm.responses == []


@pytest.mark.asyncio
async def test_single_loop_deltas_share_id_with_authoritative_final_message():
    final_text = "Verified result: " + "x" * 90

    class StreamingLLM(ScriptedLLM):
        stream_calls = 0

        async def ask_stream(self, messages, tools=None, response_format=None,
                             tool_choice=None, output_tool=None):
            self.stream_calls += 1
            assert output_tool == "deliver_result"
            yield LLMStreamChunk(text=final_text[:70])
            yield LLMStreamChunk(text=final_text)
            yield LLMStreamChunk(message=LLMMessage.assistant(tool_calls=[ToolCall(
                id="final", name="deliver_result",
                args={"message": final_text, "attachments": []},
            )]))

    llm = StreamingLLM([
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="plan-1", name="create_plan", args={
                "message": "I'll read the file.", "language": "en",
                "title": "Read file", "goal": "Verify the result",
                "steps": [{"id": "1", "description": "Read the file"}],
            },
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="notify-1", name="message_notify_user", args={"text": "Reading."},
        )]),
        _work_response(),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="report-1", name="plan_report",
            args={"steps": [{"id": "1", "status": "completed"}]},
        )]),
    ])
    flow = _flow(llm)

    events = [event async for event in flow.run(Message(message="Verify"))]

    deltas = [event for event in events if isinstance(event, MessageDeltaEvent)]
    final = next(event for event in events if isinstance(event, MessageEvent) and event.message == final_text)
    assert llm.stream_calls == 1
    assert deltas
    assert all(event.message_id == final.message_id for event in deltas)
    assert final.message_id
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_single_loop_rebuild_after_wait_reports_prior_work_without_repeating_it():
    repository = FakeAgentRepository()
    first_llm = ScriptedLLM([
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="plan-1", name="create_plan", args={
                "message": "I'll inspect the file, then ask.", "language": "en",
                "title": "Inspect and ask", "goal": "Use the selected option",
                "steps": [{"id": "1", "description": "Read and decide"}],
            },
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="notify-1", name="message_notify_user", args={"text": "Reading."},
        )]),
        _work_response(),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="ask-1", name="message_ask_user", args={"text": "Which option?"},
        )]),
    ])
    first_flow = _flow(first_llm, agent_repository=repository)

    first_events = [event async for event in first_flow.run(Message(message="Choose"))]
    await asyncio.sleep(0)
    assert any(isinstance(event, WaitEvent) for event in first_events)
    assert not any(isinstance(event, DoneEvent) for event in first_events)
    assert first_flow.agent._output_tool is None
    assert not first_flow.agent._stream_queue._getters

    resumed = FakeSession(
        status=SessionStatus.RUNNING,
        plan=first_flow.plan.model_copy(deep=True),
    )
    second_llm = ScriptedLLM([
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="report-1", name="plan_report",
            args={"steps": [{"id": "1", "status": "completed"}]},
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="final", name="deliver_result",
            args={"message": "Used option B after checking the file", "attachments": []},
        )]),
    ])
    second_flow = _flow(second_llm, session=resumed, agent_repository=repository)
    second_events = [event async for event in second_flow.run(
        Message(message="Use option B"), resumes_waiting=True,
    )]

    assert isinstance(second_events[-1], DoneEvent)
    assert second_flow.plan.steps[0].status == ExecutionStatus.COMPLETED
    assert all(
        all(call.name != "file_read" for call in response.tool_calls)
        for response in second_llm.responses
    )
    assert not any(
        isinstance(event, ToolEvent) and event.function_name == "file_read"
        for event in second_events
    )
    assert second_llm.responses == []


@pytest.mark.asyncio
async def test_replan_to_empty_steps_without_work_cannot_complete_the_task():
    llm = ScriptedLLM([
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="plan-1", name="create_plan", args={
                "message": "I'll verify the task.", "language": "en",
                "title": "Verify", "goal": "Verify the file",
                "steps": [{"id": "1", "description": "Read the file"}],
            },
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="notify-1", name="message_notify_user", args={"text": "Checking."},
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="replan-1", name="replan", args={"reason": "Maybe no work is needed"},
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="update-1", name="update_plan", args={"steps": []},
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="early", name="deliver_result",
            args={"message": "Unverified", "attachments": []},
        )]),
        _work_response(),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="report-1", name="plan_report",
            args={"steps": [{"id": "1", "status": "completed"}]},
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="final", name="deliver_result",
            args={"message": "Verified", "attachments": []},
        )]),
    ])
    flow = _flow(llm)

    events = [event async for event in flow.run(Message(message="Verify"))]

    assert not any(isinstance(event, MessageEvent) and event.message == "Unverified" for event in events)
    assert any(isinstance(event, MessageEvent) and event.message == "Verified" for event in events)
    assert flow.plan.steps[0].id == "1"
    assert flow.plan.steps[0].status == ExecutionStatus.COMPLETED
    assert isinstance(events[-1], DoneEvent)
    assert llm.responses == []


@pytest.mark.asyncio
async def test_deliver_result_and_replan_same_batch_cannot_finish_before_new_work():
    llm = ScriptedLLM([
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="plan-1", name="create_plan", args={
                "message": "I'll verify the task.", "language": "en",
                "title": "Verify", "goal": "Verify all work",
                "steps": [{"id": "1", "description": "Read first file"}],
            },
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="notify-1", name="message_notify_user", args={"text": "Checking."},
        )]),
        _work_response(),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="report-1", name="plan_report",
            args={"steps": [{"id": "1", "status": "completed"}]},
        )]),
        LLMMessage.assistant(tool_calls=[
            ToolCall(id="early", name="deliver_result",
                     args={"message": "Premature", "attachments": []}),
            ToolCall(id="replan-1", name="replan",
                     args={"reason": "A second file also needs checking"}),
        ]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="update-1", name="update_plan",
            args={"steps": [{"id": "2", "description": "Read second file"}]},
        )]),
        _work_response("work-2"),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="report-2", name="plan_report",
            args={"steps": [
                {"id": "1", "status": "completed"},
                {"id": "2", "status": "completed"},
            ]},
        )]),
        LLMMessage.assistant(tool_calls=[ToolCall(
            id="final", name="deliver_result",
            args={"message": "Both files verified", "attachments": []},
        )]),
    ])
    flow = _flow(llm)

    events = [event async for event in flow.run(Message(message="Verify"))]

    assert not any(isinstance(event, MessageEvent) and event.message == "Premature" for event in events)
    assert any(isinstance(event, MessageEvent) and event.message == "Both files verified" for event in events)
    assert any(
        message.role == Role.TOOL and message.name == "deliver_result"
        and "alone" in message.content
        for call in llm.calls for message in call
    )
    assert [step.id for step in flow.plan.steps] == ["1", "2"]
    assert isinstance(events[-1], DoneEvent)
    assert llm.responses == []


def test_restore_compacted_failed_work_does_not_authorize_plan_report():
    agent = _flow(ScriptedLLM([])).agent
    memory = Memory(messages=[
        LLMMessage.user("<authoritative_plan>current plan</authoritative_plan>"),
        LLMMessage.tool("failed", "file_read", '{"success":false,"message":"permission denied"}',
                        artifact=ToolResult(success=False, message="permission denied")),
    ])
    memory.compact(keep_recent=0)
    agent.memory = memory

    agent.restore_work_since_report()

    assert memory.messages[-1].tool_success is False
    assert not agent._work_since_report


def test_restore_compacted_successful_work_after_current_plan_allows_report():
    agent = _flow(ScriptedLLM([])).agent
    memory = Memory(messages=[
        LLMMessage.tool("old", "file_read", '{"success":true}',
                        artifact=ToolResult(success=True)),
        LLMMessage.user("<authoritative_plan>current plan</authoritative_plan>"),
        LLMMessage.tool("current", "file_read", '{"success":true,"data":"verified"}',
                        artifact=ToolResult(success=True, data="verified")),
    ])
    memory.compact(keep_recent=0)
    agent.memory = memory

    agent.restore_work_since_report()

    assert memory.messages[-1].tool_success is True
    assert agent._work_since_report


def test_restore_old_compacted_work_before_current_plan_cannot_authorize_report():
    agent = _flow(ScriptedLLM([])).agent
    agent.memory = Memory(messages=[
        LLMMessage.tool("old", "file_read", '{"success":true}',
                        artifact=ToolResult(success=True)),
        LLMMessage.user("<authoritative_plan>current plan</authoritative_plan>"),
    ])

    agent.restore_work_since_report()

    assert not agent._work_since_report


def test_restore_report_with_completion_hint_resets_work_state():
    agent = _flow(ScriptedLLM([])).agent
    agent.memory = Memory(messages=[
        LLMMessage.user("<authoritative_plan>current plan</authoritative_plan>"),
        LLMMessage.tool("work", "file_read", '{"success":true,"data":"verified"}'),
        LLMMessage.tool("report", "plan_report",
                        '{"success":true,"data":{"steps":[]}}\n\nPlan complete; use deliver_result.'),
    ])

    agent.restore_work_since_report()

    assert not agent._work_since_report
