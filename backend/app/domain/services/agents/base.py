import logging
import json
import asyncio
import uuid
import contextlib
from contextlib import aclosing
from app.domain.utils.streaming import merge_stream_events
from abc import ABC
from typing import Any, List, Literal, Optional, AsyncGenerator
from app.domain.models.message import Message, LLMMessage, Role, ToolCall
from app.domain.services.tools.base import BaseToolkit, OutputTool, Tool, ValidationError, take_brief
from app.domain.models.event import (
    BaseEvent,
    ToolEvent,
    ToolStatus,
    ErrorEvent,
    MessageEvent,
    MessageDeltaEvent,
    TerminalUpdateEvent,
)
from app.domain.models.tool_result import ToolResult
from app.domain.utils.error_reporting import safe_exception_summary
from app.core.config import get_settings
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.external.llm import LLM


logger = logging.getLogger(__name__)


class StructuredOutputEvent(BaseEvent):
    """Internal event carrying validated structured output.

    Emitted when the model submits its result through an :class:`OutputTool`.
    Consumed by the concrete agents; never part of the public
    ``AgentEvent`` union streamed to clients.
    """

    type: Literal["structured_output"] = "structured_output"
    output: Any
    message_id: Optional[str] = None


class BaseAgent(ABC):
    """
    Base agent class, defining the basic behavior of the agent
    """

    name: str = ""
    max_iterations: int = 100
    max_retries: int = 3
    retry_interval: float = 1.0
    tool_choice: Optional[str] = None
    # Context engineering budgets: tool results are truncated at ingestion,
    # and memory is compacted before each model call when over budget.
    max_tool_result_chars: int = 16000
    max_context_tokens: int = 100000

    def __init__(
        self,
        agent_id: str,
        agent_repository: AgentRepository,
        llm: LLM,
        tools: List[BaseToolkit] = []
    ):
        self._agent_id = agent_id
        self._repository = agent_repository
        self._llm = llm
        self.toolkits = tools
        self.memory = None
        self._output_tool: Optional[OutputTool] = None
        self._stream_queue = asyncio.Queue(maxsize=64)
        self._stream_message_id = None
        self._project_instruction: Optional[str] = None
        self._skill_catalog: Optional[str] = None
        self._skill_context: Optional[str] = None
        self._tool_call_timeout_seconds = get_settings().tool_call_timeout_seconds

    def stream_events(self, events):
        return merge_stream_events(events, self._stream_queue)

    async def _ask_model(self, context):
        self._stream_message_id = None
        stream = getattr(self._llm, "ask_stream", None)
        # Planner/step JSON and tool instructions remain private. Single-loop
        # final results become visible only after the plan completion guard.
        can_stream = self._output_tool and self._output_tool.name == "deliver_result" and getattr(self, "_plan_finished", False)
        if not stream or not can_stream or not get_settings().stream_responses:
            return await self._llm.ask(messages=context, tools=self.get_tool_schemas(), tool_choice=self.tool_choice)
        message_id = str(uuid.uuid4())
        previous = ""
        reply = None
        async with aclosing(stream(messages=context, tools=self.get_tool_schemas(), tool_choice=self.tool_choice, output_tool="deliver_result")) as chunks:
            async for chunk in chunks:
                if chunk.reset:
                    await self._stream_queue.put(MessageDeltaEvent(message_id=message_id, reset=True))
                    previous = ""
                elif chunk.message is not None:
                    reply = chunk.message
                elif chunk.text != previous:
                    # Bound event volume; small trailing chunks flush at the end.
                    reset = not chunk.text.startswith(previous)
                    if reset or len(chunk.text) - len(previous) >= 64:
                        await self._stream_queue.put(MessageDeltaEvent(message_id=message_id, delta=chunk.text if reset else chunk.text[len(previous):], offset=0 if reset else len(previous), reset=reset))
                        previous = chunk.text
        if reply is None:
            raise RuntimeError("Model stream ended without a canonical response")
        self._stream_message_id = message_id
        return reply

    def set_project_instruction(self, instruction: Optional[str]) -> None:
        """Bind project-level guidance used when assembling the system prompt."""
        text = (instruction or "").strip()
        self._project_instruction = text or None

    def set_skill_catalog(self, catalog: Optional[str]) -> None:
        """Bind L1 skill metadata catalog (name + description per enabled skill)."""
        text = (catalog or "").strip()
        self._skill_catalog = text or None

    def set_skill_context(self, context: Optional[str]) -> None:
        """Bind active skill guidance for the current user turn."""
        text = (context or "").strip()
        self._skill_context = text or None

    def build_system_prompt(self) -> str:
        """Assemble the system prompt for this agent; overridden by subclasses."""
        return getattr(self, "system_prompt", "")

    async def sync_system_prompt(self) -> None:
        """Insert or refresh the leading system message so project edits apply."""
        await self._ensure_memory()
        prompt = self.build_system_prompt()
        if self.memory.empty:
            self.memory.add_message(LLMMessage.system(prompt))
            await self._repository.save_memory(self._agent_id, self.name, self.memory)
            return
        first = self.memory.messages[0]
        if first.role == Role.SYSTEM and first.content != prompt:
            first.content = prompt
            await self._repository.save_memory(self._agent_id, self.name, self.memory)

    def get_tool(self, name: str) -> Optional[Tool]:
        """Get specified tool"""
        for toolkit in self.toolkits:
            tool = toolkit.get_tool(name)
            if tool:
                return tool
        return None

    def get_tool_schemas(self) -> List[dict]:
        """Get OpenAI function schemas for all available tools.

        Includes the active output tool, if any, so the model can submit
        structured results through native function calling.
        """
        schemas = [schema for toolkit in self.toolkits for schema in toolkit.get_tool_schemas()]
        if self._output_tool:
            schemas.append(self._output_tool.to_openai_schema())
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

    def _handle_output_call(self, tool_call: ToolCall) -> tuple[LLMMessage, Optional[Any]]:
        """Validate a structured-output tool call.

        Returns the tool response message to append to memory and, on
        success, the validated output model. On validation failure the
        response carries the error so the model can self-repair on the next
        iteration.
        """
        try:
            output = self._output_tool.validate(tool_call.args)
            response = LLMMessage.tool(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content='{"success": true}',
            )
            return response, output
        except ValidationError as e:
            logger.warning(f"Structured output validation failed for {tool_call.name}: {e}")
            response = LLMMessage.tool(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=f"Invalid arguments, please correct and call {tool_call.name} again: {e}",
            )
            return response, None

    async def execute(
        self,
        request: str,
        output_tool: Optional[OutputTool] = None,
    ) -> AsyncGenerator[BaseEvent, None]:
        """Run the agent loop.

        The model works with native tool calling. When ``output_tool`` is
        provided, the loop finishes when the model calls it with valid
        arguments, yielding a :class:`StructuredOutputEvent`. Otherwise a
        plain assistant message ends the loop with a :class:`MessageEvent`.
        """
        self._output_tool = output_tool
        try:
            message = await self.ask(request)
            async with aclosing(self._tool_loop(message)) as events:
                async for event in events:
                    yield event
        finally:
            self._output_tool = None

    async def continue_execute(
        self,
        output_tool: Optional[OutputTool] = None,
    ) -> AsyncGenerator[BaseEvent, None]:
        """Resume the tool loop from current memory without adding a user turn."""
        self._output_tool = output_tool
        try:
            await self._ensure_memory()
            if self.memory.estimate_tokens() > self.max_context_tokens:
                self.memory.compact(max_tokens=self.max_context_tokens)
                await self._repository.save_memory(
                    self._agent_id, self.name, self.memory
                )

            message = await self._ask_model(list(self.memory.get_messages()))
            logger.debug(f"Response from model: {message}")
            await self._add_to_memory([message])

            async with aclosing(self._tool_loop(message)) as events:
                async for event in events:
                    yield event
        finally:
            self._output_tool = None

    async def _tool_loop(
        self,
        message: LLMMessage,
    ) -> AsyncGenerator[BaseEvent, None]:
        """Process model tool calls until the run produces a final event."""
        for _ in range(self.max_iterations):
            if not message.tool_calls:
                # Plain message: final answer for unstructured runs; for
                # structured runs, nudge the model to use the output tool.
                if not self._output_tool:
                    break
                message = await self.ask(
                    f"Submit your result by calling the `{self._output_tool.name}` tool."
                )
                continue

            tool_responses = []
            structured_output: Optional[Any] = None
            for tool_call in message.tool_calls:
                function_name = tool_call.name
                if not tool_call.id:
                    tool_call.id = str(uuid.uuid4())
                tool_call_id = tool_call.id
                brief, function_args = take_brief(tool_call.args)

                if (
                    self._output_tool
                    and function_name == self._output_tool.name
                ):
                    if function_name == "deliver_result" and len(message.tool_calls) != 1:
                        response, structured_output = LLMMessage.tool(tool_call_id=tool_call.id, name=function_name, content="Call deliver_result alone after all work and plan updates are complete."), None
                    else:
                        response, structured_output = self._handle_output_call(tool_call)
                    tool_responses.append(response)
                    if structured_output is None and self._stream_message_id:
                        await self._stream_queue.put(MessageDeltaEvent(message_id=self._stream_message_id, reset=True))
                        self._stream_message_id = None
                    continue

                tool = self.get_tool(function_name)
                if not tool:
                    yield ErrorEvent(error=f"Unknown tool: {function_name}")
                    tool_responses.append(LLMMessage.tool(
                        tool_call_id=tool_call_id,
                        name=function_name,
                        content=f"Unknown tool: {function_name}",
                    ))
                    continue

                # Generate event before tool call
                yield ToolEvent(
                    status=ToolStatus.CALLING,
                    tool_call_id=tool_call_id,
                    tool_name=tool.toolkit.name,
                    function_name=function_name,
                    function_args=function_args,
                    brief=brief,
                )

                # Official terminalUpdate: poll shell console while the tool runs
                shell_id = (
                    function_args.get("id")
                    if tool.toolkit.name == "shell" and isinstance(function_args, dict)
                    else None
                )
                if shell_id and hasattr(tool.toolkit, "sandbox"):
                    invoke_task = asyncio.create_task(self.invoke_tool(tool, tool_call))
                    last_fingerprint: Optional[str] = None

                    def _console_fingerprint(console: Any) -> str:
                        """Cheap change detector — avoid repr() on large consoles."""
                        if console is None:
                            return "0:"
                        if isinstance(console, str):
                            return f"s:{len(console)}:{console[-80:]}"
                        if isinstance(console, list):
                            if not console:
                                return "0:"
                            last = console[-1]
                            if isinstance(last, dict):
                                tail = f"{last.get('command', '')}|{str(last.get('output', ''))[-60:]}"
                            else:
                                tail = str(last)[-80:]
                            return f"l:{len(console)}:{tail}"
                        return f"o:{type(console).__name__}:{str(console)[-80:]}"

                    try:
                        while not invoke_task.done():
                            done, _ = await asyncio.wait({invoke_task}, timeout=1.0)
                            if done:
                                break
                            try:
                                view = await tool.toolkit.sandbox.view_shell(
                                    shell_id, console=True
                                )
                                console = (
                                    view.data.get("console", [])
                                    if view and getattr(view, "data", None)
                                    else []
                                )
                                fingerprint = _console_fingerprint(console)
                                if fingerprint != last_fingerprint:
                                    last_fingerprint = fingerprint
                                    yield TerminalUpdateEvent(
                                        shell_id=shell_id,
                                        output=console,
                                    )
                            except Exception:
                                logger.debug(
                                    "Shell live poll failed for %s",
                                    shell_id,
                                    exc_info=True,
                                )
                        tool_result = await invoke_task
                    finally:
                        if not invoke_task.done():
                            invoke_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await invoke_task
                else:
                    tool_result = await self.invoke_tool(tool, tool_call)

                if function_name != "message_ask_user":
                    await self._add_to_memory([tool_result])
                # Generate event after tool call
                yield ToolEvent(
                    status=ToolStatus.CALLED,
                    tool_call_id=tool_call_id,
                    tool_name=tool.toolkit.name,
                    function_name=function_name,
                    function_args=function_args,
                    function_result=tool_result.artifact,
                    brief=brief,
                )

                if function_name == "message_ask_user":
                    tool_responses.append(tool_result)

            if structured_output is not None:
                # Persist the tool responses so the tool-call pairing in
                # memory stays consistent, then finish.
                await self._add_to_memory(tool_responses)
                yield StructuredOutputEvent(output=structured_output, message_id=self._stream_message_id)
                return

            message = await self.ask_with_messages(tool_responses)
        else:
            yield ErrorEvent(error="Maximum iteration count reached, failed to complete the task")

        yield MessageEvent(message=message.content)

    async def _ensure_memory(self):
        if not self.memory:
            self.memory = await self._repository.get_memory(self._agent_id, self.name)

    def _ensure_current_system_prompt(self) -> None:
        """Refresh the dynamic prompt when project or skill guidance changes."""
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
        """Update memory and save to repository"""
        await self._ensure_memory()
        self._ensure_current_system_prompt()
        self.memory.add_messages(messages)
        await self._repository.save_memory(self._agent_id, self.name, self.memory)

    async def _roll_back_memory(self) -> None:
        await self._ensure_memory()
        self.memory.roll_back()
        await self._repository.save_memory(self._agent_id, self.name, self.memory)

    async def ask_with_messages(self, messages: List[LLMMessage]) -> LLMMessage:
        await self._add_to_memory(messages)

        # Token-aware guard: reclaim budget from old tool results before the
        # context is sent to the model.
        if self.memory.estimate_tokens() > self.max_context_tokens:
            self.memory.compact(max_tokens=self.max_context_tokens)
            await self._repository.save_memory(self._agent_id, self.name, self.memory)

        context = list(self.memory.get_messages())
        message = await self._ask_model(context)
        logger.debug(f"Response from model: {message}")

        await self._add_to_memory([message])
        return message

    async def ask(self, request: str) -> LLMMessage:
        return await self.ask_with_messages([
            LLMMessage.user(request)
        ])

    async def roll_back(self, message: Message):
        await self._ensure_memory()
        messages = self.memory.messages
        assistant_index = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].role == Role.ASSISTANT), None)
        if assistant_index is None:
            return
        last_message = messages[assistant_index]
        if not last_message.tool_calls or any(m.role != Role.TOOL for m in messages[assistant_index + 1:]):
            return
        answered = {m.tool_call_id for m in messages[assistant_index + 1:] if m.role == Role.TOOL}
        for call in last_message.tool_calls:
            if call.id in answered:
                continue
            content = message.message if call.name == "message_ask_user" else '{"success":false,"message":"Interrupted tool call; its outcome is unconfirmed. Verify before retrying."}'
            self.memory.add_message(LLMMessage.tool(tool_call_id=call.id, name=call.name, content=content))
        await self._repository.save_memory(self._agent_id, self.name, self.memory)

    async def compact_memory(self) -> None:
        await self._ensure_memory()
        self.memory.compact()
        await self._repository.save_memory(self._agent_id, self.name, self.memory)
