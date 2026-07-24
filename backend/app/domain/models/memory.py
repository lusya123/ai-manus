import json
import logging
from typing import List, Optional

from pydantic import BaseModel, Field

from app.domain.models.message import LLMMessage, Role

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN = 4

# Keep an ordinary ToolResult-shaped JSON payload so infrastructure adapters
# and future model calls can still consume the compacted message.
_ELIDED_CONTENT = json.dumps(
    {"success": True, "message": "(result elided to save context)", "data": None}
)


def estimate_tokens(text: str) -> int:
    """Return a cheap, dependency-free token estimate."""
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


class Memory(BaseModel):
    """Append-only agent memory with token-aware tool-result compaction."""

    messages: List[LLMMessage] = Field(default_factory=list)

    def add_message(self, message: LLMMessage) -> None:
        """Add message to memory"""
        self.messages.append(message)
    
    def add_messages(self, messages: List[LLMMessage]) -> None:
        """Add messages to memory"""
        self.messages.extend(messages)

    def get_messages(self) -> List[LLMMessage]:
        """Get all message history"""
        return self.messages
    
    def get_last_message(self) -> Optional[LLMMessage]:
        """Get the last message"""
        if len(self.messages) > 0:  
            return self.messages[-1]
        return None
    
    def roll_back(self) -> None:
        """Roll back memory"""
        self.messages = self.messages[:-1]

    def estimate_tokens(self) -> int:
        """Estimate content and tool-call argument tokens in memory."""
        total = 0
        for message in self.messages:
            total += estimate_tokens(message.content)
            for tool_call in message.tool_calls:
                total += estimate_tokens(tool_call.name)
                total += estimate_tokens(json.dumps(tool_call.args, default=str))
        return total

    def compact(self, max_tokens: int = 0, keep_recent: int = 10) -> None:
        """Elide old tool results without breaking tool-call pairing.

        ``max_tokens=0`` compacts every eligible old result. The newest
        ``keep_recent`` messages are never modified so the model retains its
        immediate working context.
        """
        if max_tokens and self.estimate_tokens() <= max_tokens:
            return

        cutoff = max(0, len(self.messages) - keep_recent)
        for message in self.messages[:cutoff]:
            if message.role != Role.TOOL or message.content == _ELIDED_CONTENT:
                continue
            message.content = _ELIDED_CONTENT
            logger.debug("Elided old tool result from memory: %s", message.name)
            if max_tokens and self.estimate_tokens() <= max_tokens:
                return

    @property
    def empty(self) -> bool:
        """Check if memory is empty"""
        return len(self.messages) == 0
