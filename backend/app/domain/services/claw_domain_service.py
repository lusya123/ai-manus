import re
import asyncio
import secrets
import uuid
import logging
from datetime import datetime, timedelta, UTC
from typing import Optional, List

import httpx

from app.domain.models.claw import Claw, ClawStatus, ClawMessage, ClawAttachment
from app.domain.external.claw import (
    ClawClient,
    ClawResponseTooLargeError,
    ClawRuntime,
)
from app.domain.external.coordination import current_lifecycle_task_lost_lease
from app.domain.repositories.claw_repository import (
    ClawRepository,
    ClawWriteConflictError,
)
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
    application-level concerns (event bus, background task scheduling, etc.).
    """

    _OWNERSHIP_RECONCILE_INTERVAL_SECONDS = 1.0
    _OWNERSHIP_RECONCILE_MAX_ATTEMPTS = 3

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

    @staticmethod
    async def _await_task_to_known_outcome(task: asyncio.Task) -> object:
        """Let lifecycle I/O finish despite repeated caller cancellation."""

        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    break
                continue
        return task.result()

    async def _reconcile_failed_provisioning(
        self,
        claw: Claw,
        *,
        base_error_message: str,
        rollback_action: str,
    ) -> None:
        """Persist deterministic ownership before exact-name rollback."""

        async def state_machine() -> None:
            claw.status = ClawStatus.ERROR
            claw.error_message = base_error_message
            ownership_persisted = False
            try:
                # Persist the only cleanup pointer before attempting rollback.
                # If destroy is indeterminate, this record makes retry durable.
                await self.claw_repository.update(claw)
                ownership_persisted = True
            except Exception as persist_error:
                logger.error(
                    "Failed to persist Claw cleanup ownership before rollback: "
                    "id=%s error=%s",
                    claw.id,
                    safe_exception_summary(persist_error),
                )

            destroyed, detail = await self._destroy_runtime_instance(
                claw, rollback_action
            )
            if destroyed:
                self._clear_runtime_ownership(claw)
            else:
                claw.error_message = (
                    f"{claw.error_message}; rollback failed: {detail}. "
                    "Runtime ownership was retained so cleanup can be retried."
                )

            if destroyed or ownership_persisted:
                try:
                    # If deletion is exact, a failed final write leaves only a
                    # harmless stale pointer. If the first write succeeded, the
                    # cleanup pointer is already durable even if this richer
                    # error update fails.
                    await self.claw_repository.update(claw)
                except Exception as persist_error:
                    logger.error(
                        "Failed to persist final Claw provisioning state: "
                        "id=%s error=%s",
                        claw.id,
                        safe_exception_summary(persist_error),
                    )
                return

            # First-party dynamic runtimes pre-publish their deterministic
            # name in ``prepare_claw_for_creation``. Keep foreground recovery
            # bounded: if Mongo remains unavailable, that existing CREATING
            # record is the durable pointer consumed by cleanup_instances on a
            # later pass.
            for attempt in range(self._OWNERSHIP_RECONCILE_MAX_ATTEMPTS):
                try:
                    await self.claw_repository.update(claw)
                    return
                except Exception as persist_error:
                    logger.error(
                        "Retrying unpersisted Claw cleanup ownership: "
                        "id=%s error=%s",
                        claw.id,
                        safe_exception_summary(persist_error),
                    )

                destroyed, _ = await self._destroy_runtime_instance(
                    claw, rollback_action
                )
                if destroyed:
                    self._clear_runtime_ownership(claw)
                    try:
                        await self.claw_repository.update(claw)
                    except Exception as persist_error:
                        logger.error(
                            "Provider cleanup succeeded but final Claw state "
                            "could not be persisted: id=%s error=%s",
                            claw.id,
                            safe_exception_summary(persist_error),
                        )
                    return
                if attempt + 1 < self._OWNERSHIP_RECONCILE_MAX_ATTEMPTS:
                    await asyncio.sleep(
                        self._OWNERSHIP_RECONCILE_INTERVAL_SECONDS
                    )

            claw.error_message = (
                f"{claw.error_message}; cleanup reconciliation was deferred "
                "after the bounded foreground retry budget."
            )
            logger.error(
                "Deferred Claw ownership reconciliation to a later durable "
                "cleanup pass: id=%s instance=%s",
                claw.id,
                claw.container_name,
            )

        state_task = asyncio.create_task(state_machine())
        await self._await_task_to_known_outcome(state_task)

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
        claim_destroy = getattr(
            self.claw_repository, "claim_runtime_destroy", None
        )
        if callable(claim_destroy):
            claimed = await claim_destroy(claw)
            if claimed is None:
                raise ClawWriteConflictError(
                    f"Claw runtime destroy claim lost: {claw.id}"
                )
            # Keep the caller's aggregate and ephemeral credential, but advance
            # its fencing token/status to the atomic pre-delete claim.
            claw.revision = claimed.revision
            claw.status = claimed.status
            claw.updated_at = claimed.updated_at
        try:
            destroy_owned = getattr(self.claw_runtime, "destroy_owned", None)
            if callable(destroy_owned):
                destroyed = await destroy_owned(claw.container_name, claw.id)
            else:
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

    async def _delete_if_current(self, claw: Claw) -> bool:
        """Delete only the lifecycle revision inspected by this caller."""

        conditional_delete = getattr(
            self.claw_repository, "delete_if_matches", None
        )
        if callable(conditional_delete):
            return await conditional_delete(claw)
        # Compatibility for lightweight/custom repositories. Production Mongo
        # always exposes the conditional form above.
        return await self.claw_repository.delete_by_user_id(claw.user_id)

    async def _refresh_runtime_address(self, claw: Claw) -> Claw:
        """Reconnect this API replica to an owned Docker runtime on demand."""

        resolver = getattr(self.claw_runtime, "resolve_owned", None)
        if not callable(resolver) or not claw.container_name:
            return claw
        address = await resolver(claw.container_name, claw.id)
        # Non-isolated/external implementations retain their persisted address.
        if address is None:
            return claw
        if not address:
            raise RuntimeError("Owned Claw runtime has no reachable address")
        if address == claw.container_ip:
            return claw
        claw.container_ip = address
        try:
            return await self.claw_repository.update(claw)
        except Exception as exc:
            # The newly inspected address is valid for this request. Keep the
            # durable refresh best-effort; the next request repeats exact owner
            # and network verification if Mongo was temporarily unavailable.
            logger.warning(
                "Could not persist refreshed Claw address: %s",
                safe_exception_summary(exc),
            )
            return claw

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
                    try:
                        destroyed, detail = await self._destroy_runtime_instance(
                            claw, "Timed-out Claw cleanup"
                        )
                    except ClawWriteConflictError:
                        return await self.claw_repository.get_by_user_id(user_id)
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
                try:
                    claw = await self.claw_repository.update(claw)
                except ClawWriteConflictError:
                    return await self.claw_repository.get_by_user_id(user_id)
            expires = self._as_utc(claw.expires_at)
            if expires and datetime.now(UTC) >= expires:
                logger.info(f"[claw] expired for user={user_id}, auto-deleting")
                try:
                    destroyed, detail = await self._destroy_runtime_instance(
                        claw, "Expired Claw cleanup"
                    )
                except ClawWriteConflictError:
                    return await self.claw_repository.get_by_user_id(user_id)
                if not destroyed:
                    return await self._mark_destroy_failure(
                        claw, "Expired Claw cleanup", detail
                    )
                deleted = await self._delete_if_current(claw)
                if deleted:
                    return None
                # A false CAS means a newer lifecycle revision now owns the
                # record. Never overwrite it with this stale cleanup snapshot.
                return await self.claw_repository.get_by_user_id(user_id)
            # Legacy/fake records without any runtime pointer retain their old
            # behavior. Owned or addressable runtimes must resolve and pass an
            # actual health check; a missing/stale address cannot be trusted.
            if claw.container_name or claw.http_base_url:
                healthy = False
                try:
                    claw = await self._refresh_runtime_address(claw)
                    healthy = bool(
                        claw.http_base_url
                        and await self._health_check(claw.http_base_url)
                    )
                except Exception as exc:
                    logger.warning(
                        "[claw] runtime address refresh failed for user=%s: %s",
                        user_id,
                        safe_exception_summary(exc),
                    )
                if not healthy:
                    logger.warning(f"[claw] health check failed for user={user_id}, marking stopped")
                    claw.status = ClawStatus.STOPPED
                    try:
                        claw = await self.claw_repository.update(claw)
                    except ClawWriteConflictError:
                        return await self.claw_repository.get_by_user_id(user_id)
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
            try:
                destroyed, detail = await self._destroy_runtime_instance(
                    existing, "Previous Claw cleanup"
                )
            except ClawWriteConflictError as exc:
                raise RuntimeError(
                    "Claw lifecycle changed concurrently; please retry"
                ) from exc
            if not destroyed:
                existing = await self._mark_destroy_failure(
                    existing, "Previous Claw cleanup", detail
                )
                raise RuntimeError(existing.error_message)
            self._clear_runtime_ownership(existing)

        active_count = await self.claw_repository.count_by_statuses(
            [
                ClawStatus.CREATING,
                ClawStatus.RUNNING,
                ClawStatus.DESTROYING,
            ]
        )
        if (
            self.settings.claw_max_instances_total > 0
            and active_count >= self.settings.claw_max_instances_total
            and not (
                existing
                and existing.status in {
                    ClawStatus.CREATING,
                    ClawStatus.RUNNING,
                    ClawStatus.DESTROYING,
                }
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
            revision=existing.revision if existing else 0,
            last_activity_at=datetime.now(UTC),
        )
        planned_id = getattr(self.claw_runtime, "plan_owned_id", None)
        if not callable(planned_id):
            planned_id = getattr(self.claw_runtime, "planned_id", None)
        if callable(planned_id):
            # Persist the exact Docker cleanup authority before the background
            # task can enter docker-py. A timed-out thread can then finish late
            # without creating an unowned container.
            claw.container_name = planned_id(claw.id)
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
            if not claw.container_name:
                planned_id = getattr(
                    self.claw_runtime,
                    "plan_owned_id",
                    None,
                )
                if not callable(planned_id):
                    planned_id = getattr(
                        self.claw_runtime,
                        "planned_id",
                        None,
                    )
                if callable(planned_id):
                    # Robustness for direct/internal callers that did not use
                    # prepare_claw_for_creation: persist before Docker mutation.
                    claw.container_name = planned_id(claw.id)
                    await self.claw_repository.update(claw)
            create_owned = getattr(self.claw_runtime, "create_owned", None)
            if callable(create_owned) and claw.container_name:
                info = await create_owned(
                    claw.id,
                    claw.api_key,
                    claw.container_name,
                )
            else:
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
            if current_lifecycle_task_lost_lease():
                # The exact generation is already durable. Another replica
                # may have acquired the lifecycle lease and adopted it, so a
                # fenced owner must never perform provider-side rollback.
                raise
            instance_name = getattr(e, "claw_instance_name", None)
            if instance_name and not claw.container_name:
                claw.container_name = instance_name
            try:
                await self._reconcile_failed_provisioning(
                    claw,
                    base_error_message="Claw provisioning was cancelled",
                    rollback_action="Cancelled provisioning rollback",
                )
            except BaseException as persist_error:
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
            try:
                await self._reconcile_failed_provisioning(
                    claw,
                    base_error_message=(
                        "Claw provisioning failed. Please retry."
                    ),
                    rollback_action="Failed provisioning rollback",
                )
            except BaseException as persist_error:
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
            try:
                destroyed, detail = await self._destroy_runtime_instance(
                    claw, "Claw deletion"
                )
            except ClawWriteConflictError:
                return False
            if not destroyed:
                await self._mark_destroy_failure(claw, "Claw deletion", detail)
                return False
        return await self._delete_if_current(claw)

    async def cleanup_instances(self) -> dict[str, int]:
        """Clean interrupted instances and records matching retention policy."""
        now = datetime.now(UTC)
        removed = 0
        errored = 0
        claws = await self.claw_repository.list_by_statuses(
            [
                ClawStatus.CREATING,
                ClawStatus.RUNNING,
                ClawStatus.DESTROYING,
                ClawStatus.ERROR,
                ClawStatus.STOPPED,
            ]
        )

        for claw in claws:
            try:
                if claw.status in {
                    ClawStatus.DESTROYING,
                    ClawStatus.ERROR,
                    ClawStatus.STOPPED,
                }:
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
                    claw = await self.claw_repository.update(claw)
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
                    if await self._delete_if_current(claw):
                        removed += 1
            except ClawWriteConflictError:
                # A newer lifecycle revision won the race. Its generation is
                # authoritative; this stale maintenance snapshot is discarded.
                logger.info(
                    "[claw-cleanup] skipped stale lifecycle snapshot id=%s",
                    claw.id,
                )

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
                claw = await self._refresh_runtime_address(claw)
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
        for broadcasting chunks to WebSocket consumers.
        """
        assistant_content: list[str] = []
        assistant_content_bytes = 0
        response_too_large = False
        response_completed = False
        file_attachments: list[ClawAttachment] = []

        try:
            async for chunk in self.claw_client.chat_stream(base_url, message, session_id):
                if chunk.get("type") == "text" and chunk.get("content"):
                    content = chunk["content"]
                    if not isinstance(content, str):
                        raise TypeError("Claw text chunks must contain strings")
                    assistant_content_bytes += len(content.encode("utf-8"))
                    if assistant_content_bytes > max(
                        1, int(self.settings.claw_chat_max_response_bytes)
                    ):
                        response_too_large = True
                        raise ClawResponseTooLargeError(
                            "Claw response exceeded the configured size limit"
                        )
                    assistant_content.append(content)

                if chunk.get("type") == "file" and chunk.get("file_id"):
                    file_attachments.append(ClawAttachment(
                        file_id=chunk["file_id"],
                        filename=chunk.get("filename", chunk["file_id"]),
                        content_type=chunk.get("content_type"),
                        size=chunk.get("size", 0),
                        file_url=chunk.get("file_url"),
                    ))

                yield chunk

            response_completed = True

        except ClawResponseTooLargeError:
            response_too_large = True
            raise

        finally:
            if file_attachments:
                await self.claw_repository.append_message(
                    user_id, "attachments", "assistant", attachments=file_attachments,
                )
            # An over-limit answer is deliberately not stored as a truncated
            # assistant message.  The application layer emits an explicit
            # error followed by the one terminal done event for this turn.
            if (
                assistant_content
                and response_completed
                and not response_too_large
            ):
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
        return await self._refresh_runtime_address(claw)

    # ------------------------------------------------------------------
    # File proxy
    # ------------------------------------------------------------------

    async def get_file(self, user_id: str, filename: str) -> tuple[bytes, str]:
        claw = await self.claw_repository.get_by_user_id(user_id)
        if not claw or not claw.http_base_url:
            raise ValueError("No running claw instance found")
        if claw.status != ClawStatus.RUNNING:
            raise ValueError(f"Claw is not running (status: {claw.status})")
        claw = await self._refresh_runtime_address(claw)
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
