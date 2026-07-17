"""Native Anthropic LLM adapter used for DNS-pinned BYOK endpoints."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from app.core.config import Settings
from app.domain.models.message import LLMMessage, Role, ToolCall
from app.domain.utils.model_output import normalize_model_content
from app.infrastructure.external.llm.security import (
    create_pinned_model_http_client,
)


class AnthropicLLM:
    """Domain LLM gateway backed by the official asynchronous Anthropic SDK."""

    _JSON_REPAIR_PROMPT = (
        "Extract or repair the JSON object from the following text. "
        "Return only valid JSON.\n\n{text}"
    )

    def __init__(self, settings: Settings, max_retries: int = 3):
        if not settings.api_base or not settings.byok_pinned_ip:
            raise RuntimeError("Anthropic BYOK requires a pinned endpoint")
        self._model = settings.model_name
        self._temperature = settings.temperature
        self._max_tokens = settings.max_tokens
        self._http_client = create_pinned_model_http_client(
            settings.api_base, settings.byok_pinned_ip
        )
        self._client = AsyncAnthropic(
            api_key=settings.api_key,
            base_url=settings.api_base,
            default_headers=settings.extra_headers or None,
            max_retries=max_retries,
            http_client=self._http_client,
        )
        self._closed = False

    @staticmethod
    def _to_anthropic_messages(
        messages: List[LLMMessage],
    ) -> tuple[List[Dict[str, Any]], Optional[str]]:
        converted: List[Dict[str, Any]] = []
        system_parts: List[str] = []
        for message in messages:
            if message.role == Role.SYSTEM:
                if message.content:
                    system_parts.append(message.content)
                continue
            if message.role == Role.TOOL:
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id or "tool_result",
                                "content": message.content,
                            }
                        ],
                    }
                )
                continue
            role = "assistant" if message.role == Role.ASSISTANT else "user"
            if role == "assistant" and message.tool_calls:
                blocks: List[Dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": tool.id or tool.name,
                        "name": tool.name,
                        "input": tool.args or {},
                    }
                    for tool in message.tool_calls
                )
                content: Any = blocks
            else:
                content = message.content
            converted.append({"role": role, "content": content})
        return converted, "\n\n".join(system_parts) or None

    @staticmethod
    def _convert_tools(tools: Optional[List[Dict[str, Any]]]) -> list[dict] | None:
        converted = []
        for raw_tool in tools or []:
            function = (
                raw_tool.get("function")
                if raw_tool.get("type") == "function"
                else raw_tool
            )
            if not isinstance(function, dict) or not function.get("name"):
                continue
            converted.append(
                {
                    "name": function["name"],
                    "description": function.get("description") or "",
                    "input_schema": function.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
        return converted or None

    @staticmethod
    def _tool_choice(value: Optional[str]) -> dict | None:
        if value in (None, "auto"):
            return None
        if value in ("required", "any"):
            return {"type": "any"}
        if value == "none":
            return {"type": "none"}
        return {"type": "tool", "name": value}

    async def ask(
        self,
        messages: List[LLMMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[str] = None,
        tool_choice: Optional[str] = None,
    ) -> LLMMessage:
        payload, system = self._to_anthropic_messages(messages)
        kwargs: Dict[str, Any] = {
            "model": self._model,
            "messages": payload,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        if system:
            kwargs["system"] = system
        converted_tools = self._convert_tools(tools)
        choice = self._tool_choice(tool_choice)
        if converted_tools and choice != {"type": "none"}:
            kwargs["tools"] = converted_tools
            if choice:
                kwargs["tool_choice"] = choice

        response = await self._client.messages.create(**kwargs)
        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        for block in response.content or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(str(getattr(block, "text", "") or ""))
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=str(getattr(block, "id", "") or ""),
                        name=str(getattr(block, "name", "") or ""),
                        args=getattr(block, "input", None) or {},
                    )
                )
        return LLMMessage.assistant(
            content=normalize_model_content("".join(text_parts)),
            tool_calls=tool_calls,
        )

    async def parse_json(self, text: str) -> Dict[str, Any]:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        response = await self.ask(
            [
                LLMMessage.user(
                    self._JSON_REPAIR_PROMPT.format(text=text)
                )
            ]
        )
        parsed = json.loads(response.content)
        if not isinstance(parsed, dict):
            raise ValueError("Model did not return a JSON object")
        return parsed

    async def aclose(self) -> None:
        """Close the per-run SDK/HTTP clients idempotently."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._client.close()
        finally:
            if not self._http_client.is_closed:
                await self._http_client.aclose()
