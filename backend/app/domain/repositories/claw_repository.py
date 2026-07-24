from typing import Optional, List, Protocol

from app.domain.models.claw import Claw, ClawMessage, ClawAttachment, ClawStatus


class ClawWriteConflictError(RuntimeError):
    """A stale Claw lifecycle snapshot lost an optimistic write race."""


class ClawRepository(Protocol):
    """Repository interface for Claw aggregate"""

    async def get_by_user_id(self, user_id: str) -> Optional[Claw]:
        """Get claw instance by user ID"""
        ...

    async def get_by_id(self, claw_id: str) -> Optional[Claw]:
        """Get claw instance by claw ID"""
        ...

    async def get_by_api_key(self, api_key: str) -> Optional[Claw]:
        """Get claw instance by API key"""
        ...

    async def create(self, claw: Claw) -> Claw:
        """Create a new claw instance"""
        ...

    async def update(self, claw: Claw) -> Claw:
        """Update an existing claw instance"""
        ...

    async def claim_runtime_destroy(self, claw: Claw) -> Optional[Claw]:
        """Fence provider deletion by atomically claiming the exact revision."""
        ...

    async def count_by_statuses(self, statuses: List[ClawStatus]) -> int:
        """Count claw instances in any of the supplied statuses."""
        ...

    async def list_by_statuses(self, statuses: List[ClawStatus]) -> List[Claw]:
        """List claw instances in any of the supplied statuses."""
        ...

    async def delete_by_user_id(self, user_id: str) -> bool:
        """Delete claw instance by user ID"""
        ...

    async def delete_if_matches(self, claw: Claw) -> bool:
        """Delete only the exact revision/runtime generation in ``claw``."""
        ...

    async def get_messages(self, user_id: str) -> List[ClawMessage]:
        """Get chat message history for a user's claw"""
        ...

    async def append_message(
        self, user_id: str, role: str, content: str = "",
        attachments: Optional[List[ClawAttachment]] = None,
    ) -> None:
        """Append a message to the claw's chat history"""
        ...

    async def clear_messages(self, user_id: str) -> None:
        """Clear all chat messages for a user's claw"""
        ...
