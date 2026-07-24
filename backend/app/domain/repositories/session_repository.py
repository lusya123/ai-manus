from typing import Optional, Protocol, List
from datetime import datetime
from app.domain.models.session import Session, SessionStatus, SessionSummary
from app.domain.models.file import FileInfo
from app.domain.models.event import BaseEvent

class SessionRepository(Protocol):
    """Repository interface for Session aggregate"""
    
    async def save(self, session: Session) -> None:
        """Save or update a session"""
        ...

    async def update_runtime_ownership(
        self,
        session_id: str,
        sandbox_id: Optional[str],
        task_id: Optional[str],
        sandbox_provider: Optional[str] = None,
        task_sandbox_id: Optional[str] = None,
    ) -> None:
        """Update runtime IDs only when the session still exists.

        Unlike ``save``, this operation must never recreate a concurrently
        deleted session.
        """
        ...

    async def compare_and_set_runtime_ownership(
        self,
        session_id: str,
        expected_sandbox_id: Optional[str],
        expected_task_id: Optional[str],
        expected_task_sandbox_id: Optional[str],
        expected_sandbox_provider: Optional[str],
        sandbox_id: Optional[str],
        task_id: Optional[str],
        sandbox_provider: Optional[str] = None,
        task_sandbox_id: Optional[str] = None,
    ) -> bool:
        """Replace ownership only if the full durable projection matches."""
        ...

    async def claim_runtime_destroy(
        self,
        session_id: str,
        expected_sandbox_id: Optional[str],
        expected_task_id: Optional[str],
        expected_task_sandbox_id: Optional[str],
        expected_sandbox_provider: Optional[str],
    ) -> bool:
        """Fence provider deletion for one exact runtime/task projection."""
        ...

    async def publish_runtime_destroy_claim(
        self,
        session_id: str,
        expected_task_id: Optional[str],
        expected_task_sandbox_id: Optional[str],
        expected_sandbox_provider: Optional[str],
        sandbox_id: str,
        sandbox_provider: str,
    ) -> bool:
        """Publish an absent runtime pointer directly as non-adoptable."""
        ...

    async def finish_runtime_destroy(
        self,
        session_id: str,
        expected_sandbox_id: Optional[str],
        expected_task_id: Optional[str],
        expected_task_sandbox_id: Optional[str],
        expected_sandbox_provider: Optional[str],
        sandbox_id: Optional[str],
        task_id: Optional[str],
        sandbox_provider: Optional[str],
        task_sandbox_id: Optional[str],
    ) -> bool:
        """Clear an exact claimed runtime projection after provider deletion."""
        ...

    async def claim_session_delete(
        self, session_id: str, user_id: str
    ) -> bool:
        """Durably fence a session against new work before cleanup."""
        ...

    async def delete_claimed(
        self, session_id: str, user_id: str
    ) -> bool:
        """Delete only a session carrying the durable deletion claim."""
        ...
    
    async def find_by_id(self, session_id: str) -> Optional[Session]:
        """Find a session by its ID"""
        ...
    
    async def find_by_user_id(self, user_id: str) -> List[Session]:
        """Find all sessions for a specific user"""
        ...
    
    async def find_summaries_by_user_id(self, user_id: str) -> List[SessionSummary]:
        """Find lightweight session summaries for a user (excludes events/files)"""
        ...
    
    async def find_by_id_and_user_id(self, session_id: str, user_id: str) -> Optional[Session]:
        """Find a session by ID and user ID (for authorization)"""
        ...
    
    async def update_title(self, session_id: str, title: str) -> None:
        """Update the title of a session"""
        ...

    async def update_latest_message(self, session_id: str, message: str, timestamp: datetime) -> None:
        """Update the latest message of a session"""
        ...

    async def add_event(self, session_id: str, event: BaseEvent) -> BaseEvent:
        """Add a bounded event to a session."""
        ...

    async def add_event_once(self, session_id: str, event: BaseEvent) -> BaseEvent:
        """Atomically append a stable event ID at most once."""
        ...

    async def update_event_transport_cursor(
        self, session_id: str, event_id: str, transport_id: str
    ) -> None:
        """Attach a Redis output cursor without changing the logical event ID."""
        ...
    
    async def add_file(self, session_id: str, file_info: FileInfo) -> None:
        """Add a file to a session"""
        ...

    async def upsert_file_by_path(
        self,
        session_id: str,
        file_info: FileInfo,
    ) -> Optional[FileInfo]:
        """Atomically replace by path and return the previous reference."""
        ...
    
    async def remove_file(self, session_id: str, file_id: str) -> None:
        """Remove a file from a session"""
        ...

    async def get_file_by_path(self, session_id: str, file_path: str) -> Optional[FileInfo]:
        """Get file by path from a session"""
        ...

    async def count_file_references(self, file_id: str) -> int:
        """Count live session or durable-outbox references to a stored file."""
        ...

    async def update_status(self, session_id: str, status: SessionStatus) -> None:
        """Update the status of a session"""
        ...
    
    async def update_unread_message_count(self, session_id: str, count: int) -> None:
        """Update the unread message count of a session"""
        ...
    
    async def increment_unread_message_count(self, session_id: str) -> None:
        """Increment the unread message count of a session"""
        ...
    
    async def decrement_unread_message_count(self, session_id: str) -> None:
        """Decrement the unread message count of a session"""
        ...
    
    async def update_shared_status(self, session_id: str, is_shared: bool) -> str:
        """Update shared status, rotate its capability epoch, and return it."""
        ...
    
    async def delete(self, session_id: str) -> None:
        """Delete a session"""
        ...
    
    async def get_all(self) -> List[Session]:
        """Get all sessions"""
        ...
