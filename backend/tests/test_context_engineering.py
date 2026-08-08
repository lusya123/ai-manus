"""Unit tests for native structured output and bounded context engineering."""

import json
from types import MethodType
from typing import List, Optional

import pytest
from mcp.types import CallToolResult, TextContent

from app.domain.models.agent_output import (
    FinalResult,
    PlanOutput,
    PlanStepDraft,
    PlanUpdateOutput,
    StepReport,
)
from app.domain.models.event import (
    ErrorEvent,
    MessageEvent,
    StepEvent,
    StepStatus,
    ToolEvent,
    WaitEvent,
)
from app.domain.models.mcp_config import MCPConfig, MCPServerConfig, MCPTransport
from app.domain.models.memory import Memory, estimate_tokens
from app.domain.models.message import LLMMessage, Message, Role, ToolCall
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent, StructuredOutputEvent
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.agents.planner import PlannerAgent
from app.domain.services.prompts.execution import EXECUTION_PROMPT, SUMMARIZE_PROMPT
from app.domain.services.prompts.system import build_system_prompt
from app.domain.services.tools.base import (
    BaseToolkit,
    OutputTool,
    Tool,
    describe_toolkits,
    tool,
)
from app.domain.services.tools.message import MessageToolkit
from app.domain.services.tools.mcp import MCPClientManager
from app.domain.services.tools.preview import PreviewToolkit


class EchoToolkit(BaseToolkit):
    name = "echo"
    instructions = "- Echo things back verbatim"

    @tool(parse_docstring=True)
    async def echo(self, text: str) -> ToolResult:
        """Echo the given text back.

        Args:
            text: Text to echo
        """
        return ToolResult(success=True, data=text)


class SilentToolkit(BaseToolkit):
    name = "silent"

    @tool(parse_docstring=True)
    async def noop(self) -> ToolResult:
        """Do nothing."""
        return ToolResult(success=True)


class RecordingToolkit(BaseToolkit):
    name = "recording"

    def __init__(self):
        self.calls: List[str] = []
        super().__init__()

    @tool(parse_docstring=True)
    async def side_effect(self, value: str) -> ToolResult:
        """Record one externally visible operation.

        Args:
            value: Value to record
        """
        self.calls.append(value)
        return ToolResult(success=True, data=value)


class TestSystemPromptBuilder:
    def test_core_prompt_always_present(self):
        prompt = build_system_prompt()
        assert "You are Manus" in prompt
        assert "<sandbox_environment>" in prompt

    def test_bound_toolkit_contributes_section(self):
        prompt = build_system_prompt(toolkits=[EchoToolkit()])
        assert "<echo_rules>" in prompt
        assert "Echo things back verbatim" in prompt

    def test_toolkit_without_instructions_adds_no_section(self):
        prompt = build_system_prompt(toolkits=[SilentToolkit()])
        assert "<silent_rules>" not in prompt

    def test_runtime_and_role_prompts_are_appended(self):
        prompt = build_system_prompt(
            runtime_prompt="<runtime_environment>safe</runtime_environment>",
            role_prompt="<role>planner</role>",
        )
        assert "<runtime_environment>safe</runtime_environment>" in prompt
        assert prompt.endswith("<role>planner</role>")

    def test_project_instruction_section(self):
        prompt = build_system_prompt(project_instruction="Always reply in Chinese.")
        assert "<project_instructions>" in prompt
        assert "Always reply in Chinese." in prompt

    def test_blank_project_instruction_omitted(self):
        prompt = build_system_prompt(project_instruction="   ")
        assert "<project_instructions>" not in prompt

    def test_describe_toolkits_compact_overview(self):
        overview = describe_toolkits([EchoToolkit(), SilentToolkit()])
        assert overview == "- echo: echo\n- silent: noop"

    def test_interactive_web_deliverables_require_preview_contract(self):
        toolkit = PreviewToolkit()
        schema_description = toolkit.get_tool_schemas()[0]["function"]["description"]
        for contract in (toolkit.instructions, schema_description):
            assert "preview_show" in contract
            assert "complete_step" in contract
            assert "deliver_result" in contract
            assert "browser/VNC" in contract
            assert "open a file manually" in contract
            assert "third-party pages" in contract

        assert "ordinary research" in toolkit.instructions
        assert "ordinary browsing" in schema_description

        assert "preview_show" in EXECUTION_PROMPT
        assert "complete_step" in EXECUTION_PROMPT
        assert "browser/VNC" in EXECUTION_PROMPT
        assert "open a file manually" in EXECUTION_PROMPT
        assert "ordinary research" in EXECUTION_PROMPT

        assert "preview_show" in SUMMARIZE_PROMPT
        assert "deliver_result" in SUMMARIZE_PROMPT
        assert "Browser/VNC" in SUMMARIZE_PROMPT
        assert "open files manually" in SUMMARIZE_PROMPT
        assert "ordinary research" in SUMMARIZE_PROMPT


class TestOutputTool:
    def test_schema_shape(self):
        output = OutputTool("create_plan", "Submit the plan.", PlanOutput)
        schema = output.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "create_plan"
        assert set(schema["function"]["parameters"]["properties"]) == {
            "message",
            "language",
            "title",
            "goal",
            "steps",
        }

    def test_validate_success_and_failure(self):
        import pytest
        from pydantic import ValidationError

        output = OutputTool("complete_step", "Report step.", StepReport)
        report = output.validate({"success": True, "result": "done"})
        assert report.success is True and report.attachments == []
        with pytest.raises(ValidationError):
            output.validate({"result": "missing success"})


class TestMemoryCompaction:
    @staticmethod
    def _memory_with_tool_results(n: int, size: int = 400) -> Memory:
        memory = Memory()
        memory.add_message(LLMMessage.system("sys"))
        for index in range(n):
            memory.add_message(
                LLMMessage.assistant(
                    "",
                    tool_calls=[ToolCall(id=f"c{index}", name="echo", args={})],
                )
            )
            memory.add_message(
                LLMMessage.tool(
                    tool_call_id=f"c{index}",
                    name="echo",
                    content="x" * size,
                )
            )
        return memory

    def test_estimate_tokens_counts_content_and_calls(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens("abcd" * 100) == 100
        assert self._memory_with_tool_results(2).estimate_tokens() > 0

    def test_unconditional_compact_elides_old_tool_results(self):
        memory = self._memory_with_tool_results(10)
        memory.compact(keep_recent=4)
        assert any("elided" in msg.content for msg in memory.messages)
        assert memory.messages[-1].content == "x" * 400

    def test_budgeted_compact_stops_at_budget(self):
        memory = self._memory_with_tool_results(10)
        before = memory.estimate_tokens()
        memory.compact(max_tokens=before + 1)
        assert all(
            "elided" not in msg.content
            for msg in memory.messages
            if msg.role == Role.TOOL
        )
        memory.compact(max_tokens=before // 2, keep_recent=2)
        assert memory.estimate_tokens() <= before // 2

    def test_compact_preserves_message_skeleton(self):
        memory = self._memory_with_tool_results(5)
        count = len(memory.messages)
        memory.compact(keep_recent=0)
        assert len(memory.messages) == count
        assert all(
            msg.role in (Role.SYSTEM, Role.ASSISTANT, Role.TOOL)
            for msg in memory.messages
        )


class _FakeRepository:
    def __init__(self):
        self.memory = Memory()

    async def get_memory(self, agent_id: str, name: str) -> Memory:
        return self.memory

    async def save_memory(
        self, agent_id: str, name: str, memory: Memory
    ) -> None:
        self.memory = memory


class _ScriptedLLM:
    def __init__(self, responses: List[LLMMessage]):
        self._responses = list(responses)
        self.requests = []

    async def ask(
        self,
        messages,
        tools=None,
        response_format=None,
        tool_choice=None,
    ):
        self.requests.append(
            {
                "messages": list(messages),
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
        return self._responses.pop(0)

    async def parse_json(self, text: str):
        raise AssertionError("parse_json must not be used by the native agent loop")


class _TestAgent(BaseAgent):
    name = "test"

    def build_system_prompt(self) -> str:
        return "test system prompt"


def _agent(
    llm: _ScriptedLLM, toolkits: Optional[list] = None
) -> _TestAgent:
    return _TestAgent(
        agent_id="a1",
        agent_repository=_FakeRepository(),
        llm=llm,
        tools=toolkits or [],
    )


REPORT_TOOL = OutputTool("complete_step", "Report the step outcome.", StepReport)


async def _collect(generator):
    return [event async for event in generator]


class TestAgentLoopStructuredOutput:
    async def test_output_tool_call_yields_structured_output(self):
        llm = _ScriptedLLM(
            [
                LLMMessage.assistant(
                    "",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="complete_step",
                            args={
                                "success": True,
                                "result": "done",
                                "attachments": [],
                            },
                        )
                    ],
                )
            ]
        )
        agent = _agent(llm)
        events = await _collect(agent.execute("do it", output_tool=REPORT_TOOL))
        outputs = [
            event for event in events if isinstance(event, StructuredOutputEvent)
        ]
        assert len(outputs) == 1 and outputs[0].output.result == "done"
        offered = [tool["function"]["name"] for tool in llm.requests[0]["tools"]]
        assert "complete_step" in offered
        last = agent.memory.get_last_message()
        assert last.role == Role.TOOL and last.tool_call_id == "c1"

    async def test_invalid_output_args_trigger_self_repair(self):
        llm = _ScriptedLLM(
            [
                LLMMessage.assistant(
                    "",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="complete_step",
                            args={"success": True},
                        )
                    ],
                ),
                LLMMessage.assistant(
                    "",
                    tool_calls=[
                        ToolCall(
                            id="c2",
                            name="complete_step",
                            args={"success": True, "result": "fixed"},
                        )
                    ],
                ),
            ]
        )
        agent = _agent(llm)
        events = await _collect(agent.execute("do it", output_tool=REPORT_TOOL))
        outputs = [
            event for event in events if isinstance(event, StructuredOutputEvent)
        ]
        assert len(outputs) == 1 and outputs[0].output.result == "fixed"
        feedback = [
            msg
            for msg in agent.memory.get_messages()
            if msg.role == Role.TOOL and "Invalid arguments" in msg.content
        ]
        assert len(feedback) == 1

    async def test_plain_message_is_nudged_to_output_tool(self):
        llm = _ScriptedLLM(
            [
                LLMMessage.assistant("I think I'm done."),
                LLMMessage.assistant(
                    "",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="complete_step",
                            args={"success": True, "result": "ok"},
                        )
                    ],
                ),
            ]
        )
        agent = _agent(llm)
        events = await _collect(agent.execute("do it", output_tool=REPORT_TOOL))
        assert any(isinstance(event, StructuredOutputEvent) for event in events)
        assert "complete_step" in llm.requests[1]["messages"][-1].content

    async def test_regular_tools_still_execute(self):
        llm = _ScriptedLLM(
            [
                LLMMessage.assistant(
                    "",
                    tool_calls=[
                        ToolCall(id="c1", name="echo", args={"text": "hello"})
                    ],
                ),
                LLMMessage.assistant(
                    "",
                    tool_calls=[
                        ToolCall(
                            id="c2",
                            name="complete_step",
                            args={"success": True, "result": "echoed"},
                        )
                    ],
                ),
            ]
        )
        agent = _agent(llm, toolkits=[EchoToolkit()])
        events = await _collect(
            agent.execute("echo hello", output_tool=REPORT_TOOL)
        )
        assert any(isinstance(event, StructuredOutputEvent) for event in events)
        tool_messages = [
            msg
            for msg in agent.memory.get_messages()
            if msg.role == Role.TOOL and msg.name == "echo"
        ]
        assert len(tool_messages) == 1
        assert json.loads(tool_messages[0].content)["data"] == "hello"

    async def test_work_and_output_in_one_batch_requires_fresh_output_call(self):
        toolkit = RecordingToolkit()
        llm = _ScriptedLLM(
            [
                LLMMessage.assistant(
                    "",
                    tool_calls=[
                        ToolCall(
                            id="premature-output",
                            name="complete_step",
                            args={"success": True, "result": "premature"},
                        ),
                        ToolCall(
                            id="work-once",
                            name="side_effect",
                            args={"value": "once"},
                        ),
                    ],
                ),
                LLMMessage.assistant(
                    "",
                    tool_calls=[
                        ToolCall(
                            id="final-output",
                            name="complete_step",
                            args={"success": True, "result": "after observation"},
                        )
                    ],
                ),
            ]
        )
        agent = _agent(llm, toolkits=[toolkit])

        events = await _collect(
            agent.execute("do it", output_tool=REPORT_TOOL)
        )

        outputs = [
            event for event in events if isinstance(event, StructuredOutputEvent)
        ]
        assert [output.output.result for output in outputs] == ["after observation"]
        assert toolkit.calls == ["once"]
        tool_messages = {
            item.tool_call_id: item
            for item in agent.memory.get_messages()
            if item.role == Role.TOOL
        }
        assert "must be called alone" in tool_messages["premature-output"].content
        assert json.loads(tool_messages["work-once"].content)["data"] == "once"
        assert json.loads(tool_messages["final-output"].content)["success"] is True

    async def test_mixed_ask_user_defers_work_until_answer(self):
        toolkit = RecordingToolkit()
        repository = _FakeRepository()
        llm = _ScriptedLLM(
            [
                LLMMessage.assistant(
                    "",
                    tool_calls=[
                        ToolCall(
                            id="work-once",
                            name="side_effect",
                            args={"value": "once"},
                        ),
                        ToolCall(
                            id="mixed-question",
                            name="message_ask_user",
                            args={"text": "This mixed question must not be shown"},
                        ),
                    ],
                ),
                LLMMessage.assistant(
                    "",
                    tool_calls=[
                        ToolCall(
                            id="actual-question",
                            name="message_ask_user",
                            args={"text": "Approve the next action?"},
                        )
                    ],
                ),
                LLMMessage.assistant(
                    "",
                    tool_calls=[
                        ToolCall(
                            id="work-after-approval",
                            name="side_effect",
                            args={"value": "once"},
                        )
                    ],
                ),
                LLMMessage.assistant(
                    "",
                    tool_calls=[
                        ToolCall(
                            id="completed-after-approval",
                            name="complete_step",
                            args={
                                "success": True,
                                "result": "approved work completed",
                            },
                        )
                    ],
                ),
            ]
        )
        agent = ExecutionAgent(
            agent_id="a1",
            agent_repository=repository,
            llm=llm,
            tools=[toolkit, MessageToolkit()],
        )
        plan = Plan(language="en")
        step = Step(id="1", description="Ask before one side effect")

        events = await _collect(
            agent.execute_step(
                plan,
                step,
                Message(message="continue carefully"),
            )
        )

        # A request for user input is a control barrier. The sibling work call
        # is paired in memory, but must not execute before the user authorizes it.
        assert toolkit.calls == []
        assert any(isinstance(event, WaitEvent) for event in events)
        assert not any(
            isinstance(event, MessageEvent)
            and event.message == "This mixed question must not be shown"
            for event in events
        )
        assert any(
            isinstance(event, MessageEvent)
            and event.message == "Approve the next action?"
            for event in events
        )
        assert {
            event.tool_call_id
            for event in events
            if isinstance(event, ToolEvent)
        } == set()

        await agent.roll_back(Message(message="approved"))

        tool_messages = {
            item.tool_call_id: item
            for item in repository.memory.get_messages()
            if item.role == Role.TOOL
        }
        assert "must be called alone" in tool_messages["mixed-question"].content
        assert "was not executed" in tool_messages["work-once"].content
        assert tool_messages["actual-question"].content == "approved"

        resumed_events = await _collect(
            agent.execute_step(
                plan,
                step,
                Message(message="continue carefully"),
            )
        )

        assert any(
            isinstance(event, StepEvent) and event.status == StepStatus.COMPLETED
            for event in resumed_events
        )
        assert toolkit.calls == ["once"]
        assert {
            event.tool_call_id
            for event in resumed_events
            if isinstance(event, ToolEvent)
        } == {"work-after-approval"}

    async def test_legacy_roll_back_answers_the_actual_ask_user_call_id(self):
        repository = _FakeRepository()
        repository.memory.add_message(LLMMessage.system("system"))
        repository.memory.add_message(
            LLMMessage.assistant(
                "",
                tool_calls=[
                    ToolCall(
                        id="possibly-executed-work",
                        name="side_effect",
                        args={"value": "unknown"},
                    ),
                    ToolCall(
                        id="question-call",
                        name="message_ask_user",
                        args={"text": "Continue?"},
                    ),
                ],
            )
        )
        agent = _TestAgent(
            agent_id="a1",
            agent_repository=repository,
            llm=_ScriptedLLM([]),
            tools=[],
        )

        await agent.roll_back(Message(message="yes"))

        responses = [
            item for item in repository.memory.messages if item.role == Role.TOOL
        ]
        assert [item.tool_call_id for item in responses] == [
            "possibly-executed-work",
            "question-call",
        ]
        assert "Do not repeat" in responses[0].content
        assert responses[1].content == "yes"

    async def test_unknown_tool_gets_error_response(self):
        llm = _ScriptedLLM(
            [
                LLMMessage.assistant(
                    "",
                    tool_calls=[ToolCall(id="c1", name="not_a_tool", args={})],
                ),
                LLMMessage.assistant("all done"),
            ]
        )
        agent = _agent(llm)
        events = await _collect(agent.execute("do it"))
        assert not any(isinstance(event, ErrorEvent) for event in events)
        unknown = [
            msg
            for msg in agent.memory.get_messages()
            if msg.role == Role.TOOL and "Unknown tool" in msg.content
        ]
        assert len(unknown) == 1


class TestToolResultTruncation:
    async def test_oversized_tool_result_truncated(self):
        class BigToolkit(BaseToolkit):
            name = "big"

            @tool(parse_docstring=True)
            async def big(self) -> ToolResult:
                """Return something huge."""
                return ToolResult(success=True, data="y" * 100000)

        llm = _ScriptedLLM(
            [
                LLMMessage.assistant(
                    "", tool_calls=[ToolCall(id="c1", name="big", args={})]
                ),
                LLMMessage.assistant("done"),
            ]
        )
        agent = _agent(llm, toolkits=[BigToolkit()])
        await _collect(agent.execute("go"))
        tool_message = [
            msg for msg in agent.memory.get_messages() if msg.role == Role.TOOL
        ][0]
        assert len(tool_message.content) <= agent.max_tool_result_chars
        envelope = json.loads(tool_message.content)
        assert envelope["truncated"] is True
        assert envelope["omitted_chars"] > 0


class TestExecutionStepLifecycle:
    async def test_unsuccessful_report_is_completed_work_not_agent_loop_failure(self):
        agent = object.__new__(ExecutionAgent)

        async def execute(self, _request, output_tool=None):
            yield StructuredOutputEvent(
                output=StepReport(
                    success=False,
                    result="The provider rejected the operation",
                )
            )

        agent.execute = MethodType(execute, agent)
        step = Step(id="1", description="Try provider operation")
        events = [
            event
            async for event in agent.execute_step(
                Plan(language="en"), step, Message(message="try it")
            )
        ]

        assert [
            event.status for event in events if isinstance(event, StepEvent)
        ] == [StepStatus.STARTED, StepStatus.COMPLETED]
        assert step.status == ExecutionStatus.COMPLETED
        assert step.success is False
        assert not any(isinstance(event, MessageEvent) for event in events)

    async def test_terminal_agent_error_never_turns_failed_step_completed(self):
        agent = object.__new__(ExecutionAgent)

        async def execute(self, _request, output_tool=None):
            yield ErrorEvent(error="Model did not submit the result")

        agent.execute = MethodType(execute, agent)
        step = Step(id="1", description="Do work")
        events = [
            event
            async for event in agent.execute_step(
                Plan(language="en"), step, Message(message="do it")
            )
        ]

        assert step.status == ExecutionStatus.FAILED
        assert not any(isinstance(event, ErrorEvent) for event in events)
        assert not any(
            isinstance(event, StepEvent) and event.status == StepStatus.COMPLETED
            for event in events
        )


class TestFinalSummaryContext:
    async def test_zero_step_summary_receives_original_request(self):
        agent = object.__new__(ExecutionAgent)
        captured = {}

        async def execute(self, request, output_tool=None):
            captured["request"] = request
            captured["output_tool"] = output_tool
            yield StructuredOutputEvent(
                output=FinalResult(message="四。", attachments=[])
            )

        agent.execute = MethodType(execute, agent)
        events = [
            event
            async for event in agent.summarize(
                Plan(language="zh", steps=[]),
                Message(message="二加二等于多少？"),
            )
        ]

        assert captured["output_tool"].name == "deliver_result"
        assert "Original user message: 二加二等于多少？" in captured["request"]
        assert "never return only an acknowledgement" in captured["request"]
        assert [
            event.message for event in events if isinstance(event, MessageEvent)
        ] == ["四。"]


class TestPlannerUpdateLifecycle:
    @pytest.mark.parametrize(
        "terminal_status",
        [ExecutionStatus.COMPLETED, ExecutionStatus.FAILED],
    )
    async def test_recovery_steps_survive_after_last_terminal_step(
        self, terminal_status
    ):
        agent = object.__new__(PlannerAgent)

        async def execute(self, _request, output_tool=None):
            assert output_tool.name == "update_plan"
            yield StructuredOutputEvent(
                output=PlanUpdateOutput(
                    steps=[
                        PlanStepDraft(
                            id="2",
                            description="Try a safe recovery and verify it",
                        )
                    ]
                )
            )

        agent.execute = MethodType(execute, agent)
        finished = Step(
            id="1",
            description="Initial attempt",
            status=terminal_status,
        )
        plan = Plan(steps=[finished])

        events = [event async for event in agent.update_plan(plan, finished)]

        assert [step.id for step in plan.steps] == ["1", "2"]
        assert plan.steps[0] is finished
        assert plan.steps[1].status == ExecutionStatus.PENDING
        assert events[-1].plan is plan


class TestDynamicMcpTools:
    def test_mcp_schemas_become_invocable_tools(self):
        from app.domain.services.tools.mcp import MCPToolkit

        toolkit = MCPToolkit()
        toolkit.tools = toolkit._build_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "mcp_server_lookup",
                        "description": "[server] Look something up",
                        "parameters": {
                            "type": "object",
                            "properties": {"q": {"type": "string"}},
                        },
                    },
                }
            ]
        )
        found = toolkit.get_tool("mcp_server_lookup")
        assert isinstance(found, Tool)
        assert found.toolkit is toolkit
        assert toolkit.get_tool_schemas()[0]["function"]["name"] == "mcp_server_lookup"

    async def test_mcp_is_error_maps_to_failed_tool_result(self):
        class FailingSession:
            async def call_tool(self, name, arguments):
                assert name == "lookup"
                assert arguments == {"q": "missing"}
                return CallToolResult(
                    content=[
                        TextContent(type="text", text="upstream lookup failed")
                    ],
                    isError=True,
                )

        manager = MCPClientManager(
            MCPConfig(
                mcpServers={
                    "server": MCPServerConfig(
                        transport=MCPTransport.STDIO,
                        command="unused",
                    )
                }
            )
        )
        manager._clients["server"] = FailingSession()

        result = await manager.call_tool(
            "mcp_server_lookup", {"q": "missing"}
        )

        assert result.success is False
        assert result.message == "upstream lookup failed"
        assert result.data is None
