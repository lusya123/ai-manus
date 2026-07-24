import asyncio
import json
import logging
import uuid
from abc import ABC
from typing import Any, AsyncGenerator, List, Literal, Optional

from app.core.config import get_settings
from app.domain.external.llm import LLM
from app.domain.models.event import (
    BaseEvent,
    ErrorEvent,
    MessageEvent,
    ToolEvent,
    ToolStatus,
)
from app.domain.models.message import LLMMessage, Message, Role, ToolCall
from app.domain.models.tool_result import ToolResult
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.services.tools.base import BaseToolkit, OutputTool, Tool, ValidationError
from app.domain.utils.error_reporting import safe_exception_summary


logger = logging.getLogger(__name__)


class StructuredOutputEvent(BaseEvent):
    """Internal, validated output consumed by concrete agents only."""

    type: Literal["structured_output"] = "structured_output"
    output: Any


class BaseAgent(ABC):
    """Shared native-tool agent loop with bounded, durable context."""

    name: str = ""
    # Compatibility for persisted agents and small test subclasses. New
    # concrete agents override build_system_prompt instead.
    system_prompt: str = ""
    format: Optional[str] = None
    max_iterations: int = 100
    max_retries: int = 3
    retry_interval: float = 1.0
    tool_choice: Optional[str] = None
    max_tool_result_chars: int = 16000
    max_context_tokens: int = 100000
    _EMPTY_RESPONSE_RETRY_PROMPT = (
        "Your previous response was empty. Continue the task now. If a tool is "
        "needed, call exactly one available tool with complete JSON arguments. "
        "If no tool is needed, respond with the required final text."
    )
    _ASK_USER_TOOL_NAME = "message_ask_user"
    _WAITING_FOR_USER_TOOL_RESULT = json.dumps(
        {
            "success": True,
            "message": "Waiting for the user's response.",
        }
    )
    _INTERRUPTED_TOOL_RESULT = (
        "The previous execution was interrupted before this tool's result could "
        "be confirmed. Do not repeat the operation automatically; inspect the "
        "current state first."
    )

    def __init__(
        self,
        agent_id: str,
        agent_repository: AgentRepository,
        llm: LLM,
        tools: Optional[List[BaseToolkit]] = None,
    ):
        self._agent_id = agent_id
        self._repository = agent_repository
        self._llm = llm
        self.toolkits = tools or []
        self.memory = None
        self._output_tool: Optional[OutputTool] = None
        self._tool_call_timeout_seconds = get_settings().tool_call_timeout_seconds

    def build_system_prompt(self) -> str:
        """Return the current prompt; concrete agents assemble it dynamically."""
        return self.system_prompt

    async def _parse_json(self, text: str) -> dict:
        """Legacy rolling-upgrade helper; the native output loop does not use it."""
        return await self._llm.parse_json(text)

    def get_tool(self, name: str) -> Optional[Tool]:
        """Return an invocable work tool by name."""
        for toolkit in self.toolkits:
            tool = toolkit.get_tool(name)
            if tool:
                return tool
        return None

    def get_tool_schemas(self) -> List[dict]:
        """Return work schemas plus the active structured-output contract."""
        schemas = [
            schema
            for toolkit in self.toolkits
            for schema in toolkit.get_tool_schemas()
        ]
        output_tool = getattr(self, "_output_tool", None)
        if output_tool:
            schemas.append(output_tool.to_openai_schema())
        return schemas

    def _truncate_tool_result(self, content: str) -> str:
        """Bound a result while preserving a valid JSON tool-message body."""
        if len(content) <= self.max_tool_result_chars:
            return content

        # A raw prefix plus prose suffix corrupts serialized ToolResult JSON.
        # Wrap the prefix in a small explicit envelope and shrink until its
        # encoded representation fits the configured character budget.
        prefix_length = max(0, self.max_tool_result_chars - 160)
        while True:
            prefix = content[:prefix_length]
            envelope = json.dumps(
                {
                    "truncated": True,
                    "original_chars": len(content),
                    "omitted_chars": len(content) - len(prefix),
                    "content_prefix": prefix,
                },
                ensure_ascii=False,
            )
            if len(envelope) <= self.max_tool_result_chars or prefix_length == 0:
                return envelope
            prefix_length = max(
                0, prefix_length - max(1, len(envelope) - self.max_tool_result_chars)
            )

    async def invoke_tool(self, tool: Tool, tool_call: ToolCall) -> LLMMessage:
        """Invoke a tool with timeout, safe retry policy, and bounded output."""
        retries = 0
        last_error = ""
        max_retries = self.max_retries if getattr(tool, "retryable", False) else 0
        while retries <= max_retries:
            try:
                raw_result = await asyncio.wait_for(
                    tool.invoke(tool_call.args),
                    timeout=self._tool_call_timeout_seconds,
                )
                content = (
                    raw_result.model_dump_json()
                    if hasattr(raw_result, "model_dump_json")
                    else str(raw_result)
                )
                return LLMMessage.tool(
                    tool_call_id=tool_call.id,
                    name=tool.name,
                    content=self._truncate_tool_result(content),
                    artifact=raw_result,
                )
            except asyncio.TimeoutError:
                timeout_result = ToolResult(
                    success=False,
                    message=(
                        f"Tool '{tool.name}' timed out after "
                        f"{self._tool_call_timeout_seconds} seconds"
                    ),
                )
                return LLMMessage.tool(
                    tool_call_id=tool_call.id,
                    name=tool.name,
                    content=timeout_result.model_dump_json(),
                    artifact=timeout_result,
                )
            except Exception as exc:
                # Provider exceptions may contain prompts, keys, signed URLs,
                # or request bodies. Persist and log stable metadata only.
                last_error = f"Tool execution failed: {safe_exception_summary(exc)}"
                retries += 1
                if retries <= max_retries:
                    await asyncio.sleep(self.retry_interval)
                else:
                    logger.error(
                        "Tool execution failed: agent_id=%s operation=%s error=%s",
                        getattr(self, "_agent_id", "unknown"),
                        tool_call.name,
                        safe_exception_summary(exc),
                    )
                    break

        return LLMMessage.tool(
            tool_call_id=tool_call.id,
            name=tool.name,
            content=last_error,
        )

    def _handle_output_call(
        self, tool_call: ToolCall
    ) -> tuple[LLMMessage, Optional[Any]]:
        """Validate structured output and provide safe self-repair feedback."""
        output_tool = getattr(self, "_output_tool", None)
        if output_tool is None:
            raise RuntimeError("No structured output tool is active")
        try:
            output = output_tool.validate(tool_call.args)
            return (
                LLMMessage.tool(
                    tool_call_id=tool_call.id,
                    name=tool_call.name,
                    content='{"success": true}',
                ),
                output,
            )
        except ValidationError as exc:
            # Pydantic's default string contains the rejected input. Exclude it
            # from logs and feedback so a malformed secret is not duplicated.
            details = exc.errors(include_url=False, include_input=False)
            logger.warning(
                "Structured output validation failed: operation=%s errors=%d",
                tool_call.name,
                len(details),
            )
            feedback = json.dumps(details, ensure_ascii=False, default=str)
            return (
                LLMMessage.tool(
                    tool_call_id=tool_call.id,
                    name=tool_call.name,
                    content=(
                        "Invalid arguments; correct the schema errors and call "
                        f"{tool_call.name} again: {feedback}"
                    ),
                ),
                None,
            )

    @staticmethod
    def _control_tool_must_be_called_alone(name: str) -> str:
        """Return model feedback for a control call mixed with other calls."""
        return (
            f"The `{name}` control tool must be called alone in one assistant "
            "response. Other work-tool calls in this response were handled once; "
            "observe their results and then call the control tool alone if it is "
            "still needed. Do not repeat successful work automatically."
        )

    @classmethod
    def _ask_user_must_be_called_alone(cls) -> str:
        """Require an authorization/input request before any sibling work."""
        return (
            f"The `{cls._ASK_USER_TOOL_NAME}` control tool must be called alone "
            "in one assistant response. No calls in this mixed response were "
            "executed. Ask the user first, then decide which work tools are still "
            "needed after receiving the answer."
        )

    @classmethod
    def _tool_deferred_for_user_input(cls, name: str) -> str:
        """Protocol-valid result for work withheld pending user input."""
        return (
            f"The `{name}` call was not executed because this response also "
            f"called `{cls._ASK_USER_TOOL_NAME}`. Request the required user "
            "input in a separate assistant response, then call this tool again "
            "only if it is still appropriate."
        )

    async def execute(
        self,
        request: str,
        output_tool: Optional[OutputTool] = None,
        format: Optional[str] = None,
    ) -> AsyncGenerator[BaseEvent, None]:
        """Run work tools until plain text or validated structured output."""
        self._output_tool = output_tool
        response_format = format or self.format
        empty_response_retries = 0
        try:
            message = await self.ask(request, response_format)
            for _ in range(self.max_iterations):
                if not message.tool_calls:
                    if output_tool:
                        if empty_response_retries >= self.max_retries:
                            yield ErrorEvent(
                                error="Model did not submit the required structured result."
                            )
                            return
                        empty_response_retries += 1
                        message = await self.ask_with_messages(
                            [
                                LLMMessage.user(
                                    "Submit your result now by calling the "
                                    f"`{output_tool.name}` tool."
                                )
                            ],
                            response_format,
                        )
                        continue

                    if message.content.strip():
                        yield MessageEvent(message=message.content)
                        return
                    if empty_response_retries >= self.max_retries:
                        yield ErrorEvent(
                            error="Model returned an incomplete response after retries."
                        )
                        return
                    empty_response_retries += 1
                    logger.warning(
                        "Empty model response in execute loop, retrying (%d/%d)",
                        empty_response_retries,
                        self.max_retries,
                    )
                    message = await self.ask_with_messages(
                        [LLMMessage.user(self._EMPTY_RESPONSE_RETRY_PROMPT)],
                        response_format,
                    )
                    continue

                empty_response_retries = 0
                tool_responses: List[LLMMessage] = []
                structured_output: Optional[Any] = None
                # ``message_ask_user`` means execution is blocked on essential
                # input or authorization. If it appears in a parallel batch,
                # executing any sibling would risk acting before consent. Pair
                # every call with a deferred result and require a fresh, lone
                # ask-user call. Structured output is different: sibling work
                # may run once, but the premature output must be retried after
                # the model has observed those results.
                mixed_ask_user_call = len(message.tool_calls) > 1 and any(
                    tool_call.name == self._ASK_USER_TOOL_NAME
                    for tool_call in message.tool_calls
                )
                mixed_output_call = len(message.tool_calls) > 1 and any(
                    output_tool is not None and tool_call.name == output_tool.name
                    for tool_call in message.tool_calls
                )
                waiting_for_user = False
                for tool_call in message.tool_calls:
                    if not tool_call.id:
                        tool_call.id = str(uuid.uuid4())
                    function_name = tool_call.name

                    is_output_call = bool(
                        output_tool and function_name == output_tool.name
                    )
                    is_ask_user_call = function_name == self._ASK_USER_TOOL_NAME
                    if mixed_ask_user_call:
                        content = (
                            self._ask_user_must_be_called_alone()
                            if is_ask_user_call
                            else self._tool_deferred_for_user_input(function_name)
                        )
                        tool_responses.append(
                            LLMMessage.tool(
                                tool_call_id=tool_call.id,
                                name=function_name,
                                content=content,
                            )
                        )
                        continue

                    if mixed_output_call and is_output_call:
                        tool_responses.append(
                            LLMMessage.tool(
                                tool_call_id=tool_call.id,
                                name=function_name,
                                content=self._control_tool_must_be_called_alone(
                                    function_name
                                ),
                            )
                        )
                        continue

                    if is_output_call:
                        response, candidate = self._handle_output_call(tool_call)
                        tool_responses.append(response)
                        if structured_output is None and candidate is not None:
                            structured_output = candidate
                        continue

                    tool = self.get_tool(function_name)
                    if not tool:
                        # Always answer a tool call so persisted/API history is
                        # structurally valid on the next model request. This is
                        # recoverable model feedback, not a public task error.
                        tool_responses.append(
                            LLMMessage.tool(
                                tool_call_id=tool_call.id,
                                name=function_name,
                                content=f"Unknown tool: {function_name}",
                            )
                        )
                        continue

                    yield ToolEvent(
                        status=ToolStatus.CALLING,
                        tool_call_id=tool_call.id,
                        tool_name=tool.toolkit.name,
                        function_name=function_name,
                        function_args=tool_call.args,
                    )
                    tool_result = await self.invoke_tool(tool, tool_call)

                    if is_ask_user_call:
                        # Persist a structurally valid placeholder *before* the
                        # CALLED event. ExecutionAgent turns that event into a
                        # WaitEvent and closes this generator, so appending at
                        # the bottom of the loop would lose the result.  The next
                        # user turn replaces this exact call-id placeholder.
                        tool_result = tool_result.model_copy(
                            update={
                                "content": self._WAITING_FOR_USER_TOOL_RESULT,
                            }
                        )
                        tool_responses.append(tool_result)
                        await self._add_to_memory(tool_responses)
                        tool_responses = []
                        waiting_for_user = True

                    yield ToolEvent(
                        status=ToolStatus.CALLED,
                        tool_call_id=tool_call.id,
                        tool_name=tool.toolkit.name,
                        function_name=function_name,
                        function_args=tool_call.args,
                        function_result=tool_result.artifact,
                    )
                    if not is_ask_user_call:
                        tool_responses.append(tool_result)

                if waiting_for_user:
                    # A lone ask-user call is a terminal control transition for
                    # this execution turn. The concrete execution agent exposes
                    # WaitEvent; never ask the model to continue without the
                    # user's answer.
                    return

                if structured_output is not None:
                    await self._add_to_memory(tool_responses)
                    yield StructuredOutputEvent(output=structured_output)
                    return

                message = await self.ask_with_messages(
                    tool_responses, response_format
                )

            yield ErrorEvent(
                error="Maximum iteration count reached, failed to complete the task"
            )
        finally:
            self._output_tool = None

    async def _ensure_memory(self) -> None:
        if not self.memory:
            self.memory = await self._repository.get_memory(self._agent_id, self.name)

    def _ensure_current_system_prompt(self) -> None:
        """Insert or refresh the dynamic system prompt in persisted memory."""
        prompt = self.build_system_prompt()
        if not prompt:
            return
        if self.memory.empty:
            self.memory.add_message(LLMMessage.system(prompt))
            return
        first = self.memory.messages[0]
        if first.role == Role.SYSTEM:
            if first.content != prompt:
                self.memory.messages[0] = LLMMessage.system(prompt)
            return
        self.memory.messages.insert(0, LLMMessage.system(prompt))

    async def _add_to_memory(self, messages: List[LLMMessage]) -> None:
        """Refresh the system prompt, append messages, and persist memory."""
        await self._ensure_memory()
        self._ensure_current_system_prompt()
        self.memory.add_messages(messages)
        await self._repository.save_memory(self._agent_id, self.name, self.memory)

    async def _roll_back_memory(self) -> None:
        await self._ensure_memory()
        self.memory.roll_back()
        await self._repository.save_memory(self._agent_id, self.name, self.memory)

    async def ask_with_messages(
        self,
        messages: List[LLMMessage],
        format: Optional[str] = None,
    ) -> LLMMessage:
        await self._add_to_memory(messages)

        if self.memory.estimate_tokens() > self.max_context_tokens:
            self.memory.compact(max_tokens=self.max_context_tokens)
            await self._repository.save_memory(self._agent_id, self.name, self.memory)

        message = await self._llm.ask(
            messages=list(self.memory.get_messages()),
            tools=self.get_tool_schemas(),
            response_format=format,
            tool_choice=self.tool_choice,
        )
        logger.debug(
            "Model response received: agent_id=%s role=%s content_chars=%d "
            "tool_calls=%d",
            getattr(self, "_agent_id", "unknown"),
            message.role,
            len(message.content or ""),
            len(message.tool_calls or []),
        )
        await self._add_to_memory([message])
        return message

    async def ask(
        self, request: str, format: Optional[str] = None
    ) -> LLMMessage:
        return await self.ask_with_messages([LLMMessage.user(request)], format)

    async def roll_back(self, message: Message) -> None:
        await self._ensure_memory()

        # Native runs persist a placeholder before yielding WaitEvent. Replace
        # only the matching ask-user tool result so the provider sees one result
        # for the correct call id and no side effect is replayed.
        for index in range(len(self.memory.messages) - 1, -1, -1):
            existing = self.memory.messages[index]
            if (
                existing.role == Role.TOOL
                and existing.name == self._ASK_USER_TOOL_NAME
                and existing.content == self._WAITING_FOR_USER_TOOL_RESULT
            ):
                self.memory.messages[index] = LLMMessage.tool(
                    tool_call_id=existing.tool_call_id or "",
                    name=self._ASK_USER_TOOL_NAME,
                    content=message.message,
                )
                await self._repository.save_memory(
                    self._agent_id, self.name, self.memory
                )
                return

        last_message = self.memory.get_last_message()
        if (
            not last_message
            or last_message.role != Role.ASSISTANT
            or not last_message.tool_calls
        ):
            return
        ask_user_calls = [
            tool_call
            for tool_call in last_message.tool_calls
            if tool_call.name == self._ASK_USER_TOOL_NAME
        ]
        if ask_user_calls:
            # Rolling-upgrade compatibility for an older worker that persisted
            # only the assistant batch. Complete every call exactly once so the
            # history remains structurally valid, and bind the user's answer to
            # the actual ask-user call rather than blindly using the first call.
            answer_call = ask_user_calls[-1]
            responses = []
            for tool_call in last_message.tool_calls:
                responses.append(
                    LLMMessage.tool(
                        tool_call_id=tool_call.id,
                        name=tool_call.name,
                        content=(
                            message.message
                            if tool_call is answer_call
                            else self._INTERRUPTED_TOOL_RESULT
                        ),
                    )
                )
            self.memory.add_messages(responses)
        else:
            self.memory.roll_back()
        await self._repository.save_memory(self._agent_id, self.name, self.memory)

    async def compact_memory(self) -> None:
        await self._ensure_memory()
        self.memory.compact()
        await self._repository.save_memory(self._agent_id, self.name, self.memory)
