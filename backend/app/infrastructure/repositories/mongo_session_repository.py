from typing import Optional, List
from datetime import datetime, UTC
from bson import BSON
from pymongo import ReturnDocument
from app.domain.models.session import Session, SessionStatus, SessionSummary
from app.domain.models.file import FileInfo
from app.domain.repositories.session_repository import SessionRepository
from app.domain.models.event import BaseEvent, ErrorEvent
from app.infrastructure.models.documents import (
    SessionDocument,
    TurnOutputEventDocument,
)
from app.core.config import get_settings
import logging
import uuid

logger = logging.getLogger(__name__)

# `$bsonSize` measures an embedded event document, but not the surrounding
# array element key/type/terminator. Charge a conservative fixed amount for
# that framing. The configured aggregate ceiling separately leaves at least
# 10 MiB of headroom for Session.files and the rest of the Mongo document.
_HISTORY_ARRAY_ENTRY_OVERHEAD_BYTES = 32

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
        """Create a blank aggregate and reject stale whole-aggregate updates.

        Embedded events and files have dedicated atomic repository methods.
        Lifecycle, sharing, status, title, and counters likewise have explicit
        mutation methods. Re-saving an existing snapshot could otherwise undo
        an irreversible delete fence, revive stale runtime ownership, roll back
        a share epoch, or bypass the embedded-collection bounds.
        """
        mongo_session = await SessionDocument.find_one(
            SessionDocument.session_id == session.id
        )
        
        if not mongo_session:
            if session.events or session.files:
                raise ValueError(
                    "Create Session without embedded events/files and append "
                    "them through their bounded repository methods"
                )
            mongo_session = SessionDocument.from_domain(session)
            await mongo_session.save()
            return

        raise RuntimeError(
            "Existing Session aggregates require an explicit atomic "
            "repository mutation"
        )

    async def update_runtime_ownership(
        self,
        session_id: str,
        sandbox_id: Optional[str],
        task_id: Optional[str],
        sandbox_provider: Optional[str] = None,
        task_sandbox_id: Optional[str] = None,
    ) -> None:
        """Persist sandbox/task ownership without upserting deleted sessions."""
        result = await SessionDocument.get_pymongo_collection().update_one(
            {
                "session_id": session_id,
                "sandbox_destroying": {"$ne": True},
                "deleting": {"$ne": True},
            },
            {"$set": {
                "sandbox_id": sandbox_id,
                "sandbox_provider": sandbox_provider,
                "task_id": task_id,
                "task_sandbox_id": task_sandbox_id,
                "updated_at": datetime.now(UTC),
            }}
        )
        if not result.matched_count:
            raise ValueError(
                f"Session {session_id} not found or runtime deletion is claimed"
            )

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
        """CAS the exact sandbox generation/task without stale overwrite."""

        def expected(field: str, value: Optional[str]) -> dict:
            if value is not None:
                return {field: value}
            return {
                "$or": [
                    {field: None},
                    {field: {"$exists": False}},
                ]
            }

        result = await SessionDocument.get_pymongo_collection().update_one(
            {"$and": [
                {"session_id": session_id},
                expected("sandbox_id", expected_sandbox_id),
                expected("task_id", expected_task_id),
                expected("task_sandbox_id", expected_task_sandbox_id),
                expected("sandbox_provider", expected_sandbox_provider),
                {"sandbox_destroying": {"$ne": True}},
                {"deleting": {"$ne": True}},
            ]},
            {"$set": {
                "sandbox_id": sandbox_id,
                "sandbox_provider": sandbox_provider,
                "sandbox_destroying": False,
                "task_id": task_id,
                "task_sandbox_id": task_sandbox_id,
                "updated_at": datetime.now(UTC),
            }},
        )
        return bool(result.matched_count)

    async def claim_session_delete(
        self, session_id: str, user_id: str
    ) -> bool:
        """Persist an irreversible delete intent before resource cleanup."""

        result = await SessionDocument.get_pymongo_collection().update_one(
            {
                "session_id": session_id,
                "user_id": user_id,
                "deleting": {"$ne": True},
            },
            {"$set": {
                "deleting": True,
                "updated_at": datetime.now(UTC),
            }},
        )
        return bool(result.matched_count)

    async def delete_claimed(
        self, session_id: str, user_id: str
    ) -> bool:
        """Conditionally delete only an irreversibly claimed session."""

        result = await SessionDocument.get_pymongo_collection().delete_one({
            "session_id": session_id,
            "user_id": user_id,
            "deleting": True,
        })
        return bool(result.deleted_count)

    async def claim_runtime_destroy(
        self,
        session_id: str,
        expected_sandbox_id: Optional[str],
        expected_task_id: Optional[str],
        expected_task_sandbox_id: Optional[str],
        expected_sandbox_provider: Optional[str],
    ) -> bool:
        """Atomically mark one exact session runtime as non-adoptable."""

        def expected(field: str, value: Optional[str]) -> dict:
            if value is not None:
                return {field: value}
            return {"$or": [
                {field: None},
                {field: {"$exists": False}},
            ]}

        result = await SessionDocument.get_pymongo_collection().update_one(
            {"$and": [
                {"session_id": session_id},
                expected("sandbox_id", expected_sandbox_id),
                expected("task_id", expected_task_id),
                expected("task_sandbox_id", expected_task_sandbox_id),
                expected("sandbox_provider", expected_sandbox_provider),
                {"sandbox_destroying": {"$ne": True}},
            ]},
            {"$set": {
                "sandbox_destroying": True,
                "updated_at": datetime.now(UTC),
            }},
        )
        return bool(result.matched_count)

    async def publish_runtime_destroy_claim(
        self,
        session_id: str,
        expected_task_id: Optional[str],
        expected_task_sandbox_id: Optional[str],
        expected_sandbox_provider: Optional[str],
        sandbox_id: str,
        sandbox_provider: str,
    ) -> bool:
        """Publish an untracked generation only as a deletion tombstone."""

        def expected(field: str, value: Optional[str]) -> dict:
            if value is not None:
                return {field: value}
            return {"$or": [
                {field: None},
                {field: {"$exists": False}},
            ]}

        result = await SessionDocument.get_pymongo_collection().update_one(
            {"$and": [
                {"session_id": session_id},
                expected("sandbox_id", None),
                expected("task_id", expected_task_id),
                expected("task_sandbox_id", expected_task_sandbox_id),
                expected("sandbox_provider", expected_sandbox_provider),
                {"sandbox_destroying": {"$ne": True}},
                {"deleting": {"$ne": True}},
            ]},
            {"$set": {
                "sandbox_id": sandbox_id,
                "sandbox_provider": sandbox_provider,
                "sandbox_destroying": True,
                "updated_at": datetime.now(UTC),
            }},
        )
        return bool(result.matched_count)

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
        """Commit cleanup only for the exact projection claimed beforehand."""

        def expected(field: str, value: Optional[str]) -> dict:
            if value is not None:
                return {field: value}
            return {"$or": [
                {field: None},
                {field: {"$exists": False}},
            ]}

        result = await SessionDocument.get_pymongo_collection().update_one(
            {"$and": [
                {"session_id": session_id},
                expected("sandbox_id", expected_sandbox_id),
                expected("task_id", expected_task_id),
                expected("task_sandbox_id", expected_task_sandbox_id),
                expected("sandbox_provider", expected_sandbox_provider),
                {"sandbox_destroying": True},
            ]},
            {"$set": {
                "sandbox_id": sandbox_id,
                "sandbox_provider": sandbox_provider,
                "sandbox_destroying": False,
                "task_id": task_id,
                "task_sandbox_id": task_sandbox_id,
                "updated_at": datetime.now(UTC),
            }},
        )
        return bool(result.matched_count)


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
        """Atomically append one ID and retain the newest count/byte-safe tail."""
        bounded = self._bounded_event(event)
        settings = get_settings()
        event_limit = max(
            1,
            min(512, int(settings.session_history_max_events)),
        )
        max_bytes = int(settings.session_history_max_bytes)
        event_document = bounded.model_dump()
        event_bytes = (
            len(BSON.encode(event_document))
            + _HISTORY_ARRAY_ENTRY_OVERHEAD_BYTES
        )
        if event_bytes > max_bytes:
            # Startup validation normally makes this impossible. Keep this
            # repository boundary fail-closed for tests, migrations, and any
            # caller that constructs settings without running validate().
            raise ValueError(
                "Session event exceeds SESSION_HISTORY_MAX_BYTES"
            )

        # `$slice` first applies the count cap. `$reduce` then walks that tail
        # newest-to-oldest and stops at the first record that would exceed the
        # BSON budget, preserving a contiguous chronological tail. The incoming
        # event is wrapped in `$literal`: message/tool/file fields beginning
        # with `$` are user/model data, never aggregation expressions.
        candidates = {
            "$slice": [
                {
                    "$concatArrays": [
                        {"$ifNull": ["$events", []]},
                        {"$literal": [event_document]},
                    ]
                },
                -event_limit,
            ]
        }
        retained = {
            "$reduce": {
                "input": {"$reverseArray": "$$candidates"},
                "initialValue": {
                    "events": [],
                    "bytes": 0,
                    "full": False,
                },
                "in": {
                    "$let": {
                        "vars": {
                            "event_bytes": {
                                "$add": [
                                    {"$bsonSize": "$$this"},
                                    _HISTORY_ARRAY_ENTRY_OVERHEAD_BYTES,
                                ]
                            }
                        },
                        "in": {
                            "$cond": [
                                {
                                    "$or": [
                                        "$$value.full",
                                        {
                                            "$gt": [
                                                {
                                                    "$add": [
                                                        "$$value.bytes",
                                                        "$$event_bytes",
                                                    ]
                                                },
                                                max_bytes,
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "events": "$$value.events",
                                    "bytes": "$$value.bytes",
                                    "full": True,
                                },
                                {
                                    "events": {
                                        "$concatArrays": [
                                            "$$value.events",
                                            ["$$this"],
                                        ]
                                    },
                                    "bytes": {
                                        "$add": [
                                            "$$value.bytes",
                                            "$$event_bytes",
                                        ]
                                    },
                                    "full": False,
                                },
                            ]
                        },
                    }
                },
            }
        }
        pipeline = [{
            "$set": {
                "events": {
                    "$let": {
                        "vars": {"candidates": candidates},
                        "in": {
                            "$let": {
                                "vars": {"retained": retained},
                                "in": {
                                    "$reverseArray": "$$retained.events"
                                },
                            }
                        },
                    }
                },
                "updated_at": datetime.now(UTC),
            }
        }]
        collection = SessionDocument.get_pymongo_collection()
        result = await collection.update_one(
            {
                "session_id": session_id,
                "events": {"$not": {"$elemMatch": {"id": bounded.id}}},
            },
            pipeline,
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
        """Idempotently add one canonical file ID to a session.

        A user may attach the same GridFS object to many turns. Counting those
        references repeatedly would bypass the storage file-count quota and
        eventually fill the embedded Session document even though no new file
        exists. The predicate makes concurrent/retried publication atomic.
        """
        if not file_info.file_id:
            raise ValueError("A canonical file ID is required")
        collection = SessionDocument.get_pymongo_collection()
        result = await collection.update_one(
            {
                "session_id": session_id,
                "files": {
                    "$not": {
                        "$elemMatch": {"file_id": file_info.file_id}
                    }
                },
            },
            {
                "$push": {"files": file_info.model_dump()},
                "$set": {"updated_at": datetime.now(UTC)},
            },
        )
        if not result.matched_count:
            exists = await collection.find_one(
                {"session_id": session_id},
                {"_id": 1},
            )
            if not exists:
                raise ValueError(f"Session {session_id} not found")

    async def upsert_file_by_path(
        self,
        session_id: str,
        file_info: FileInfo,
    ) -> Optional[FileInfo]:
        """Atomically retire the current path owner and append its successor.

        Historical events authorize attachments through their immutable file ID.
        Dropping the prior ``FileInfo`` here would therefore make an attachment
        disappear from an already-shared event.  Keep the canonical prior entry,
        but clear its path so path lookups identify only the latest version.
        """
        if not file_info.file_path:
            raise ValueError("A file path is required for path upsert")
        if not file_info.file_id:
            raise ValueError("A canonical file ID is required")
        collection = SessionDocument.get_pymongo_collection()
        previous_document = None
        # A same-ID retry that observes the committed successor is successful
        # and returns no replacement. If that ID was concurrently removed
        # between the conditional update and reconciliation read, retry once
        # rather than silently claiming a missing publication.
        for attempt in range(2):
            previous_document = await collection.find_one_and_update(
                {
                    "session_id": session_id,
                    "files": {
                        "$not": {
                            "$elemMatch": {"file_id": file_info.file_id}
                        }
                    },
                },
                [
                    {
                        "$set": {
                            "files": {
                                "$concatArrays": [
                                    {
                                        "$map": {
                                            "input": {"$ifNull": ["$files", []]},
                                            "as": "file",
                                            "in": {
                                                "$cond": [
                                                    {
                                                        "$eq": [
                                                            "$$file.file_path",
                                                            {
                                                                "$literal": (
                                                                    file_info.file_path
                                                                )
                                                            },
                                                        ]
                                                    },
                                                    {
                                                        "$mergeObjects": [
                                                            "$$file",
                                                            {"file_path": None},
                                                        ]
                                                    },
                                                    "$$file",
                                                ]
                                            },
                                        }
                                    },
                                    [{"$literal": file_info.model_dump()}],
                                ]
                            },
                            "updated_at": datetime.now(UTC),
                        }
                    }
                ],
                projection={"files": 1},
                return_document=ReturnDocument.BEFORE,
            )
            if previous_document is not None:
                break
            current = await collection.find_one(
                {"session_id": session_id},
                {"_id": 1, "files": 1},
            )
            if current is None:
                raise ValueError(f"Session {session_id} not found")
            if any(
                item.get("file_id") == file_info.file_id
                for item in current.get("files", [])
            ):
                return None
            if attempt:
                raise RuntimeError(
                    "Session file projection changed during path publication"
                )

        assert previous_document is not None
        for previous_file in previous_document.get("files", []):
            if (
                previous_file.get("file_path") == file_info.file_path
                and previous_file.get("file_id") != file_info.file_id
            ):
                return FileInfo.model_validate(previous_file)
        return None
    
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

    async def count_file_references(self, file_id: str) -> int:
        """Check both the bounded session view and durable replay outbox."""
        session_references = await (
            SessionDocument.get_pymongo_collection().count_documents(
                {
                    "$or": [
                        {"files.file_id": file_id},
                        {"events.attachments.file_id": file_id},
                    ]
                },
                limit=1,
            )
        )
        if session_references:
            return 1
        outbox_references = await (
            TurnOutputEventDocument.get_pymongo_collection().count_documents(
                {"event.attachments.file_id": file_id},
                limit=1,
            )
        )
        return 1 if outbox_references else 0

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
