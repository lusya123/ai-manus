"""LangChain implementation of the domain :class:`LLM` gateway.

Keeps all LangChain-specific concerns — model instantiation, message
translation, tool binding, the JSON-repair chain and model-level retries —
inside the infrastructure layer, so the domain agents depend only on the
:class:`app.domain.external.llm.LLM` Protocol and domain message types.
"""
import logging
from functools import lru_cache
from typing import Any, Dict, List, Optional

from langchain.chat_models import init_chat_model
from langchain.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_classic.output_parsers.retry import RetryWithErrorOutputParser
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate

from app.core.config import Settings, get_settings
from app.domain.models.message import LLMMessage, Role, ToolCall
from app.domain.utils.model_output import normalize_model_content
from app.infrastructure.external.llm.robust_json_parser import (
    RobustJsonParser,
    ToolCallParseError,
)
from app.infrastructure.external.llm.model_capabilities import (
    effective_temperature,
)
from app.infrastructure.external.llm.security import provider_api_base, provider_api_key

logger = logging.getLogger(__name__)


class LangchainLLM:
    """Concrete :class:`LLM` gateway backed by LangChain chat models."""

    _JSON_PARSE_PROMPT = PromptTemplate.from_template(
        "Extract or repair the JSON from the following LLM output.\n\n{input}"
    )
    _EMPTY_TOOL_USE_RETRY_PROMPT = (
        "Your previous response stopped for a tool call but did not include a "
        "valid tool call payload. Please either call exactly one available tool "
        "with complete JSON arguments, or respond with the required final text."
    )
    _EMPTY_TOOL_USE_FALLBACK_PROMPT = (
        "Tool calling failed repeatedly because the provider returned empty "
        "tool_use responses. Do not call tools now. Use the observations and "
        "tool results already in the conversation to produce the required final "
        "response. If JSON is required, return valid JSON only."
    )

    def __init__(self, settings: Optional[Settings] = None, max_retries: int = 3):
        settings = settings or get_settings()
        self._max_retries = max_retries
        self._model_provider = settings.model_provider.lower()
        api_key = provider_api_key(settings, self._model_provider)
        api_base = provider_api_base(
            settings, self._model_provider, settings.api_base
        )

        kwargs: Dict[str, Any] = dict(
            model=settings.model_name,
            model_provider=self._model_provider,
            max_tokens=settings.max_tokens,
            base_url=api_base,
        )
        temperature = effective_temperature(
            self._model_provider,
            settings.model_name,
            settings.temperature,
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        if api_key:
            kwargs["api_key"] = api_key
        if settings.extra_headers:
            kwargs["default_headers"] = settings.extra_headers
        self._model = init_chat_model(**kwargs)

        self._json_output_parser = RetryWithErrorOutputParser.from_llm(
            parser=JsonOutputParser(),
            llm=self._model,
            max_retries=self._max_retries,
        )
        self._closed = False

    # ------------------------------------------------------------------
    # Message translation (domain <-> LangChain)
    # ------------------------------------------------------------------

    def _to_langchain(self, messages: List[LLMMessage]) -> List[Any]:
        lc_messages: List[Any] = []
        for m in messages:
            if m.role == Role.SYSTEM:
                lc_messages.append(SystemMessage(content=m.content))
            elif m.role == Role.USER:
                lc_messages.append(HumanMessage(content=m.content))
            elif m.role == Role.ASSISTANT:
                tool_calls = [
                    {
                        "name": tc.name,
                        "args": tc.args,
                        "id": tc.id or None,
                        "type": "tool_call",
                    }
                    for tc in m.tool_calls
                ]
                lc_messages.append(
                    AIMessage(content=m.content, tool_calls=tool_calls)
                )
            elif m.role == Role.TOOL:
                lc_messages.append(
                    ToolMessage(
                        tool_call_id=m.tool_call_id or "",
                        name=m.name,
                        content=m.content,
                    )
                )
        return lc_messages

    def _from_langchain(self, message: AIMessage) -> LLMMessage:
        tool_calls = [
            ToolCall(
                id=tc.get("id") or "",
                name=tc.get("name") or "",
                args=tc.get("args") or {},
            )
            for tc in (message.tool_calls or [])
        ]
        content = normalize_model_content(message.content)
        return LLMMessage.assistant(content=content, tool_calls=tool_calls)

    @staticmethod
    def _is_empty_tool_use_response(message: AIMessage) -> bool:
        stop_reason = (message.response_metadata or {}).get("stop_reason")
        return (
            stop_reason == "tool_use"
            and not message.tool_calls
            and not message.invalid_tool_calls
            and not normalize_model_content(message.content).strip()
        )

    # ------------------------------------------------------------------
    # LLM Protocol
    # ------------------------------------------------------------------

    async def ask(
        self,
        messages: List[LLMMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[str] = None,
        tool_choice: Optional[str] = None,
    ) -> LLMMessage:
        bind_kwargs: Dict[str, Any] = {}
        if response_format and self._model_provider != "anthropic":
            bind_kwargs["response_format"] = {"type": response_format}
        if tool_choice is not None:
            bind_kwargs["tool_choice"] = tool_choice

        def build_chain(use_tools: bool = True):
            effective_bind_kwargs = dict(bind_kwargs)
            if not use_tools or not tools:
                # A provider cannot satisfy a required/forced tool choice when
                # the recovery path deliberately removes all tool schemas.
                # Keeping it here makes the empty-tool-use fallback fail at
                # the transport layer instead of producing a final answer.
                effective_bind_kwargs.pop("tool_choice", None)
                model = self._model.bind(**effective_bind_kwargs)
            else:
                # ``bind_tools`` creates a new runnable from the underlying
                # chat model. Calling it after ``bind(tool_choice=...)`` can
                # replace that earlier binding, silently turning a required
                # output-tool call back into an optional one. Bind the schemas
                # and all invocation options atomically instead.
                if (
                    self._model_provider == "anthropic"
                    and effective_bind_kwargs.get("tool_choice") == "required"
                ):
                    # LangChain's Anthropic adapter names the cross-provider
                    # "at least one tool" choice ``any``.
                    effective_bind_kwargs["tool_choice"] = "any"
                model = self._model.bind_tools(tools, **effective_bind_kwargs)
            return model | RobustJsonParser.from_llm(self._model)

        # Stages 1-3: RobustJsonParser repairs invalid tool call JSON locally
        # and via a cheap fixing call. Stages 4-5: this outer loop retries the
        # model, silently first then with error feedback.
        chain = build_chain(use_tools=True)

        original_context = self._to_langchain(messages)
        context = list(original_context)
        message: Optional[AIMessage] = None
        saw_empty_tool_use = False
        for attempt in range(self._max_retries):
            try:
                message = await chain.ainvoke(context)
                if self._is_empty_tool_use_response(message):
                    saw_empty_tool_use = True
                    if attempt == self._max_retries - 1:
                        break
                    logger.warning(
                        "Attempt %d/%d: model returned empty tool_use response, retrying",
                        attempt + 1,
                        self._max_retries,
                    )
                    context = context + [
                        HumanMessage(content=self._EMPTY_TOOL_USE_RETRY_PROMPT)
                    ]
                    continue
                break
            except ToolCallParseError as e:
                if attempt == self._max_retries - 1:
                    raise
                logger.warning(
                    "Attempt %d/%d: tool call JSON repair failed, retrying model",
                    attempt + 1,
                    self._max_retries,
                )
                if attempt > 0:
                    # Stage 5: append the failed message and error feedback.
                    context = e.make_retry_context(context)

        if message is None:
            raise RuntimeError("Model did not return a response")

        if saw_empty_tool_use and self._is_empty_tool_use_response(message):
            logger.warning(
                "Model kept returning empty tool_use responses; retrying once without tools"
            )
            message = await build_chain(use_tools=False).ainvoke(
                original_context
                + [HumanMessage(content=self._EMPTY_TOOL_USE_FALLBACK_PROMPT)]
            )

        logger.debug(
            "Model response received: content_chars=%d tool_calls=%d",
            len(str(message.content or "")),
            len(message.tool_calls or []),
        )
        return self._from_langchain(message)

    async def parse_json(self, text: str) -> Dict[str, Any]:
        """Extract/repair a JSON object from raw model output."""
        prompt_value = self._JSON_PARSE_PROMPT.format_prompt(input=text)
        return await self._json_output_parser.aparse_with_prompt(text, prompt_value)

    async def aclose(self) -> None:
        """Release this gateway without closing LangChain's provider clients.

        ``init_chat_model`` owns the provider client lifecycle.  In particular,
        current LangChain OpenAI and Anthropic integrations cache their default
        HTTPX transports and share them between model instances.  Closing an
        internal ``root_async_client`` here therefore closes the process-wide
        transport and breaks subsequently-created gateways.

        This adapter does not inject an HTTP client of its own, so it has no
        client resource to close.  Native gateways that construct their own
        clients continue to close those clients in their own implementations.
        """
        if self._closed:
            return
        self._closed = True


@lru_cache()
def get_langchain_llm() -> LangchainLLM:
    """Return a process-wide singleton LangChain LLM gateway."""
    logger.info("Creating LangchainLLM gateway")
    return LangchainLLM()
