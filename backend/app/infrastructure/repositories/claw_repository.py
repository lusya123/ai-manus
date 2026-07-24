from typing import Optional, List
from datetime import datetime, UTC
from bson import BSON
from pymongo import ReturnDocument
from app.infrastructure.models.documents import ClawDocument
from app.domain.models.claw import Claw, ClawMessage, ClawAttachment, ClawStatus
from app.domain.repositories.claw_repository import ClawWriteConflictError
from app.domain.utils.claw_credentials import (
    claw_api_key_digest,
    claw_api_key_hmac_secrets,
)
from app.core.config import get_settings
import logging

logger = logging.getLogger(__name__)


# `$bsonSize` measures the embedded message document itself.  Charge a small
# conservative amount for its surrounding array key/type/terminator as well;
# the configured history ceiling already reserves a further 4 MiB of document
# headroom below MongoDB's hard 16 MiB limit.
_HISTORY_ARRAY_ENTRY_OVERHEAD_BYTES = 32


class ClawRepository:
    """MongoDB repository for Claw instances"""

    @staticmethod
    def _api_key_digests(api_key: str) -> tuple[str, ...]:
        return tuple(
            claw_api_key_digest(api_key, secret)
            for secret in claw_api_key_hmac_secrets(get_settings())
        )

    @classmethod
    def _api_key_digest(cls, api_key: str) -> str:
        return cls._api_key_digests(api_key)[0]

    async def _migrate_legacy_api_key(
        self, doc: ClawDocument
    ) -> ClawDocument:
        """Replace a legacy plaintext key with its keyed digest in place."""

        legacy_key = doc.api_key
        if not legacy_key:
            return doc
        digest = self._api_key_digest(legacy_key)
        await doc.update({
            "$set": {"api_key_digest": digest},
            "$unset": {"api_key": ""},
        })
        doc.api_key_digest = digest
        doc.api_key = None
        return doc

    @staticmethod
    def _with_ephemeral_key(claw: Claw, api_key: Optional[str]) -> Claw:
        claw.api_key = api_key
        return claw

    async def get_by_user_id(self, user_id: str) -> Optional[Claw]:
        """Get claw instance by user ID"""
        doc = await ClawDocument.find_one({"user_id": user_id})
        if not doc:
            return None
        doc = await self._migrate_legacy_api_key(doc)
        return doc.to_domain()

    async def get_by_id(self, claw_id: str) -> Optional[Claw]:
        """Get claw instance by claw ID"""
        doc = await ClawDocument.find_one({"claw_id": claw_id})
        if not doc:
            return None
        doc = await self._migrate_legacy_api_key(doc)
        return doc.to_domain()

    async def get_by_api_key(self, api_key: str) -> Optional[Claw]:
        """Get claw instance by API key"""
        digests = self._api_key_digests(api_key)
        digest = digests[0]
        doc = None
        matched_digest = None
        for candidate in digests:
            doc = await ClawDocument.find_one({"api_key_digest": candidate})
            if doc:
                matched_digest = candidate
                break
        if not doc:
            # One-release lazy migration path for records written before
            # keyed-HMAC storage.  The matching plaintext is immediately
            # removed and is never returned to the domain layer.
            doc = await ClawDocument.find_one({"api_key": api_key})
        if not doc:
            return None
        doc = await self._migrate_legacy_api_key(doc)
        if matched_digest and matched_digest != digest:
            # The presented plaintext proves possession, so a match under a
            # previous server key can be safely rewritten under the current
            # key without ever storing the plaintext capability.
            await doc.update({
                "$set": {"api_key_digest": digest},
                "$unset": {"api_key": ""},
            })
            doc.api_key_digest = digest
        if doc.api_key_digest != digest:
            return None
        return doc.to_domain()

    async def create(self, claw: Claw) -> Claw:
        """Create a new claw instance"""
        doc = ClawDocument.from_domain(claw)
        await doc.insert()
        return self._with_ephemeral_key(doc.to_domain(), claw.api_key)

    async def update(self, claw: Claw) -> Claw:
        """Atomically update one exact lifecycle revision."""
        update_fields = claw.model_dump(
            exclude={"created_at", "revision"}
        )
        update_fields.pop("id", None)
        update_fields["updated_at"] = datetime.now(UTC)
        if claw.api_key:
            update_fields["api_key_digest"] = self._api_key_digest(claw.api_key)
        revision_filter: dict = {"revision": claw.revision}
        if claw.revision == 0:
            revision_filter = {
                "$or": [
                    {"revision": 0},
                    {"revision": {"$exists": False}},
                ]
            }
        raw = await ClawDocument.get_pymongo_collection().find_one_and_update(
            {"$and": [
                {"claw_id": claw.id},
                revision_filter,
            ]},
            {
                "$set": update_fields,
                "$unset": {"api_key": ""},
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if raw is None:
            exists = await ClawDocument.find_one({"claw_id": claw.id})
            if not exists:
                raise ValueError(f"Claw not found: {claw.id}")
            raise ClawWriteConflictError(
                f"Claw lifecycle changed concurrently: {claw.id}"
            )
        refreshed = ClawDocument.model_validate(raw)
        refreshed = await self._migrate_legacy_api_key(refreshed)
        result_claw = self._with_ephemeral_key(
            refreshed.to_domain(), claw.api_key
        )
        # Callers often perform several lifecycle transitions on the same
        # aggregate instance. Advance their token after every successful CAS.
        claw.revision = result_claw.revision
        claw.updated_at = result_claw.updated_at
        return result_claw

    async def claim_runtime_destroy(self, claw: Claw) -> Optional[Claw]:
        """Atomically fence provider deletion for one exact generation."""

        revision_filter: dict = {"revision": claw.revision}
        if claw.revision == 0:
            revision_filter = {
                "$or": [
                    {"revision": 0},
                    {"revision": {"$exists": False}},
                ]
            }
        container_filter: dict = {"container_name": claw.container_name}
        if claw.container_name is None:
            container_filter = {
                "$or": [
                    {"container_name": None},
                    {"container_name": {"$exists": False}},
                ]
            }
        raw = await ClawDocument.get_pymongo_collection().find_one_and_update(
            {"$and": [
                {"claw_id": claw.id},
                {"user_id": claw.user_id},
                revision_filter,
                container_filter,
            ]},
            {
                "$set": {
                    "status": ClawStatus.DESTROYING,
                    "updated_at": datetime.now(UTC),
                },
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if raw is None:
            return None
        claimed = ClawDocument.model_validate(raw)
        claimed = await self._migrate_legacy_api_key(claimed)
        return claimed.to_domain()

    async def count_by_statuses(self, statuses: List[ClawStatus]) -> int:
        values = [status.value for status in statuses]
        return await ClawDocument.find({"status": {"$in": values}}).count()

    async def list_by_statuses(self, statuses: List[ClawStatus]) -> List[Claw]:
        values = [status.value for status in statuses]
        docs = await ClawDocument.find({"status": {"$in": values}}).to_list()
        migrated = [await self._migrate_legacy_api_key(doc) for doc in docs]
        return [doc.to_domain() for doc in migrated]

    async def delete_by_user_id(self, user_id: str) -> bool:
        """Delete claw instance by user ID"""
        doc = await ClawDocument.find_one({"user_id": user_id})
        if not doc:
            return False
        await doc.delete()
        return True

    async def delete_if_matches(self, claw: Claw) -> bool:
        """Delete only the exact aggregate revision/runtime generation."""

        def nullable(field: str, value: Optional[str]) -> dict:
            if value is not None:
                return {field: value}
            return {
                "$or": [
                    {field: None},
                    {field: {"$exists": False}},
                ]
            }

        revision_filter: dict = {"revision": claw.revision}
        if claw.revision == 0:
            revision_filter = {
                "$or": [
                    {"revision": 0},
                    {"revision": {"$exists": False}},
                ]
            }
        result = await ClawDocument.get_pymongo_collection().delete_one(
            {"$and": [
                {"claw_id": claw.id},
                {"user_id": claw.user_id},
                revision_filter,
                nullable("container_name", claw.container_name),
            ]}
        )
        return bool(result.deleted_count)

    async def get_messages(self, user_id: str) -> List[ClawMessage]:
        """Get chat message history for a user's claw"""
        doc = await ClawDocument.find_one({"user_id": user_id})
        if not doc:
            return []
        await self._migrate_legacy_api_key(doc)
        return doc.messages

    async def append_message(
        self, user_id: str, role: str, content: str = "",
        attachments: Optional[List[ClawAttachment]] = None,
    ) -> None:
        """Atomically append and retain the newest byte/count-bounded tail."""
        msg = ClawMessage(
            role=role,
            content=content,
            timestamp=int(datetime.now(UTC).timestamp()),
            attachments=attachments,
        )
        message_document = msg.model_dump()
        now = datetime.now(UTC)
        settings = get_settings()
        max_messages = max(1, min(128, int(settings.claw_history_max_messages)))
        max_bytes = int(settings.claw_history_max_bytes)
        message_bytes = (
            len(BSON.encode(message_document))
            + _HISTORY_ARRAY_ENTRY_OVERHEAD_BYTES
        )
        if message_bytes > max_bytes:
            raise ValueError(
                "Claw history message exceeds CLAW_HISTORY_MAX_BYTES"
            )

        # Use an update pipeline rather than read/modify/write or a follow-up
        # trim.  `$reduce` walks newest-to-oldest, stops at the first record
        # that would exceed the aggregate byte budget, then reverses the kept
        # tail back into chronological order.  `$literal` prevents content
        # beginning with `$` from being interpreted as an aggregation field.
        candidates = {
            "$slice": [
                {
                    "$concatArrays": [
                        {"$ifNull": ["$messages", []]},
                        {"$literal": [message_document]},
                    ]
                },
                -max_messages,
            ]
        }
        retained = {
            "$reduce": {
                "input": {"$reverseArray": "$$candidates"},
                "initialValue": {
                    "messages": [],
                    "bytes": 0,
                    "full": False,
                },
                "in": {
                    "$let": {
                        "vars": {
                            "message_bytes": {
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
                                                        "$$message_bytes",
                                                    ]
                                                },
                                                max_bytes,
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "messages": "$$value.messages",
                                    "bytes": "$$value.bytes",
                                    "full": True,
                                },
                                {
                                    "messages": {
                                        "$concatArrays": [
                                            "$$value.messages",
                                            ["$$this"],
                                        ]
                                    },
                                    "bytes": {
                                        "$add": [
                                            "$$value.bytes",
                                            "$$message_bytes",
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
                "messages": {
                    "$let": {
                        "vars": {"candidates": candidates},
                        "in": {
                            "$let": {
                                "vars": {"retained": retained},
                                "in": {
                                    "$reverseArray": "$$retained.messages"
                                },
                            }
                        },
                    }
                },
                "updated_at": now,
                "last_activity_at": now,
                "revision": {
                    "$add": [{"$ifNull": ["$revision", 0]}, 1]
                },
            }
        }]
        result = await ClawDocument.get_pymongo_collection().update_one(
            {
                "user_id": user_id,
                "status": ClawStatus.RUNNING.value,
            },
            pipeline,
        )
        if getattr(result, "matched_count", int(bool(result))) != 1:
            raise ClawWriteConflictError(
                "Claw is no longer running; message was not appended"
            )

    async def clear_messages(self, user_id: str) -> None:
        """Clear all chat messages for a user's claw"""
        result = await ClawDocument.find_one({
            "user_id": user_id,
            "status": ClawStatus.RUNNING.value,
        }).update({
            "$set": {
                "messages": [],
                "updated_at": datetime.now(UTC),
            },
            "$inc": {"revision": 1},
        })
        if getattr(result, "matched_count", int(bool(result))) != 1:
            raise ClawWriteConflictError(
                "Claw is no longer running; history was not cleared"
            )
