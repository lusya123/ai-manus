from typing import Optional, List
from datetime import datetime, UTC
from app.domain.models.session import Session, SessionStatus, SessionSummary
from app.domain.models.file import FileInfo
from app.domain.repositories.session_repository import SessionRepository
from app.domain.models.event import BaseEvent, ErrorEvent
from app.infrastructure.models.documents import SessionDocument
from app.core.config import get_settings
import logging
import uuid

logger = logging.getLogger(__name__)

SESSION_LIST_PROJECTION = {
    "session_id": 1,
    "user_id": 1,
    "title": 1,
    "unread_message_count": 1,
    "latest_message": 1,
    "latest_message_at": 1,
    "status": 1,
    "is_shared": 1,
}

class MongoSessionRepository(SessionRepository):
    """MongoDB implementation of SessionRepository"""

    async def _to_domain_with_share_epoch(
        self, mongo_session: SessionDocument
    ) -> Session:
        """Atomically backfill the public capability epoch on legacy records."""

        if not mongo_session.share_epoch:
            candidate = uuid.uuid4().hex
            collection = SessionDocument.get_pymongo_collection()
            result = await collection.update_one(
                {
                    "_id": mongo_session.id,
                    "$or": [
                        {"share_epoch": {"$exists": False}},
                        {"share_epoch": None},
                        {"share_epoch": ""},
                    ],
                },
                {"$set": {"share_epoch": candidate}},
            )
            if result.modified_count:
                mongo_session.share_epoch = candidate
            else:
                persisted = await collection.find_one(
                    {"_id": mongo_session.id}, {"share_epoch": 1}
                )
                mongo_session.share_epoch = (
                    (persisted or {}).get("share_epoch") or candidate
                )
        return mongo_session.to_domain()
    
    async def save(self, session: Session) -> None:
        """Save or update a session"""
        mongo_session = await SessionDocument.find_one(
            SessionDocument.session_id == session.id
        )
        
        if not mongo_session:
            mongo_session = SessionDocument.from_domain(session)
            await mongo_session.save()
            return
        
        # Update fields from session domain model
        mongo_session.update_from_domain(session)
        await mongo_session.save()

    async def update_runtime_ownership(
        self,
        session_id: str,
        sandbox_id: Optional[str],
        task_id: Optional[str],
        sandbox_provider: Optional[str] = None,
    ) -> None:
        """Persist sandbox/task ownership without upserting deleted sessions."""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$set": {
                "sandbox_id": sandbox_id,
                "sandbox_provider": sandbox_provider,
                "task_id": task_id,
                "updated_at": datetime.now(UTC),
            }}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")


    async def find_by_id(self, session_id: str) -> Optional[Session]:
        """Find a session by its ID"""
        mongo_session = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        )
        return (
            await self._to_domain_with_share_epoch(mongo_session)
            if mongo_session
            else None
        )
    
    async def find_by_user_id(self, user_id: str) -> List[Session]:
        """Find all sessions for a specific user"""
        mongo_sessions = await SessionDocument.find(
            SessionDocument.user_id == user_id
        ).sort("-latest_message_at").to_list()
        return [
            await self._to_domain_with_share_epoch(mongo_session)
            for mongo_session in mongo_sessions
        ]

    async def find_summaries_by_user_id(self, user_id: str) -> List[SessionSummary]:
        """Find lightweight session summaries for a user (excludes events/files)"""
        collection = SessionDocument.get_pymongo_collection()
        cursor = collection.find(
            {"user_id": user_id},
            SESSION_LIST_PROJECTION,
        ).sort("latest_message_at", -1)
        summaries = []
        async for doc in cursor:
            summaries.append(SessionSummary(
                id=doc["session_id"],
                user_id=doc["user_id"],
                title=doc.get("title"),
                unread_message_count=doc.get("unread_message_count", 0),
                latest_message=doc.get("latest_message"),
                latest_message_at=doc.get("latest_message_at"),
                status=doc.get("status", SessionStatus.PENDING),
                is_shared=doc.get("is_shared", False),
            ))
        return summaries
    
    async def find_by_id_and_user_id(self, session_id: str, user_id: str) -> Optional[Session]:
        """Find a session by ID and user ID (for authorization)"""
        mongo_session = await SessionDocument.find_one(
            SessionDocument.session_id == session_id,
            SessionDocument.user_id == user_id
        )
        return (
            await self._to_domain_with_share_epoch(mongo_session)
            if mongo_session
            else None
        )
    
    async def update_title(self, session_id: str, title: str) -> None:
        """Update the title of a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$set": {"title": title, "updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")

    async def update_latest_message(self, session_id: str, message: str, timestamp: datetime) -> None:
        """Update the latest message of a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$set": {"latest_message": message, "latest_message_at": timestamp, "updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")

    @staticmethod
    def _bounded_event(event: BaseEvent) -> BaseEvent:
        """Replace an oversized event with a safe, stable summary."""
        max_bytes = max(1024, int(get_settings().session_event_max_bytes))
        if len(event.model_dump_json().encode("utf-8")) <= max_bytes:
            return event
        return ErrorEvent(
            id=event.id,
            turn_id=event.turn_id,
            timestamp=event.timestamp,
            error=(
                "Event details were omitted because the serialized event "
                f"exceeded the {max_bytes}-byte persistence limit"
            ),
        )

    async def add_event(self, session_id: str, event: BaseEvent) -> BaseEvent:
        """Append through the idempotent, atomically bounded event path."""
        return await self.add_event_once(session_id, event)

    async def add_event_once(self, session_id: str, event: BaseEvent) -> BaseEvent:
        """Append one stable event ID and retain only the configured tail."""
        bounded = self._bounded_event(event)
        event_limit = max(1, int(get_settings().session_history_max_events))
        collection = SessionDocument.get_pymongo_collection()
        now = datetime.now(UTC)
        result = await collection.update_one(
            {
                "session_id": session_id,
                "events": {"$not": {"$elemMatch": {"id": bounded.id}}},
            },
            {
                "$push": {
                    "events": {
                        "$each": [bounded.model_dump()],
                        "$slice": -event_limit,
                    }
                },
                "$set": {"updated_at": now},
            },
        )
        if not result.matched_count:
            exists = await collection.find_one(
                {"session_id": session_id}, {"_id": 1}
            )
            if not exists:
                raise ValueError(f"Session {session_id} not found")
        return bounded

    async def update_event_transport_cursor(
        self, session_id: str, event_id: str, transport_id: str
    ) -> None:
        """Persist a Redis cursor on a retained event without changing its ID."""
        await SessionDocument.get_pymongo_collection().update_one(
            {"session_id": session_id},
            {
                "$set": {
                    "events.$[event].transport_id": transport_id,
                    "updated_at": datetime.now(UTC),
                }
            },
            array_filters=[{"event.id": event_id}],
        )
    
    async def add_file(self, session_id: str, file_info: FileInfo) -> None:
        """Add a file to a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$push": {"files": file_info.model_dump()}, "$set": {"updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")
    
    async def remove_file(self, session_id: str, file_id: str) -> None:
        """Remove a file from a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$pull": {"files": {"file_id": file_id}}, "$set": {"updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")

    async def get_file_by_path(self, session_id: str, file_path: str) -> Optional[FileInfo]:
        """Get file by path from a session"""
        mongo_session = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        )
        if not mongo_session:
            raise ValueError(f"Session {session_id} not found")
        
        # Search for file with matching path
        for file_info in mongo_session.files:
            if file_info.file_path == file_path:
                return file_info
        return None

    async def delete(self, session_id: str) -> None:
        """Delete a session"""
        mongo_session = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        )
        if mongo_session:
            await mongo_session.delete()

    async def get_all(self) -> List[Session]:
        """Get all sessions"""
        mongo_sessions = await SessionDocument.find().sort("-latest_message_at").to_list()
        return [
            await self._to_domain_with_share_epoch(mongo_session)
            for mongo_session in mongo_sessions
        ]
    
    async def update_status(self, session_id: str, status: SessionStatus) -> None:
        """Update the status of a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$set": {"status": status, "updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")

    async def update_unread_message_count(self, session_id: str, count: int) -> None:
        """Update the unread message count of a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$set": {"unread_message_count": count, "updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")

    async def increment_unread_message_count(self, session_id: str) -> None:
        """Atomically increment the unread message count of a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$inc": {"unread_message_count": 1}, "$set": {"updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")

    async def decrement_unread_message_count(self, session_id: str) -> None:
        """Atomically decrement the unread message count of a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$inc": {"unread_message_count": -1}, "$set": {"updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")

    async def update_shared_status(self, session_id: str, is_shared: bool) -> str:
        """Update shared status and revoke every previously issued share URL."""
        share_epoch = uuid.uuid4().hex
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$set": {
                "is_shared": is_shared,
                "share_epoch": share_epoch,
                "updated_at": datetime.now(UTC),
            }}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")
        return share_epoch
