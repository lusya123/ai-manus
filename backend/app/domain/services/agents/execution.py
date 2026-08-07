import logging
from typing import AsyncGenerator, List

from app.domain.external.llm import LLM
from app.domain.models.agent_output import FinalResult, StepReport
from app.domain.models.event import (
    BaseEvent,
    ErrorEvent,
    MessageEvent,
    StepEvent,
    StepStatus,
    ToolEvent,
    ToolStatus,
    WaitEvent,
)
from app.domain.models.file import FileInfo
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.services.agents.base import BaseAgent, StructuredOutputEvent
from app.domain.services.prompts.execution import (
    EXECUTION_PROMPT,
    EXECUTION_ROLE_PROMPT,
    SUMMARIZE_PROMPT,
)
from app.domain.services.prompts.system import build_system_prompt
from app.domain.services.tools.base import BaseToolkit, OutputTool


logger = logging.getLogger(__name__)

COMPLETE_STEP_TOOL = OutputTool(
    name="complete_step",
    description=(
        "Report the outcome of the current plan step when it is finished or "
        "cannot proceed."
    ),
    schema=StepReport,
)

DELIVER_RESULT_TOOL = OutputTool(
    name="deliver_result",
    description="Deliver the final task result and its verified files to the user.",
    schema=FinalResult,
)


class ExecutionAgent(BaseAgent):
    """Execute plan steps and submit outcomes through native output tools."""

    name: str = "execution"

    def __init__(
        self,
        agent_id: str,
        agent_repository: AgentRepository,
        llm: LLM,
        tools: List[BaseToolkit],
        runtime_prompt: str = "",
    ):
        super().__init__(
            agent_id=agent_id,
            agent_repository=agent_repository,
            llm=llm,
            tools=tools,
        )
        self._runtime_prompt = runtime_prompt

    def build_system_prompt(self) -> str:
        return build_system_prompt(
            toolkits=self.toolkits,
            runtime_prompt=self._runtime_prompt,
            role_prompt=EXECUTION_ROLE_PROMPT,
            project_instruction=self._project_instruction,
        )

    @staticmethod
    def _apply_step_report(step: Step, report: StepReport) -> None:
        step.status = ExecutionStatus.COMPLETED
        step.success = report.success
        step.result = report.result
        step.attachments = report.attachments

    async def execute_step(
        self, plan: Plan, step: Step, message: Message
    ) -> AsyncGenerator[BaseEvent, None]:
        request = EXECUTION_PROMPT.format(
            step=step.description,
            message=message.message,
            attachments="\n".join(message.attachments),
            language=plan.language,
        )
        step.status = ExecutionStatus.RUNNING
        yield StepEvent(status=StepStatus.STARTED, step=step)

        try:
            event_stream = self.execute(request, output_tool=COMPLETE_STEP_TOOL)
        except TypeError as exc:
            # Compatibility for a legacy subclass/test double that still
            # implements execute(request) without the output_tool keyword.
            if "output_tool" not in str(exc):
                raise
            event_stream = self.execute(request)

        async for event in event_stream:
            if isinstance(event, ErrorEvent):
                step.status = ExecutionStatus.FAILED
                step.error = event.error
                yield StepEvent(status=StepStatus.FAILED, step=step)
                # A step failure is recoverable plan state, not a terminal
                # failure for the whole chat turn.  ErrorEvent is a transport
                # terminal everywhere else (SSE, durable outbox, and the
                # frontend), so leaking it here would stop the client while
                # PlanActFlow continues into planner recovery in the worker.
                return
            elif isinstance(event, StructuredOutputEvent):
                self._apply_step_report(step, event.output)
                # Step outcomes belong to the plan/step stream. Do not leak
                # intermediate model output as a chat message; the final
                # deliver_result call is the sole user-facing result.
                yield StepEvent(status=StepStatus.COMPLETED, step=step)
                continue
            elif isinstance(event, MessageEvent):
                # Rolling-upgrade compatibility for already running workers or
                # legacy test doubles. New model calls use complete_step.
                try:
                    parsed = await self._parse_json(event.message)
                    report = StepReport.model_validate(parsed)
                except Exception:
                    continue
                self._apply_step_report(step, report)
                yield StepEvent(status=StepStatus.COMPLETED, step=step)
                continue
            elif isinstance(event, ToolEvent):
                if event.function_name == "message_notify_user":
                    # Notifications are intentionally internal progress, not
                    # duplicate tool cards in the public event stream.
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

    async def summarize(
        self, plan: Plan, message: Message
    ) -> AsyncGenerator[BaseEvent, None]:
        request = SUMMARIZE_PROMPT.format(
            message=message.message,
            attachments="\n".join(message.attachments),
            language=plan.language,
            plan=plan.dump_json(),
        )
        async for event in self.execute(
            request, output_tool=DELIVER_RESULT_TOOL
        ):
            if isinstance(event, StructuredOutputEvent):
                result: FinalResult = event.output
                logger.debug(
                    "Execution summary completed: message_chars=%d attachments=%d",
                    len(result.message),
                    len(result.attachments),
                )
                attachments = [
                    FileInfo(file_path=file_path) for file_path in result.attachments
                ]
                yield MessageEvent(message=result.message, attachments=attachments)
                continue
            if isinstance(event, MessageEvent):
                # Rolling-upgrade compatibility; native runs do not take this
                # branch because deliver_result is required.
                try:
                    parsed = await self._parse_json(event.message)
                    result = FinalResult.model_validate(parsed)
                except Exception:
                    continue
                attachments = [
                    FileInfo(file_path=file_path) for file_path in result.attachments
                ]
                yield MessageEvent(message=result.message, attachments=attachments)
                continue
            yield event
