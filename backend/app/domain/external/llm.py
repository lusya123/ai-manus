from typing import Any, Dict, List, Optional, Protocol, TYPE_CHECKING
from app.domain.models.message import LLMMessage

if TYPE_CHECKING:
    from app.domain.models.agent import Agent


class LLM(Protocol):
    """LLM gateway interface.

    Abstracts the underlying model framework (LangChain, raw SDK, …) away from
    the domain. Implementations live in ``infrastructure/external/llm`` and are
    responsible for translating :class:`LLMMessage` to/from framework types,
    tool binding, JSON repair and retries.
    """

    async def ask(
        self,
        messages: List[LLMMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[str] = None,
        tool_choice: Optional[str] = None,
    ) -> LLMMessage:
        """Send a chat request and return the assistant message.

        Args:
            messages: Full conversation context as domain messages.
            tools: Optional OpenAI-style function schemas for tool calling.
            response_format: Optional response format hint (e.g. ``json_object``).
            tool_choice: Optional tool choice directive (e.g. ``none``).

        Returns:
            The assistant :class:`LLMMessage`, with any tool calls parsed.
        """
        ...

    async def parse_json(self, text: str) -> Dict[str, Any]:
        """Extract/repair a JSON object from raw model output."""
        ...


class LLMFactory(Protocol):
    """Build an LLM gateway for one persisted agent configuration.

    The factory is deliberately part of the domain boundary: task runners may
    execute in the API process or in a Celery worker, but both must reconstruct
    the same per-session model from the persisted :class:`Agent` aggregate.
    """

    def create(self, agent: Optional["Agent"] = None) -> LLM:
        """Create a gateway using *agent* overrides or system defaults."""
        ...
