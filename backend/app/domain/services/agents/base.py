import logging
import asyncio
import uuid
from abc import ABC
from typing import List, Dict, Any, Optional, AsyncGenerator
from app.domain.models.message import Message
from app.domain.services.tools.base import BaseToolkit
from app.domain.models.event import (
    BaseEvent,
    ToolEvent,
    ToolStatus,
    ErrorEvent,
    MessageEvent,
)
from app.domain.repositories.agent_repository import AgentRepository
from langchain.chat_models import init_chat_model
from langchain_classic.output_parsers.retry import RetryWithErrorOutputParser
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from app.core.config import get_settings
from langchain.messages import AIMessage, HumanMessage, ToolCall, ToolMessage, SystemMessage
from app.domain.services.tools.base import Tool
from app.domain.utils.robust_json_parser import RobustJsonParser, ToolCallParseError
from app.domain.models.tool_result import ToolResult


logger = logging.getLogger(__name__)
class BaseAgent(ABC):
    """
    Base agent class, defining the basic behavior of the agent
    """

    name: str = ""
    system_prompt: str = ""
    format: Optional[str] = None
    max_iterations: int = 100
    max_retries: int = 3
    retry_interval: float = 1.0
    tool_choice: Optional[str] = None

    _JSON_PARSE_PROMPT = PromptTemplate.from_template(
        "Extract or repair the JSON from the following LLM output.\n\n{input}"
    )
    _EMPTY_TOOL_USE_RETRY_PROMPT = (
        "Your previous response stopped for a tool call but did not include a "
        "valid tool call payload. Please either call exactly one available tool "
        "with complete JSON arguments, or respond with the required final text."
    )
    _EMPTY_RESPONSE_RETRY_PROMPT = (
        "Your previous response was empty. Continue the task now. If a tool is "
        "needed, call exactly one available tool with complete JSON arguments. "
        "If no tool is needed, respond with the required final text."
    )
    _EMPTY_TOOL_USE_FALLBACK_PROMPT = (
        "Tool calling failed repeatedly because the provider returned empty "
        "tool_use responses. Do not call tools now. Use the observations and "
        "tool results already in the conversation to produce the required final "
        "response. If the current agent requires JSON, return valid JSON only."
    )

    def __init__(
        self,
        agent_id: str,
        agent_repository: AgentRepository,
        tools: Optional[List[BaseToolkit]] = None
    ):
        settings = get_settings()
        self._agent_id = agent_id
        self._repository = agent_repository
        self._model_provider = settings.model_provider
        self._tool_call_timeout_seconds = settings.tool_call_timeout_seconds
        kwargs = dict(
            model=settings.model_name,
            model_provider=settings.model_provider,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            base_url=settings.api_base,
        )
        if settings.extra_headers:
            kwargs["default_headers"] = settings.extra_headers
        self._model = init_chat_model(**kwargs)
        self._json_output_parser = RetryWithErrorOutputParser.from_llm(
            parser=JsonOutputParser(),
            llm=self._model,
            max_retries=self.max_retries,
        )
        self.toolkits = tools or []
        self.memory = None

    async def _parse_json(self, text: str) -> dict:
        """Parse JSON from LLM output using RetryWithErrorOutputParser."""
        prompt_value = self._JSON_PARSE_PROMPT.format_prompt(input=text)
        return await self._json_output_parser.aparse_with_prompt(text, prompt_value)
    
    def get_tool(self, name: str) -> Optional[Tool]:
        """Get specified tool"""
        for toolkit in self.toolkits:
            tool = toolkit.get_tool(name)
            if tool:
                return tool
        return None

    def get_tools(self) -> List[Tool]:
        """Get all available tools list"""
        return [tool for toolkit in self.toolkits for tool in toolkit.get_tools()]

    def _is_empty_tool_use_response(self, message: AIMessage) -> bool:
        return (
            message.response_metadata.get("stop_reason") == "tool_use"
            and not message.tool_calls
            and not message.invalid_tool_calls
            and not MessageEvent.normalize_message(message.content).strip()
        )

    async def invoke_tool(self, tool: Tool, tool_call: ToolCall) -> ToolMessage:
        """Invoke specified tool, with retry mechanism."""
        retries = 0
        while retries <= self.max_retries:
            try:
                return await asyncio.wait_for(
                    tool.ainvoke(tool_call),
                    timeout=self._tool_call_timeout_seconds,
                )
            except asyncio.TimeoutError:
                timeout_result = ToolResult(
                    success=False,
                    message=(
                        f"Tool '{tool.name}' timed out after "
                        f"{self._tool_call_timeout_seconds} seconds"
                    ),
                )
                return ToolMessage(
                    tool_call_id=tool_call["id"],
                    name=tool.name,
                    content=timeout_result.model_dump_json(),
                    artifact=timeout_result,
                )
            except Exception as e:
                last_error = str(e)
                retries += 1
                if retries <= self.max_retries:
                    await asyncio.sleep(self.retry_interval)
                else:
                    logger.exception(f"Tool execution failed, {tool_call['name']}, {tool_call['args']}")
                    break

        return ToolMessage(tool_call_id=tool_call["id"], name=tool.name, content=last_error)
    
    async def execute(self, request: str, format: Optional[str] = None) -> AsyncGenerator[BaseEvent, None]:
        format = format or self.format
        message = await self.ask(request, format)
        empty_response_retries = 0
        for _ in range(self.max_iterations):
            if message.tool_calls:
                empty_response_retries = 0
                tool_responses = []
                for tool_call in message.tool_calls:
                    function_name = tool_call["name"]
                    tool_call_id = tool_call["id"] = tool_call["id"] or str(uuid.uuid4())
                    function_args = tool_call["args"]

                    tool = self.get_tool(function_name)
                    if not tool:
                        yield ErrorEvent(error=f"Unknown tool: {function_name}")
                        continue

                    # Generate event before tool call
                    yield ToolEvent(
                        status=ToolStatus.CALLING,
                        tool_call_id=tool_call_id,
                        tool_name=tool.toolkit.name,
                        function_name=function_name,
                        function_args=function_args
                    )

                    tool_result = await self.invoke_tool(tool, tool_call)

                    # Generate event after tool call
                    yield ToolEvent(
                        status=ToolStatus.CALLED,
                        tool_call_id=tool_call_id,
                        tool_name=tool.toolkit.name,
                        function_name=function_name,
                        function_args=function_args,
                        function_result=tool_result.artifact
                    )

                    tool_responses.append(tool_result)

                message = await self.ask_with_messages(tool_responses)
                continue

            response_text = MessageEvent.normalize_message(message.content)
            if response_text.strip():
                yield MessageEvent(message=response_text)
                return

            if empty_response_retries < self.max_retries:
                empty_response_retries += 1
                logger.warning(
                    "Empty model response in execute loop, retrying (%d/%d): %s",
                    empty_response_retries,
                    self.max_retries,
                    message,
                )
                message = await self.ask_with_messages([
                    HumanMessage(content=self._EMPTY_RESPONSE_RETRY_PROMPT)
                ], format)
                continue

            yield ErrorEvent(error="Model returned an incomplete response after retries.")
            return
        else:
            yield ErrorEvent(error="Maximum iteration count reached, failed to complete the task")
    
    async def _ensure_memory(self):
        if not self.memory:
            self.memory = await self._repository.get_memory(self._agent_id, self.name)

    def _ensure_current_system_prompt(self) -> None:
        current_prompt = self.system_prompt
        if not current_prompt:
            return
        if self.memory.empty:
            self.memory.add_message(SystemMessage(content=current_prompt))
            return
        first_message = self.memory.messages[0]
        if getattr(first_message, "type", None) == "system":
            if first_message.content != current_prompt:
                self.memory.messages[0] = SystemMessage(content=current_prompt)
            return
        self.memory.messages.insert(0, SystemMessage(content=current_prompt))
    
    async def _add_to_memory(self, messages: List[Dict[str, Any]]) -> None:
        """Update memory and save to repository"""
        await self._ensure_memory()
        self._ensure_current_system_prompt()
        self.memory.add_messages(messages)
        await self._repository.save_memory(self._agent_id, self.name, self.memory)
    
    async def _roll_back_memory(self) -> None:
        await self._ensure_memory()
        self.memory.roll_back()
        await self._repository.save_memory(self._agent_id, self.name, self.memory)

    async def ask_with_messages(self, messages: List[Dict[str, Any]], format: Optional[str] = None) -> AIMessage:
        await self._add_to_memory(messages)

        response_format = None
        if format and self._model_provider != "anthropic":
            response_format = {"type": format}

        bind_kwargs = {}
        if self.tool_choice is not None:
            bind_kwargs["tool_choice"] = self.tool_choice
        if response_format:
            bind_kwargs["response_format"] = response_format

        def build_chain(use_tools: bool = True):
            bound_model = self._model.bind(**bind_kwargs)
            if use_tools:
                bound_model = bound_model.bind_tools(self.get_tools())
            return bound_model | RobustJsonParser.from_llm(self._model)

        # Stage 1-3: model chain | RobustJsonParser repairs invalid tool call JSON.
        # Stages 4-5: outer retry loop handles cases that survive stages 1-3.
        chain = build_chain(use_tools=True)
        context = list(self.memory.get_messages())
        saw_empty_tool_use = False
        for attempt in range(self.max_retries):
            try:
                message: AIMessage = await chain.ainvoke(context)
                if self._is_empty_tool_use_response(message):
                    saw_empty_tool_use = True
                    if attempt == self.max_retries - 1:
                        break
                    logger.warning(
                        "Attempt %d/%d: model returned empty tool_use response, retrying",
                        attempt + 1,
                        self.max_retries,
                    )
                    context = context + [
                        HumanMessage(content=self._EMPTY_TOOL_USE_RETRY_PROMPT),
                    ]
                    continue
                break
            except ToolCallParseError as e:
                if attempt == self.max_retries - 1:
                    raise
                logger.warning(
                    "Attempt %d/%d: tool call JSON repair failed, retrying model",
                    attempt + 1, self.max_retries,
                )
                if attempt == 0:
                    # Stage 4 (RetryOutputParser style): silent retry, same context.
                    pass
                else:
                    # Stage 5 (RetryWithErrorOutputParser style): add error feedback.
                    context = e.make_retry_context(context)

        if saw_empty_tool_use and self._is_empty_tool_use_response(message):
            logger.warning(
                "Model kept returning empty tool_use responses; retrying once without tools"
            )
            fallback_context = list(self.memory.get_messages()) + [
                HumanMessage(content=self._EMPTY_TOOL_USE_FALLBACK_PROMPT)
            ]
            message = await build_chain(use_tools=False).ainvoke(fallback_context)

        logger.debug(f"Response from model: {message}")

        await self._add_to_memory([message])
        return message

    async def ask(self, request: str, format: Optional[str] = None) -> AIMessage:
        return await self.ask_with_messages([
            HumanMessage(content=request)
        ], format)
    
    async def roll_back(self, message: Message):
        await self._ensure_memory()
        last_message = self.memory.get_last_message()
        if not last_message:
            return
        if last_message.type != "ai":
            return
        if not last_message.tool_calls:
            return
        tool_call = last_message.tool_calls[0]
        function_name = tool_call["name"]
        tool_call_id = tool_call["id"]
        if function_name == "message_ask_user":
            self.memory.add_message(ToolMessage(tool_call_id=tool_call_id, name=function_name, content=message))
        else:
            self.memory.roll_back()
        await self._repository.save_memory(self._agent_id, self.name, self.memory)
    
    async def compact_memory(self) -> None:
        await self._ensure_memory()
        self.memory.compact()
        await self._repository.save_memory(self._agent_id, self.name, self.memory)
