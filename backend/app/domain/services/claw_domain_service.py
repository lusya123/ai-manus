import re
import asyncio
import secrets
import uuid
import logging
from datetime import datetime, timedelta, UTC
from typing import Optional, List

import httpx

from app.domain.models.claw import Claw, ClawStatus, ClawMessage, ClawAttachment
from app.domain.external.claw import ClawRuntime, ClawClient
from app.domain.repositories.claw_repository import ClawRepository
from app.domain.utils.model_output import sanitize_model_text
from app.domain.utils.error_reporting import safe_exception_summary
from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _generate_api_key() -> str:
    """Generate a secure per-user API key for LLM proxy authentication"""
    return f"manus-{secrets.token_urlsafe(32)}"


def _generate_claw_id() -> str:
    return str(uuid.uuid4())


class ClawDomainService:
    """Domain service for Claw lifecycle, history merge, and auth logic.

    This service encapsulates pure business rules that are independent of
    application-level concerns (SSE event bus, background task scheduling, etc.).
    """

    def __init__(
        self,
        claw_repository: ClawRepository,
        claw_runtime: ClawRuntime,
        claw_client: ClawClient,
    ):
        self.claw_repository = claw_repository
        self.claw_runtime = claw_runtime
        self.claw_client = claw_client
        self.settings = get_settings()

    @staticmethod
    def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
        if value and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    def _creating_timeout_seconds(self) -> int:
        runtime_timeout = getattr(self.claw_runtime, "ready_timeout", None)
        return max(30, runtime_timeout or self.settings.claw_ready_timeout)

    async def _destroy_runtime_instance(
        self, claw: Claw, action: str
    ) -> tuple[bool, Optional[str]]:
        """Destroy an owned runtime without ever discarding ownership on error.

        Older third-party runtime implementations returned ``None`` on
        success, so only an explicit ``False`` or an exception is treated as a
        failure.  First-party implementations return a boolean.
        """
        if not claw.container_name:
            return True, None
        try:
            destroyed = await self.claw_runtime.destroy(claw.container_name)
        except Exception as e:
            logger.error(
                "[claw] %s failed for instance=%s: %s",
                action,
                claw.container_name,
                safe_exception_summary(e),
            )
            return False, safe_exception_summary(e)
        if destroyed is False:
            logger.error(
                "[claw] %s returned failure for instance=%s",
                action,
                claw.container_name,
            )
            return False, "runtime reported that the instance was not destroyed"
        return True, None

    @staticmethod
    def _clear_runtime_ownership(claw: Claw) -> None:
        claw.container_name = None
        claw.container_ip = None

    async def _persist_lifecycle_error(
        self, claw: Claw, message: str
    ) -> Claw:
        claw.status = ClawStatus.ERROR
        claw.error_message = message
        return await self.claw_repository.update(claw)

    async def _mark_destroy_failure(
        self,
        claw: Claw,
        action: str,
        detail: Optional[str],
    ) -> Claw:
        instance = claw.container_name or "unknown"
        return await self._persist_lifecycle_error(
            claw,
            f"{action} failed for runtime instance {instance}: "
            f"{detail or 'unknown error'}. Runtime ownership was retained "
            "so cleanup can be retried.",
        )

    # ------------------------------------------------------------------
    # Claw CRUD / lifecycle
    # ------------------------------------------------------------------

    async def get_claw(self, user_id: str) -> Optional[Claw]:
        claw = await self.claw_repository.get_by_user_id(user_id)
        if claw and claw.status == ClawStatus.CREATING:
            updated_at = self._as_utc(claw.updated_at)
            if updated_at:
                age_seconds = (datetime.now(UTC) - updated_at).total_seconds()
                if age_seconds > self._creating_timeout_seconds() + 30:
                    destroyed, detail = await self._destroy_runtime_instance(
                        claw, "Timed-out Claw cleanup"
                    )
                    if destroyed:
                        self._clear_runtime_ownership(claw)
                        claw = await self._persist_lifecycle_error(
                            claw,
                            "Claw provisioning timed out or was interrupted. "
                            "Please retry deployment.",
                        )
                    else:
                        claw = await self._mark_destroy_failure(
                            claw, "Timed-out Claw cleanup", detail
                        )
        if claw and claw.status == ClawStatus.RUNNING:
            if self.settings.claw_ttl_seconds <= 0 and claw.expires_at:
                claw.expires_at = None
                claw = await self.claw_repository.update(claw)
            expires = self._as_utc(claw.expires_at)
            if expires and datetime.now(UTC) >= expires:
                logger.info(f"[claw] expired for user={user_id}, auto-deleting")
                destroyed, detail = await self._destroy_runtime_instance(
                    claw, "Expired Claw cleanup"
                )
                if not destroyed:
                    return await self._mark_destroy_failure(
                        claw, "Expired Claw cleanup", detail
                    )
                deleted = await self.claw_repository.delete_by_user_id(user_id)
                if deleted:
                    return None
                self._clear_runtime_ownership(claw)
                return await self._persist_lifecycle_error(
                    claw,
                    "The expired runtime was destroyed, but its database "
                    "record could not be deleted. Please retry cleanup.",
                )
            if claw.http_base_url and not await self._health_check(claw.http_base_url):
                logger.warning(f"[claw] health check failed for user={user_id}, marking stopped")
                claw.status = ClawStatus.STOPPED
                await self.claw_repository.update(claw)
        return claw

    @staticmethod
    async def _health_check(base_url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{base_url}/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def get_claw_by_api_key(self, api_key: str) -> Optional[Claw]:
        return await self.claw_repository.get_by_api_key(api_key)

    async def prepare_claw_for_creation(self, user_id: str) -> Claw:
        """Prepare a Claw record for creation (or return existing running one).

        Returns an existing healthy/in-flight instance or a newly persisted
        creating record ready for asynchronous provisioning.
        """
        if self.settings.claw_address:
            auth_provider = (self.settings.auth_provider or "").strip().lower()
            if auth_provider not in {"none", "local"}:
                raise RuntimeError(
                    "Fixed Claw runtime is restricted to single-user authentication"
                )
            fixed_user_id = (
                "anonymous" if auth_provider == "none" else "local_admin"
            )
            if user_id != fixed_user_id:
                raise RuntimeError(
                    "Fixed Claw runtime may only be owned by the configured "
                    "single-user account"
                )
        existing = await self.claw_repository.get_by_user_id(user_id)
        if existing and existing.status == ClawStatus.RUNNING:
            return existing
        if existing and existing.status == ClawStatus.CREATING:
            updated_at = self._as_utc(existing.updated_at)
            if (
                updated_at
                and (datetime.now(UTC) - updated_at).total_seconds()
                <= self._creating_timeout_seconds() + 30
            ):
                return existing

        # ERROR/STOPPED and stale CREATING records may still own a runtime.
        # Destroy it before replacing the record; otherwise resetting the
        # fields below would orphan the only container ownership pointer.
        if existing and existing.container_name:
            destroyed, detail = await self._destroy_runtime_instance(
                existing, "Previous Claw cleanup"
            )
            if not destroyed:
                existing = await self._mark_destroy_failure(
                    existing, "Previous Claw cleanup", detail
                )
                raise RuntimeError(existing.error_message)
            self._clear_runtime_ownership(existing)

        active_count = await self.claw_repository.count_by_statuses(
            [ClawStatus.CREATING, ClawStatus.RUNNING]
        )
        if (
            self.settings.claw_max_instances_total > 0
            and active_count >= self.settings.claw_max_instances_total
            and not (
                existing
                and existing.status in {ClawStatus.CREATING, ClawStatus.RUNNING}
            )
        ):
            raise RuntimeError("Claw capacity reached, please try again later")

        # A fixed development/runtime address cannot receive a newly generated
        # secret during provisioning, so its operator-provided bootstrap key is
        # persisted on the owned Claw record.  It is never accepted as a
        # process-wide bypass: verification below still requires this record to
        # be RUNNING and provisioned.
        fixed_runtime_key = (
            self.settings.claw_api_key
            if self.settings.claw_address and self.settings.claw_api_key
            else None
        )
        # Any rebuilt dynamic runtime receives a fresh capability.  The
        # plaintext is held only on this in-memory provisioning object; Mongo
        # stores its keyed digest.  A fixed runtime reuses its operator-held
        # key, but verification still resolves through the same digest lookup.
        api_key = fixed_runtime_key or _generate_api_key()
        claw_id = existing.id if existing else _generate_claw_id()

        claw = Claw(
            id=claw_id,
            user_id=user_id,
            api_key=api_key,
            status=ClawStatus.CREATING,
            last_activity_at=datetime.now(UTC),
        )
        if existing:
            claw = await self.claw_repository.update(claw)
        else:
            claw = await self.claw_repository.create(claw)
        return claw

    async def provision_claw_instance(self, claw: Claw, ttl_seconds: Optional[int] = None) -> None:
        """Provision the underlying claw runtime instance and update the record.

        Intended to be called in a background task after ``prepare_claw_for_creation``.
        """
        try:
            # Capture the start time before creating the instance so the DB
            # expiry never lags behind the container's own TTL clock (the
            # container starts counting down as soon as it boots).
            started_at = datetime.now(UTC)
            if not claw.api_key:
                raise RuntimeError("Claw runtime credential is unavailable")
            info = await self.claw_runtime.create(claw.id, claw.api_key)
            claw.container_name = info.instance_name
            claw.container_ip = info.address
            if claw.http_base_url:
                ready = await self.claw_runtime.wait_for_ready(claw.http_base_url)
                if not ready:
                    raise RuntimeError(f"Claw service not ready: {claw.http_base_url}")
            logger.info("Claw created: id=%s", claw.id)
            claw.status = ClawStatus.RUNNING
            claw.last_activity_at = datetime.now(UTC)
            if ttl_seconds and ttl_seconds > 0:
                claw.expires_at = started_at + timedelta(seconds=ttl_seconds)
            else:
                claw.expires_at = None
            await self.claw_repository.update(claw)
            await self.claw_repository.append_message(
                claw.user_id, "assistant", "i18n:Claw is ready, let's chat!",
            )
        except asyncio.CancelledError as e:
            logger.warning("Claw provisioning cancelled: id=%s", claw.id)
            instance_name = getattr(e, "claw_instance_name", None)
            if instance_name and not claw.container_name:
                claw.container_name = instance_name
            claw.status = ClawStatus.ERROR
            claw.error_message = "Claw provisioning was cancelled"
            destroyed, detail = await self._destroy_runtime_instance(
                claw, "Cancelled provisioning rollback"
            )
            if destroyed:
                self._clear_runtime_ownership(claw)
            else:
                claw.error_message = (
                    f"{claw.error_message}; rollback failed: {detail}. "
                    "Runtime ownership was retained so cleanup can be retried."
                )
            try:
                await self.claw_repository.update(claw)
            except Exception as persist_error:
                logger.error(
                    "Failed to persist cancelled claw provisioning: "
                    "id=%s error=%s",
                    claw.id,
                    safe_exception_summary(persist_error),
                )
            raise
        except Exception as e:
            logger.error(
                "Failed to create claw instance id=%s: %s",
                claw.id,
                safe_exception_summary(e),
            )
            instance_name = getattr(e, "claw_instance_name", None)
            if instance_name and not claw.container_name:
                claw.container_name = instance_name
            claw.status = ClawStatus.ERROR
            claw.error_message = "Claw provisioning failed. Please retry."
            destroyed, detail = await self._destroy_runtime_instance(
                claw, "Failed provisioning rollback"
            )
            if destroyed:
                self._clear_runtime_ownership(claw)
            else:
                claw.error_message = (
                    f"{claw.error_message}; rollback failed: {detail}. "
                    "Runtime ownership was retained so cleanup can be retried."
                )
            try:
                await self.claw_repository.update(claw)
            except Exception as persist_error:
                logger.error(
                    "Failed to persist claw provisioning error: "
                    "id=%s error=%s",
                    claw.id,
                    safe_exception_summary(persist_error),
                )

    async def delete_claw(self, user_id: str) -> bool:
        """Delete the claw record from MongoDB and destroy its runtime instance.

        The record is deleted only after the runtime confirms destruction.  On
        failure it is retained in ERROR state with its instance ownership
        fields intact so a later delete can safely retry cleanup.
        """
        claw = await self.claw_repository.get_by_user_id(user_id)
        if not claw:
            return False
        # Dynamic runtimes are owned resources and must always be destroyed
        # before their durable ownership record can be deleted.  The opt-out is
        # meaningful only for a fixed externally managed runtime, which has no
        # ``container_name`` ownership pointer.
        destroy_runtime = bool(claw.container_name) or bool(
            self.settings.claw_destroy_on_delete
        )
        if destroy_runtime:
            destroyed, detail = await self._destroy_runtime_instance(
                claw, "Claw deletion"
            )
            if not destroyed:
                await self._mark_destroy_failure(claw, "Claw deletion", detail)
                return False
        deleted = await self.claw_repository.delete_by_user_id(user_id)
        if not deleted and destroy_runtime:
            self._clear_runtime_ownership(claw)
            await self._persist_lifecycle_error(
                claw,
                "The runtime was destroyed, but its database record could "
                "not be deleted. Please retry deletion.",
            )
        return deleted

    async def cleanup_instances(self) -> dict[str, int]:
        """Clean interrupted instances and records matching retention policy."""
        now = datetime.now(UTC)
        removed = 0
        errored = 0
        claws = await self.claw_repository.list_by_statuses(
            [
                ClawStatus.CREATING,
                ClawStatus.RUNNING,
                ClawStatus.ERROR,
                ClawStatus.STOPPED,
            ]
        )

        for claw in claws:
            if claw.status in {ClawStatus.ERROR, ClawStatus.STOPPED}:
                if not claw.container_name:
                    continue
                destroyed, detail = await self._destroy_runtime_instance(
                    claw, f"{claw.status.value.title()} Claw ownership cleanup"
                )
                if not destroyed:
                    await self._mark_destroy_failure(
                        claw,
                        f"{claw.status.value.title()} Claw ownership cleanup",
                        detail,
                    )
                    errored += 1
                    continue
                self._clear_runtime_ownership(claw)
                await self.claw_repository.update(claw)
                continue

            if claw.status == ClawStatus.CREATING:
                updated_at = self._as_utc(claw.updated_at)
                if (
                    updated_at
                    and (now - updated_at).total_seconds()
                    > self._creating_timeout_seconds() + 30
                ):
                    destroyed, detail = await self._destroy_runtime_instance(
                        claw, "Timed-out Claw cleanup"
                    )
                    if destroyed:
                        self._clear_runtime_ownership(claw)
                        await self._persist_lifecycle_error(
                            claw,
                            "Claw provisioning timed out or was interrupted.",
                        )
                    else:
                        await self._mark_destroy_failure(
                            claw, "Timed-out Claw cleanup", detail
                        )
                    errored += 1
                continue

            if self.settings.claw_ttl_seconds <= 0 and claw.expires_at:
                claw.expires_at = None
                await self.claw_repository.update(claw)
            expires_at = self._as_utc(claw.expires_at)
            expired = bool(expires_at and now >= expires_at)
            idle = False
            if self.settings.claw_idle_timeout_seconds > 0:
                last_activity_at = self._as_utc(
                    claw.last_activity_at or claw.updated_at
                )
                idle = bool(
                    last_activity_at
                    and (now - last_activity_at).total_seconds()
                    > self.settings.claw_idle_timeout_seconds
                )

            if expired or idle:
                reason = "expired" if expired else "idle"
                logger.info(
                    "[claw-cleanup] removing %s claw id=%s user=%s",
                    reason,
                    claw.id,
                    claw.user_id,
                )
                destroyed, detail = await self._destroy_runtime_instance(
                    claw, f"{reason.title()} Claw cleanup"
                )
                if not destroyed:
                    await self._mark_destroy_failure(
                        claw, f"{reason.title()} Claw cleanup", detail
                    )
                    errored += 1
                    continue
                deleted = await self.claw_repository.delete_by_user_id(
                    claw.user_id
                )
                if deleted:
                    removed += 1
                else:
                    self._clear_runtime_ownership(claw)
                    await self._persist_lifecycle_error(
                        claw,
                        "The runtime was destroyed, but its database record "
                        "could not be deleted. Please retry cleanup.",
                    )
                    errored += 1

        return {"removed": removed, "errored": errored}

    # ------------------------------------------------------------------
    # History merge
    # ------------------------------------------------------------------

    async def get_history(self, user_id: str) -> List[ClawMessage]:
        """Merge MongoDB messages with OpenClaw's native session history."""
        db_msgs = []
        for message in await self.claw_repository.get_messages(user_id):
            sanitized = self._sanitize_message(message)
            if sanitized.role == "attachments" or sanitized.content:
                db_msgs.append(sanitized)

        claw_msgs: List[ClawMessage] = []
        try:
            claw = await self.claw_repository.get_by_user_id(user_id)
            if claw and claw.http_base_url and claw.status == ClawStatus.RUNNING:
                claw_msgs = await self.claw_client.get_history(
                    claw.http_base_url, "default", 200,
                )
        except Exception as e:
            logger.warning(
                "[claw-history] failed to fetch native history: %s",
                safe_exception_summary(e),
            )

        if not claw_msgs:
            return db_msgs

        return self._merge_histories(db_msgs, claw_msgs)

    @staticmethod
    def _normalize_ts(ts: int) -> int:
        """Normalize timestamp to seconds (Claw uses ms, MongoDB uses seconds)."""
        if ts > 1_000_000_000_000:
            return ts // 1000
        return ts

    @staticmethod
    def _strip_openclaw_prefix(text: str) -> str:
        """Strip OpenClaw's timestamp prefix like '[Sat 2026-03-21 11:11 UTC] '."""
        return re.sub(r'^\[.*?\]\s*', '', text)

    @classmethod
    def _normalize_content(cls, text: str, sanitize_reasoning: bool = True) -> str:
        """Normalize message text for dedup comparison."""
        text = cls._strip_openclaw_prefix(text)
        text = re.sub(r'<MANUS_FILE\b[^>]*/>', '', text)
        if sanitize_reasoning:
            text = sanitize_model_text(text)
        return text.strip()

    @classmethod
    def _sanitize_message(cls, message: ClawMessage) -> ClawMessage:
        if message.role != "assistant":
            return message
        content = cls._normalize_content(message.content or "")
        return message.model_copy(update={"content": content})

    @classmethod
    def _merge_histories(
        cls, db_msgs: List[ClawMessage], claw_msgs: List[ClawMessage],
    ) -> List[ClawMessage]:
        """Merge two message lists, dedup, return sorted by timestamp.

        DB messages are authoritative; Claw messages fill gaps.
        Uses (role, timestamp proximity, content prefix) for cross-source dedup
        so that identical messages sent at different times are kept distinct.
        Attachment messages are deduped by file_id.
        """

        TS_WINDOW = 5  # seconds tolerance between DB and Claw timestamps

        seen_file_ids: set[str] = set()
        for m in db_msgs:
            if m.role == "attachments" and m.attachments:
                for att in m.attachments:
                    if att.file_id:
                        seen_file_ids.add(att.file_id)

        db_fingerprints: list[tuple[str, int, str, bool]] = []
        for m in db_msgs:
            if m.role != "attachments":
                norm = cls._normalize_content(
                    m.content or "",
                    sanitize_reasoning=m.role == "assistant",
                )
                db_fingerprints.append((m.role, m.timestamp or 0, norm[:120], False))

        merged: List[ClawMessage] = list(db_msgs)

        for m in claw_msgs:
            ts = cls._normalize_ts(m.timestamp or 0)
            content = cls._normalize_content(
                m.content or "",
                sanitize_reasoning=m.role == "assistant",
            )

            if m.attachments:
                new_atts = [a for a in m.attachments if a.file_id and a.file_id not in seen_file_ids]
                if new_atts:
                    for a in new_atts:
                        seen_file_ids.add(a.file_id)
                    att_role = "user" if m.role == "user" else "assistant"
                    merged.append(ClawMessage(
                        role="attachments", content=att_role,
                        timestamp=ts, attachments=new_atts,
                    ))

            if not content:
                continue

            prefix = content[:120]
            matched = False
            for idx, (fp_role, fp_ts, fp_prefix, fp_used) in enumerate(db_fingerprints):
                if fp_used:
                    continue
                if fp_role != m.role:
                    continue
                if abs(fp_ts - ts) > TS_WINDOW:
                    continue
                if fp_prefix == prefix:
                    db_fingerprints[idx] = (fp_role, fp_ts, fp_prefix, True)
                    matched = True
                    break

            if not matched:
                merged.append(ClawMessage(
                    role=m.role, content=content, timestamp=ts,
                ))

        merged.sort(key=lambda m: m.timestamp or 0)
        return merged

    # ------------------------------------------------------------------
    # Chat processing (core streaming logic)
    # ------------------------------------------------------------------

    async def process_chat_stream(
        self, user_id: str, base_url: str, message: str, session_id: str,
    ):
        """Stream chat from the claw client, persisting messages.

        Yields raw chunk dicts from the claw client. The caller is responsible
        for broadcasting chunks to SSE / WebSocket consumers.
        """
        assistant_content: list[str] = []
        file_attachments: list[ClawAttachment] = []

        try:
            async for chunk in self.claw_client.chat_stream(base_url, message, session_id):
                if chunk.get("type") == "text" and chunk.get("content"):
                    assistant_content.append(chunk["content"])

                if chunk.get("type") == "file" and chunk.get("file_id"):
                    file_attachments.append(ClawAttachment(
                        file_id=chunk["file_id"],
                        filename=chunk.get("filename", chunk["file_id"]),
                        content_type=chunk.get("content_type"),
                        size=chunk.get("size", 0),
                        file_url=chunk.get("file_url"),
                    ))

                yield chunk

        finally:
            if file_attachments:
                await self.claw_repository.append_message(
                    user_id, "attachments", "assistant", attachments=file_attachments,
                )
            if assistant_content:
                content = sanitize_model_text("".join(assistant_content))
                if content:
                    await self.claw_repository.append_message(
                        user_id, "assistant", content,
                    )

    async def validate_claw_for_chat(self, user_id: str) -> Claw:
        """Validate that a user has a running claw instance ready for chat.

        Returns the Claw instance or raises ValueError.
        """
        claw = await self.claw_repository.get_by_user_id(user_id)
        if not claw or not claw.http_base_url:
            raise ValueError("No running claw instance found")
        if claw.status != ClawStatus.RUNNING:
            raise ValueError(f"Claw is not running (status: {claw.status})")
        return claw

    # ------------------------------------------------------------------
    # File proxy
    # ------------------------------------------------------------------

    async def get_file(self, user_id: str, filename: str) -> tuple[bytes, str]:
        claw = await self.claw_repository.get_by_user_id(user_id)
        if not claw or not claw.http_base_url:
            raise ValueError("No running claw instance found")
        if claw.status != ClawStatus.RUNNING:
            raise ValueError(f"Claw is not running (status: {claw.status})")
        return await self.claw_client.get_file(claw.http_base_url, filename)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def verify_api_key(self, api_key: str) -> Optional[str]:
        """Return the owner of an active, provisioned Claw API key.

        The proxy credential is a runtime capability, not a general user API
        token.  Fail closed when Claw is disabled, the record is not RUNNING,
        the runtime address is absent, or the instance lease has expired.
        """
        if not self.settings.claw_enabled or not api_key:
            return None
        fixed_user_id: Optional[str] = None
        if self.settings.claw_address:
            auth_provider = (self.settings.auth_provider or "").strip().lower()
            if auth_provider not in {"none", "local"}:
                # A fixed runtime has one bootstrap key and cannot safely map
                # the same capability to multiple tenant records.
                return None
            fixed_user_id = (
                "anonymous" if auth_provider == "none" else "local_admin"
            )
        claw = await self.get_claw_by_api_key(api_key)
        if (
            not claw
            or (fixed_user_id is not None and claw.user_id != fixed_user_id)
            or claw.status != ClawStatus.RUNNING
            or not claw.http_base_url
        ):
            return None

        expires_at = self._as_utc(claw.expires_at)
        if expires_at and self.settings.claw_ttl_seconds <= 0:
            # A deployment may disable TTL after older records already gained
            # expiry timestamps.  Reconcile the record without denying the
            # first legitimate runtime request.
            claw.expires_at = None
            claw = await self.claw_repository.update(claw)
        elif expires_at and datetime.now(UTC) >= expires_at:
            # Reuse the lifecycle cleanup path, but never authorize the
            # request that discovered an expired capability.
            await self.get_claw(claw.user_id)
            return None
        return claw.user_id
