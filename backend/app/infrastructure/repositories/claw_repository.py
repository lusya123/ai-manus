from typing import Optional, List
from datetime import datetime, UTC
from app.infrastructure.models.documents import ClawDocument
from app.domain.models.claw import Claw, ClawMessage, ClawAttachment, ClawStatus
from app.domain.utils.claw_credentials import (
    claw_api_key_digest,
    claw_api_key_hmac_secrets,
)
from app.core.config import get_settings
import logging

logger = logging.getLogger(__name__)


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
        """Update an existing claw instance"""
        doc = await ClawDocument.find_one({"claw_id": claw.id})
        if not doc:
            raise ValueError(f"Claw not found: {claw.id}")
        update_fields = claw.model_dump(exclude={"created_at"})
        update_fields.pop("id", None)
        update_fields["updated_at"] = datetime.now(UTC)
        update_doc = {"$set": update_fields, "$unset": {"api_key": ""}}
        if claw.api_key:
            update_fields["api_key_digest"] = self._api_key_digest(claw.api_key)
        await doc.update(update_doc)
        refreshed = await ClawDocument.find_one({"claw_id": claw.id})
        if not refreshed:
            raise ValueError(f"Claw not found: {claw.id}")
        refreshed = await self._migrate_legacy_api_key(refreshed)
        return self._with_ephemeral_key(refreshed.to_domain(), claw.api_key)

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
        """Append a message to the claw's chat history"""
        msg = ClawMessage(
            role=role,
            content=content,
            timestamp=int(datetime.now(UTC).timestamp()),
            attachments=attachments,
        )
        now = datetime.now(UTC)
        max_messages = max(
            1,
            min(128, int(get_settings().claw_history_max_messages)),
        )
        await ClawDocument.find_one({"user_id": user_id}).update({
            "$push": {
                "messages": {
                    "$each": [msg.model_dump()],
                    "$slice": -max_messages,
                }
            },
            "$set": {
                "updated_at": now,
                "last_activity_at": now,
            },
        })

    async def clear_messages(self, user_id: str) -> None:
        """Clear all chat messages for a user's claw"""
        await ClawDocument.find_one({"user_id": user_id}).update({
            "$set": {
                "messages": [],
                "updated_at": datetime.now(UTC),
            }
        })
