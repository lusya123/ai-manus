import json
from contextlib import aclosing
from typing import AsyncGenerator, AsyncIterable, List

from app.domain.external.llm import LLM
from app.domain.models.agent_output import FinalResult, PlanReportOutput
from app.domain.models.plan import Plan, ExecutionStatus
from app.domain.models.event import (
    BaseEvent,
    ErrorEvent,
    MessageEvent,
    TitleEvent,
    ToolEvent,
    ToolStatus,
    WaitEvent,
)
from app.domain.models.file import FileInfo
from app.domain.models.message import LLMMessage, Message, Role, ToolCall
from app.domain.models.todo import TodoItem
from app.domain.models.tool_result import ToolResult
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.services.agents.base import BaseAgent, StructuredOutputEvent
from app.domain.services.prompts.manus import MANUS_ROLE_PROMPT
from app.domain.services.prompts.system import build_system_prompt
from app.domain.services.tools.base import BaseToolkit, OutputTool


DELIVER_RESULT_TOOL = OutputTool(
    name="deliver_result",
    description="Deliver the final task result and its files to the user.",
    schema=FinalResult,
)

# Chat / wait surface — may run before other work.
# plan_report / replan stay outside this set so notify-before-work applies;
# AgentLoopFlow intercepts their ToolEvents after Manus yields them.
_CONTROL_TOOLS = frozenset({
    "message_notify_user",
    "message_ask_user",
})

# Allowed after every plan step is done (until deliver_result / replan adds work).
_AFTER_PLAN_DONE_TOOLS = frozenset({
    "message_notify_user",
    "message_ask_user",
    "plan_report",
    "replan",
})

_NOTIFY_REQUIRED_BEFORE_WORK = (
    "Blocked: call message_notify_user first with a brief acknowledgment "
    "in the user's language, then retry this work tool."
)

_PLAN_FINISHED_BLOCK = (
    "Blocked: all authoritative plan steps are already completed or failed. "
    "Call deliver_result now with the final answer (and attachments). "
    "Call replan only if new remaining work is genuinely required."
)


def suggest_title(user_message: str) -> str:
    text = (user_message or "").strip().replace("\n", " ")
    return text[:80] if text else "New task"


class ManusAgent(BaseAgent):
    name: str = "manus"

    def __init__(
        self,
        agent_id: str,
        agent_repository: AgentRepository,
        llm: LLM,
        tools: List[BaseToolkit],
    ):
        super().__init__(
            agent_id=agent_id,
            agent_repository=agent_repository,
            llm=llm,
            tools=tools,
        )
        # Kept for AgentLoopFlow wait-resume / plan completion (session plan).
        self._todo_items: List[TodoItem] = []
        self._plan_title: str | None = None
        self._plan_goal: str | None = None
        self._title_emitted = False
        self._user_notified = False
        self._user_message = ""
        self._injected_plan_text: str | None = None
        self._injected_plan_complete_hint: str | None = None
        self._plan_finished = False
        self._plan: Plan | None = None
        self._active_skill_name = None
        self._delivered = False
        self._work_since_report = False
        self._skill_work_since_report = False

    def restore_work_since_report(self):
        self._work_since_report = False
        self._skill_work_since_report = False
        start = next((i for i in range(len(self.memory.messages) - 1, -1, -1) if self.memory.messages[i].role == Role.USER and "<authoritative_plan>" in self.memory.messages[i].content), 0)
        calls = {call.id: call for message in self.memory.messages[start:] for call in message.tool_calls}
        for message in self.memory.messages[start:]:
            if message.role != Role.TOOL:
                continue
            success = message.tool_success
            if success is None:
                try:
                    result, _ = json.JSONDecoder().raw_decode(message.content.lstrip())
                except (ValueError, TypeError):
                    continue
                if not isinstance(result, dict) or result.get("truncated") or "elided to save context" in str(result.get("message", "")):
                    continue
                success = result.get("success")
            if success is not True:
                continue
            if message.name == "plan_report":
                self._work_since_report = False
                self._skill_work_since_report = False
            elif message.name == "load_skill":
                self._skill_work_since_report = True
                call = calls.get(message.tool_call_id)
                if not self._active_skill_name and call:
                    self._active_skill_name = call.args.get("name")
            elif message.name and (message.name.startswith(("shell_", "file_", "browser_", "search", "mcp_"))):
                self._work_since_report = True

    def build_system_prompt(self) -> str:
        return build_system_prompt(
            toolkits=self.toolkits,
            role_prompt=MANUS_ROLE_PROMPT,
            project_instruction=self._project_instruction,
            skill_catalog=self._skill_catalog,
            skill_context=self._skill_context,
        )

    async def invoke_tool(self, tool, tool_call: ToolCall) -> LLMMessage:
        """Require a brief notify before shell/browser/file/search/mcp."""
        name = tool_call.name
        if name not in _CONTROL_TOOLS and not self._user_notified:
            result = ToolResult(
                success=False,
                message=_NOTIFY_REQUIRED_BEFORE_WORK,
            )
            return LLMMessage.tool(
                tool_call_id=tool_call.id,
                name=name,
                content=result.model_dump_json(),
                artifact=result,
            )
        if (
            self._plan_finished
            and name not in _AFTER_PLAN_DONE_TOOLS
        ):
            result = ToolResult(
                success=False,
                message=_PLAN_FINISHED_BLOCK,
            )
            return LLMMessage.tool(
                tool_call_id=tool_call.id,
                name=name,
                content=result.model_dump_json(),
                artifact=result,
            )
        if name == "plan_report" and self._plan:
            try:
                report = PlanReportOutput.model_validate(tool_call.args)
                previous = {step.id: step for step in self._plan.steps}
                reported = [step.id for step in report.steps]
                if len(reported) != len(set(reported)) or set(reported) != set(previous):
                    raise ValueError("Report must include every authoritative step id exactly once")
                newly_completed = [step for step in report.steps if step.status == ExecutionStatus.COMPLETED and previous[step.id].status != ExecutionStatus.COMPLETED]
                if sum(step.status == ExecutionStatus.RUNNING for step in report.steps) > 1:
                    raise ValueError("Only one authoritative step may run at a time")
                if len(newly_completed) > 1:
                    raise ValueError("Report verified completion one step at a time")
                from app.domain.services.skills.plan_steps import plan_already_starts_with_skill_read
                skill_only = newly_completed and self._skill_work_since_report and previous[newly_completed[0].id] is self._plan.steps[0] and bool(self._active_skill_name) and plan_already_starts_with_skill_read(self._plan, self._active_skill_name)
                if newly_completed and not self._work_since_report and not skill_only:
                    raise ValueError("Perform successful work before reporting completed steps")
                if any(previous[step.id].status in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED} and step.status != previous[step.id].status for step in report.steps):
                    raise ValueError("Use replan to change terminal steps")
            except (ValueError, TypeError):
                result = ToolResult(success=False, message="Invalid plan report: include all known ids and perform successful work before completing steps")
                return LLMMessage.tool(tool_call_id=tool_call.id, name=name, content=result.model_dump_json(), artifact=result)
        response = await super().invoke_tool(tool, tool_call)
        result = response.artifact
        if result and getattr(result, "success", False):
            if tool.toolkit.name in {"shell", "browser", "file", "search", "mcp"}:
                self._work_since_report = True
            elif name == "load_skill":
                self._skill_work_since_report = True
            elif name == "plan_report":
                self._work_since_report = False
                self._skill_work_since_report = False
        return response

    def _handle_output_call(self, tool_call):
        if self._plan is not None and not self._plan_finished:
            return LLMMessage.tool(tool_call_id=tool_call.id, name=tool_call.name, content="Complete or fail all authoritative plan steps through plan_report before delivering the result."), None
        return super()._handle_output_call(tool_call)

    async def ask_with_messages(self, messages: List[LLMMessage]) -> LLMMessage:
        targets = messages or (self.memory.messages if self.memory else [])
        if self._injected_plan_text:
            for message in reversed(targets):
                if message.role == Role.TOOL and message.name == "replan":
                    message.content += f"\n\n{self._injected_plan_text}"
                    self._injected_plan_text = None
                    break
        if self._injected_plan_complete_hint:
            for message in reversed(targets):
                if message.role == Role.TOOL and message.name == "plan_report":
                    message.content += f"\n\n{self._injected_plan_complete_hint}"
                    self._injected_plan_complete_hint = None
                    break
        return await super().ask_with_messages(messages)

    async def _fan_out(
        self,
        events: AsyncIterable[BaseEvent],
    ) -> AsyncGenerator[BaseEvent, None]:
        async with aclosing(events):
            async for event in events:
                if isinstance(event, ToolEvent):
                    # Hide gated work-tool attempts (before notify) from the timeline.
                    if (
                        event.function_name not in _CONTROL_TOOLS
                        and not self._user_notified
                    ):
                        continue

                    # Hide work tools blocked after the plan is fully finished.
                    if (
                        self._plan_finished
                        and event.function_name not in _AFTER_PLAN_DONE_TOOLS
                    ):
                        continue

                    if event.function_name == "message_notify_user":
                        if event.status == ToolStatus.CALLING:
                            text = (event.function_args or {}).get("text", "")
                            if isinstance(text, str) and text.strip():
                                self._user_notified = True
                                yield MessageEvent(message=text.strip())
                        continue

                    if event.function_name == "message_ask_user":
                        if event.status == ToolStatus.CALLING:
                            yield MessageEvent(
                                message=event.function_args.get("text", "")
                            )
                        elif event.status == ToolStatus.CALLED:
                            yield WaitEvent()
                            return
                        continue

                    yield event
                    continue

                if isinstance(event, StructuredOutputEvent):
                    result: FinalResult = event.output
                    self._delivered = True
                    if not self._title_emitted:
                        title = self._plan_title or suggest_title(self._user_message)
                        if title:
                            self._plan_title = title
                            self._title_emitted = True
                            yield TitleEvent(title=title)
                    attachments = [
                        FileInfo(file_path=file_path)
                        for file_path in result.attachments
                    ]
                    yield MessageEvent(
                        message=result.message,
                        message_id=event.message_id,
                        attachments=attachments,
                    )
                    return

                if isinstance(event, MessageEvent):
                    continue

                if isinstance(event, ErrorEvent):
                    yield event
                    continue

                yield event

    async def run(self, message: Message) -> AsyncGenerator[BaseEvent, None]:
        self._user_message = message.message
        request = message.message
        if message.attachments:
            request += "\n\nAttachments:\n" + "\n".join(message.attachments)
        events = self._fan_out(self.stream_events(self.execute(request, output_tool=DELIVER_RESULT_TOOL)))
        async with aclosing(events):
            async for event in events:
                yield event

    async def resume(self) -> AsyncGenerator[BaseEvent, None]:
        events = self._fan_out(self.stream_events(self.continue_execute(output_tool=DELIVER_RESULT_TOOL)))
        async with aclosing(events):
            async for event in events:
                yield event
