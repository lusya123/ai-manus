"""Durable AgentBay lifecycle coordinated with the Mongo cost ledger."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

from app.domain.external.agentbay_quota import (
    AgentBayBootstrapState,
    AgentBayQuotaBootstrapError,
    AgentBayQuotaConfigurationError,
    AgentBayQuotaInconsistentError,
    AgentBayQuotaLedger,
    AgentBayQuotaOutcome,
    AgentBayQuotaReservation,
    AgentBayReservationPhase,
)
from app.domain.external.sandbox import Sandbox, SandboxUnavailableError
from app.domain.external.sandbox_provisioner import SandboxProvisioner
from app.domain.models.session import Session
from app.domain.repositories.session_repository import SessionRepository
from app.infrastructure.external.sandbox.agentbay_sandbox import AgentBaySandbox


_EXPECTED_TASK_SANDBOX_ID_UNSET = object()


class AgentBayProvisioner(SandboxProvisioner):
    """The only application path allowed to allocate billable sessions.

    ``ensure_locked`` and ``destroy_locked`` assume the caller already holds
    the logical session's renewable distributed lifecycle lease.
    """

    _OK_PROVISIONED = {
        AgentBayQuotaOutcome.PROVISIONED,
        AgentBayQuotaOutcome.ALREADY_PROVISIONED,
    }
    _PROVIDER_NAME = "agentbay"
    _OK_RELEASED = {
        AgentBayQuotaOutcome.RELEASED,
        AgentBayQuotaOutcome.ALREADY_RELEASED,
    }
    _OK_RESERVED = {
        AgentBayQuotaOutcome.RESERVED,
        AgentBayQuotaOutcome.EXISTING_RESERVED,
        AgentBayQuotaOutcome.EXISTING_PROVISIONED,
        # ``ensure_locked`` deliberately supplies a new UUID each time.  An
        # existing logical-session reservation therefore reports stale while
        # returning the authoritative operation that must be recovered.
        AgentBayQuotaOutcome.STALE_OPERATION,
    }

    def __init__(
        self,
        *,
        ledger: AgentBayQuotaLedger,
        session_repository: SessionRepository,
        deployment_id: str,
        sandbox_cls: type[AgentBaySandbox] = AgentBaySandbox,
    ) -> None:
        if not isinstance(deployment_id, str) or not deployment_id.strip():
            raise ValueError("AgentBay deployment_id is required")
        self._ledger = ledger
        self._sessions = session_repository
        self._sandbox_cls = sandbox_cls
        self._deployment_label = hashlib.sha256(
            f"ai-manus-agentbay-deployment-v1\0{deployment_id}".encode()
        ).hexdigest()

    def _labels(
        self, session_id: str, user_id: str, operation_id: str
    ) -> dict[str, str]:
        session_label = hashlib.sha256(
            f"ai-manus-agentbay-session-v1\0{session_id}".encode()
        ).hexdigest()
        user_label = hashlib.sha256(
            f"ai-manus-agentbay-user-v1\0{user_id}".encode()
        ).hexdigest()
        return {
            "manus_app": "ai-manus-v1",
            "manus_deployment": self._deployment_label,
            "manus_session": session_label,
            "manus_user": user_label,
            "manus_operation": operation_id,
        }

    async def _persist_runtime(
        self,
        session: Session,
        *,
        expected_sandbox_id: str | None,
        expected_task_id: str | None,
        expected_sandbox_provider: str | None,
        expected_task_sandbox_id: object = _EXPECTED_TASK_SANDBOX_ID_UNSET,
    ) -> None:
        compare_and_set = getattr(
            self._sessions, "compare_and_set_runtime_ownership", None
        )
        if callable(compare_and_set):
            updated = await compare_and_set(
                session.id,
                expected_sandbox_id,
                expected_task_id,
                (
                    session.task_sandbox_id
                    if expected_task_sandbox_id
                    is _EXPECTED_TASK_SANDBOX_ID_UNSET
                    else expected_task_sandbox_id
                ),
                expected_sandbox_provider,
                session.sandbox_id,
                session.task_id,
                session.sandbox_provider,
                session.task_sandbox_id,
            )
            if updated:
                return
            current = await self._sessions.find_by_id(session.id)
            if (
                current is not None
                and current.sandbox_id == session.sandbox_id
                and current.task_id == session.task_id
                and current.sandbox_provider == session.sandbox_provider
                and current.task_sandbox_id == session.task_sandbox_id
                and not current.sandbox_destroying
                and not current.deleting
            ):
                return
            raise AgentBayQuotaInconsistentError(
                "Session runtime changed during AgentBay lifecycle operation"
            )
        update = getattr(self._sessions, "update_runtime_ownership", None)
        if callable(update):
            await update(
                session.id,
                session.sandbox_id,
                session.task_id,
                session.sandbox_provider,
                session.task_sandbox_id,
            )
            return
        await self._sessions.save(session)

    async def _persist_runtime_before_cancellation(
        self,
        session: Session,
        *,
        expected_sandbox_id: str | None,
        expected_task_id: str | None,
        expected_sandbox_provider: str | None,
        expected_task_sandbox_id: object = _EXPECTED_TASK_SANDBOX_ID_UNSET,
    ) -> None:
        task = asyncio.create_task(self._persist_runtime(
            session,
            expected_sandbox_id=expected_sandbox_id,
            expected_task_id=expected_task_id,
            expected_sandbox_provider=expected_sandbox_provider,
            expected_task_sandbox_id=expected_task_sandbox_id,
        ))
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                # Cancellation can be requested more than once. Keep the
                # recovery projection shielded until it has a definite result.
                if task.cancelled():
                    # A cancellation originating inside the repository task
                    # cannot become successful by retrying the same Future.
                    raise
                cancelled = True
                if task.done():
                    # Observe a simultaneously completed child result.  The
                    # caller's cancellation still takes precedence below.
                    task.exception()
                    break
                continue
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError
                raise
        if cancelled:
            raise asyncio.CancelledError

    async def _claim_destroy(self, session: Session) -> None:
        if session.sandbox_destroying:
            return
        claim = getattr(self._sessions, "claim_runtime_destroy", None)
        if not callable(claim):
            return
        claimed = await claim(
            session.id,
            session.sandbox_id,
            session.task_id,
            session.task_sandbox_id,
            session.sandbox_provider,
        )
        if not claimed:
            current = await self._sessions.find_by_id(session.id)
            if not (
                current is not None
                and current.sandbox_id == session.sandbox_id
                and current.task_id == session.task_id
                and current.task_sandbox_id == session.task_sandbox_id
                and current.sandbox_provider == session.sandbox_provider
                and current.sandbox_destroying
            ):
                raise AgentBayQuotaInconsistentError(
                    "Session runtime changed before AgentBay deletion"
                )
        session.sandbox_destroying = True

    async def _finish_destroy(
        self,
        session: Session,
        *,
        expected_sandbox_id: str | None,
        expected_task_id: str | None,
        expected_task_sandbox_id: str | None,
        expected_sandbox_provider: str | None,
    ) -> None:
        finish = getattr(self._sessions, "finish_runtime_destroy", None)
        if callable(finish):
            finished = await finish(
                session.id,
                expected_sandbox_id,
                expected_task_id,
                expected_task_sandbox_id,
                expected_sandbox_provider,
                session.sandbox_id,
                session.task_id,
                session.sandbox_provider,
                session.task_sandbox_id,
            )
            if not finished:
                current = await self._sessions.find_by_id(session.id)
                if not (
                    current is not None
                    and current.sandbox_id == session.sandbox_id
                    and current.task_id == session.task_id
                    and current.sandbox_provider == session.sandbox_provider
                    and current.task_sandbox_id == session.task_sandbox_id
                    and not current.sandbox_destroying
                    and current.deleting == session.deleting
                ):
                    raise AgentBayQuotaInconsistentError(
                        "Session runtime changed while finishing AgentBay deletion"
                    )
            session.sandbox_destroying = False
            return
        session.sandbox_destroying = False
        await self._persist_runtime_before_cancellation(
            session,
            expected_sandbox_id=expected_sandbox_id,
            expected_task_id=expected_task_id,
            expected_sandbox_provider=expected_sandbox_provider,
            expected_task_sandbox_id=expected_task_sandbox_id,
        )

    async def _adopt_legacy_ownership(
        self,
        session: Session,
        reservation: AgentBayQuotaReservation | None,
    ) -> None:
        if not session.sandbox_id or session.sandbox_provider is not None:
            return
        if (
            reservation is None
            or reservation.phase is not AgentBayReservationPhase.PROVISIONED
            or reservation.provider_id != session.sandbox_id
        ):
            raise AgentBayQuotaInconsistentError(
                "Legacy sandbox provider ownership is unknown; the AgentBay "
                "cost ledger does not exactly match the Session pointer"
            )
        previous_id = session.sandbox_id
        previous_task_id = session.task_id
        session.sandbox_provider = self._PROVIDER_NAME
        await self._persist_runtime_before_cancellation(
            session,
            expected_sandbox_id=previous_id,
            expected_task_id=previous_task_id,
            expected_sandbox_provider=None,
        )

    async def _live_labeled_sessions(
        self,
        session: Session,
        operation_id: str,
        *,
        include_deployment: bool = True,
    ) -> list[Any]:
        labels = self._labels(session.id, session.user_id, operation_id)
        if not include_deployment:
            # Cleanup may run from a replica whose deployment/config version
            # differs from the stored ledger.  The stable logical-session and
            # operation labels remain exact recovery keys across that drift.
            labels.pop("manus_deployment")
        ids = await self._sandbox_cls.list_provider_session_ids(
            labels
        )
        live: list[Any] = []
        for provider_id in ids:
            provider = await self._sandbox_cls.lookup_provider_session(provider_id)
            if provider is not None:
                self._exact_provider_id(provider, provider_id)
                live.append(provider)
        return live

    async def _ensure_bootstrapped(self) -> None:
        result = await self._ledger.ensure_ledger()
        if result.bootstrap_state is AgentBayBootstrapState.READY:
            return
        if result.bootstrap_state is AgentBayBootstrapState.RECONCILING:
            raise AgentBayQuotaBootstrapError(AgentBayBootstrapState.RECONCILING)
        if result.bootstrap_state is not AgentBayBootstrapState.REQUIRED:
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota ledger has an unknown bootstrap state"
            )

        transition = await self._ledger.begin_reconciliation()
        if (
            transition.bootstrap_state is not AgentBayBootstrapState.RECONCILING
            or not isinstance(transition.revision, int)
            or isinstance(transition.revision, bool)
            or transition.revision < 0
        ):
            raise AgentBayQuotaInconsistentError(
                "AgentBay reconciliation transition has no valid revision"
            )
        # The maintenance gate is now closed. Only an authoritatively empty
        # Mongo + provider inventory may initialize itself automatically.
        # Query the whole AgentBay account: legacy sessions may predate the
        # deployment labels and are still billable resources.
        sessions = await self._sessions.get_all()
        has_mongo_ownership = any(item.sandbox_id for item in sessions)
        provider_ids = await self._sandbox_cls.list_provider_session_ids({})
        if has_mongo_ownership or provider_ids:
            raise AgentBayQuotaBootstrapError(AgentBayBootstrapState.RECONCILING)
        try:
            reconciled = await self._ledger.reconcile_inventory(
                [], expected_revision=transition.revision
            )
        except AgentBayQuotaBootstrapError:
            snapshot = await self._ledger.snapshot()
            if snapshot.bootstrap_state is AgentBayBootstrapState.READY:
                return
            raise
        if reconciled.bootstrap_state is not AgentBayBootstrapState.READY:
            raise AgentBayQuotaBootstrapError(
                reconciled.bootstrap_state or AgentBayBootstrapState.RECONCILING
            )

    @staticmethod
    def _reservation_or_raise(result) -> AgentBayQuotaReservation:
        if result.reservation is None:
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota operation has no reservation"
            )
        return result.reservation

    @staticmethod
    def _provider_id(provider: Any) -> str:
        provider_id = getattr(provider, "session_id", None)
        if (
            not isinstance(provider_id, str)
            or not provider_id
            or provider_id != provider_id.strip()
            or len(provider_id) > 1024
        ):
            raise AgentBayQuotaInconsistentError(
                "AgentBay returned an invalid provider ID"
            )
        return provider_id

    @classmethod
    def _exact_provider_id(cls, provider: Any, expected: str) -> str:
        provider_id = cls._provider_id(provider)
        if provider_id != expected:
            raise AgentBayQuotaInconsistentError(
                "AgentBay lookup returned a mismatched provider ID"
            )
        return provider_id

    @classmethod
    def _provisioned_reservation_or_raise(
        cls,
        result,
        *,
        operation_id: str,
        provider_id: str,
    ) -> AgentBayQuotaReservation:
        if result.outcome not in cls._OK_PROVISIONED:
            raise AgentBayQuotaInconsistentError(
                "AgentBay provider ownership could not be persisted"
            )
        persisted = cls._reservation_or_raise(result)
        if (
            persisted.phase is not AgentBayReservationPhase.PROVISIONED
            or persisted.operation_id != operation_id
            or persisted.provider_id != provider_id
        ):
            raise AgentBayQuotaInconsistentError(
                "AgentBay provider ownership response is inconsistent"
            )
        return persisted

    async def _clear_stale_session_pointer(self, session: Session) -> None:
        if not session.sandbox_id:
            return
        stale_id = session.sandbox_id
        provider = await self._sandbox_cls.lookup_provider_session(stale_id)
        if provider is not None:
            raise AgentBayQuotaInconsistentError(
                "Session points to a live provider outside its ledger operation"
            )
        previous_task_id = session.task_id
        previous_provider = session.sandbox_provider
        session.sandbox_id = None
        session.sandbox_provider = self._PROVIDER_NAME
        await self._persist_runtime_before_cancellation(
            session,
            expected_sandbox_id=stale_id,
            expected_task_id=previous_task_id,
            expected_sandbox_provider=previous_provider,
        )

    async def _ensure_reserved(
        self,
        session: Session,
        reservation: AgentBayQuotaReservation,
        *,
        allow_allocate: bool,
    ) -> Sandbox:
        if reservation.phase is not AgentBayReservationPhase.RESERVED:
            raise AgentBayQuotaInconsistentError(
                "AgentBay allocation expected a reserved operation"
            )
        if reservation.provider_id is not None:
            raise AgentBayQuotaInconsistentError(
                "Reserved AgentBay operation unexpectedly has a provider ID"
            )
        live = await self._live_labeled_sessions(session, reservation.operation_id)
        if len(live) > 1:
            raise AgentBayQuotaInconsistentError(
                "Multiple AgentBay sessions share one operation label"
            )
        if session.sandbox_id and (
            not live or self._provider_id(live[0]) != session.sandbox_id
        ):
            await self._clear_stale_session_pointer(session)

        if live:
            provider = live[0]
        else:
            if not allow_allocate:
                # A recovered RESERVED operation may have reached AgentBay even
                # though its create response never reached Mongo. Provider
                # label listing is eventually consistent, so one empty result
                # cannot authorize another billable allocation.
                raise AgentBayQuotaInconsistentError(
                    "Reserved AgentBay allocation outcome is unknown; "
                    "wait for provider inventory consistency and reconcile"
                )
            provider = await self._sandbox_cls.allocate(
                labels=self._labels(
                    session.id, session.user_id, reservation.operation_id
                )
            )
            # Detect an already-completed create whose response raced this
            # attempt. Include the direct response because provider list may
            # be briefly eventually consistent.
            recovered = await self._live_labeled_sessions(
                session, reservation.operation_id
            )
            provider_id = self._provider_id(provider)
            provider_ids = {self._provider_id(item) for item in recovered}
            provider_ids.add(provider_id)
            if len(provider_ids) > 1:
                raise AgentBayQuotaInconsistentError(
                    "Multiple AgentBay sessions were allocated for one operation"
                )

        provider_id = self._provider_id(provider)
        provisioned = await self._ledger.mark_provisioned(
            session.id,
            session.user_id,
            reservation.operation_id,
            provider_id,
        )
        self._provisioned_reservation_or_raise(
            provisioned,
            operation_id=reservation.operation_id,
            provider_id=provider_id,
        )
        previous_id = session.sandbox_id
        previous_task_id = session.task_id
        previous_provider = session.sandbox_provider
        session.sandbox_id = provider_id
        session.sandbox_provider = self._PROVIDER_NAME
        await self._persist_runtime_before_cancellation(
            session,
            expected_sandbox_id=previous_id,
            expected_task_id=previous_task_id,
            expected_sandbox_provider=previous_provider,
        )
        # Link resolution happens only after both durable recovery pointers.
        return await self._sandbox_cls.connect(provider)

    async def _ensure_provisioned(
        self, session: Session, reservation: AgentBayQuotaReservation
    ) -> Sandbox:
        provider_id = reservation.provider_id
        if not provider_id:
            raise AgentBayQuotaInconsistentError(
                "Provisioned AgentBay reservation has no provider ID"
            )
        if (
            not isinstance(provider_id, str)
            or provider_id != provider_id.strip()
            or len(provider_id) > 1024
        ):
            raise AgentBayQuotaInconsistentError(
                "Provisioned AgentBay reservation has an invalid provider ID"
            )
        if session.sandbox_id and session.sandbox_id != provider_id:
            await self._clear_stale_session_pointer(session)
        provider = await self._sandbox_cls.lookup_provider_session(provider_id)
        if provider is not None:
            self._exact_provider_id(provider, provider_id)
            if (
                session.sandbox_id != provider_id
                or session.sandbox_provider != self._PROVIDER_NAME
            ):
                previous_id = session.sandbox_id
                previous_task_id = session.task_id
                previous_provider = session.sandbox_provider
                session.sandbox_id = provider_id
                session.sandbox_provider = self._PROVIDER_NAME
                await self._persist_runtime_before_cancellation(
                    session,
                    expected_sandbox_id=previous_id,
                    expected_task_id=previous_task_id,
                    expected_sandbox_provider=previous_provider,
                )
            return await self._sandbox_cls.connect(provider)

        replacement_id = str(uuid.uuid4())
        replacement = await self._ledger.replace_operation(
            session.id,
            session.user_id,
            reservation.operation_id,
            replacement_id,
            expected_provider_id=provider_id,
        )
        if replacement.outcome not in {
            AgentBayQuotaOutcome.REPLACED,
            AgentBayQuotaOutcome.ALREADY_REPLACED,
        }:
            raise AgentBayQuotaInconsistentError(
                "AgentBay replacement lost its lifecycle compare-and-set"
            )
        previous_id = session.sandbox_id
        previous_task_id = session.task_id
        previous_provider = session.sandbox_provider
        session.sandbox_id = None
        session.sandbox_provider = self._PROVIDER_NAME
        await self._persist_runtime_before_cancellation(
            session,
            expected_sandbox_id=previous_id,
            expected_task_id=previous_task_id,
            expected_sandbox_provider=previous_provider,
        )
        return await self._ensure_reserved(
            session,
            self._reservation_or_raise(replacement),
            allow_allocate=True,
        )

    async def ensure_locked(self, session: Session) -> Sandbox:
        if session.deleting:
            raise AgentBayQuotaInconsistentError(
                "Session deletion is in progress"
            )
        if session.sandbox_destroying:
            # Resume an exact durable tombstone after a crashed/timed-out
            # owner. The session lifecycle lease serializes this recovery and
            # the AgentBay ledger/provider IDs fence it from replacements.
            await self.destroy_locked(session, preserve_task=True)
        if session.sandbox_provider not in (None, self._PROVIDER_NAME):
            raise AgentBayQuotaInconsistentError(
                "Session sandbox belongs to a different provider"
            )
        await self._ensure_bootstrapped()
        if session.sandbox_id and session.sandbox_provider is None:
            reservation = await self._ledger.get_reservation_for_cleanup(
                session.id, session.user_id
            )
            await self._adopt_legacy_ownership(session, reservation)
        result = await self._ledger.reserve(
            session.id, session.user_id, str(uuid.uuid4())
        )
        if result.outcome not in self._OK_RESERVED:
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota reservation was not confirmed"
            )
        reservation = self._reservation_or_raise(result)
        if (
            result.outcome is AgentBayQuotaOutcome.RESERVED
            and reservation.phase is not AgentBayReservationPhase.RESERVED
        ) or (
            result.outcome is AgentBayQuotaOutcome.EXISTING_RESERVED
            and reservation.phase is not AgentBayReservationPhase.RESERVED
        ) or (
            result.outcome is AgentBayQuotaOutcome.EXISTING_PROVISIONED
            and reservation.phase is not AgentBayReservationPhase.PROVISIONED
        ):
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota reservation response has an inconsistent phase"
            )
        if reservation.phase is AgentBayReservationPhase.PROVISIONED:
            return await self._ensure_provisioned(session, reservation)
        return await self._ensure_reserved(
            session,
            reservation,
            allow_allocate=result.outcome is AgentBayQuotaOutcome.RESERVED,
        )

    async def _release(
        self, session: Session, reservation: AgentBayQuotaReservation
    ) -> None:
        result = await self._ledger.release(
            session.id,
            session.user_id,
            reservation.operation_id,
            provider_id=reservation.provider_id,
        )
        if result.outcome not in self._OK_RELEASED:
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota release did not confirm the exact operation"
            )

    async def destroy_locked(
        self, session: Session, *, preserve_task: bool = False
    ) -> None:
        if session.sandbox_provider not in (None, self._PROVIDER_NAME):
            raise AgentBayQuotaInconsistentError(
                "Session sandbox belongs to a different provider"
            )
        reservation = await self._ledger.get_reservation_for_cleanup(
            session.id, session.user_id
        )
        await self._adopt_legacy_ownership(session, reservation)
        if reservation is None:
            if session.sandbox_id:
                raise AgentBayQuotaInconsistentError(
                    "Session has AgentBay ownership missing from the cost ledger"
                )
            previous_id = session.sandbox_id
            previous_task_id = session.task_id
            previous_task_sandbox_id = session.task_sandbox_id
            previous_provider = session.sandbox_provider
            if not preserve_task:
                session.task_id = None
                session.task_sandbox_id = None
            session.sandbox_provider = None
            await self._persist_runtime_before_cancellation(
                session,
                expected_sandbox_id=previous_id,
                expected_task_id=previous_task_id,
                expected_sandbox_provider=previous_provider,
                expected_task_sandbox_id=previous_task_sandbox_id,
            )
            return

        provider: Any | None = None
        discovered_provider_id: str | None = None
        if reservation.phase is AgentBayReservationPhase.RESERVED:
            live = await self._live_labeled_sessions(
                session,
                reservation.operation_id,
                include_deployment=False,
            )
            if len(live) > 1:
                raise AgentBayQuotaInconsistentError(
                    "Multiple AgentBay sessions share one reserved operation"
            )
            if live:
                provider = live[0]
                discovered_provider_id = self._provider_id(provider)
                try:
                    marked = await self._ledger.mark_provisioned(
                        session.id,
                        session.user_id,
                        reservation.operation_id,
                        discovered_provider_id,
                    )
                except AgentBayQuotaConfigurationError:
                    # Cleanup reads/releases intentionally tolerate a rolling
                    # deployment configuration mismatch.  The reserved
                    # operation plus stable provider labels remains a durable
                    # recovery handle, so delete it exactly without promoting
                    # the reservation through the create-only CAS path.
                    pass
                else:
                    reservation = self._provisioned_reservation_or_raise(
                        marked,
                        operation_id=reservation.operation_id,
                        provider_id=discovered_provider_id,
                    )
            else:
                # The provider list is eventually consistent. An empty label
                # scan cannot prove that an interrupted create never committed,
                # so retaining the RESERVED ledger entry is the only safe way
                # to avoid an untracked billable orphan.
                raise AgentBayQuotaInconsistentError(
                    "Reserved AgentBay allocation outcome is unknown; "
                    "cleanup requires reconciliation"
                )

        provider_id = reservation.provider_id or discovered_provider_id
        if not provider_id:
            raise AgentBayQuotaInconsistentError(
                "AgentBay cleanup reservation has no provider ID"
            )
        if session.sandbox_id and session.sandbox_id != provider_id:
            await self._clear_stale_session_pointer(session)
        previous_id = session.sandbox_id
        previous_task_id = session.task_id
        previous_task_sandbox_id = session.task_sandbox_id
        previous_provider = session.sandbox_provider
        await self._claim_destroy(session)
        if provider is None:
            provider = await self._sandbox_cls.lookup_provider_session(provider_id)
        if provider is not None:
            self._exact_provider_id(provider, provider_id)
            async with asyncio.timeout(
                getattr(
                    self._sandbox_cls,
                    "_PROVIDER_DELETE_TIMEOUT_SECONDS",
                    AgentBaySandbox._PROVIDER_DELETE_TIMEOUT_SECONDS,
                )
            ):
                result = await provider.delete()
            if not getattr(result, "success", False):
                raise SandboxUnavailableError(
                    "AgentBay did not confirm session deletion"
                )
        # The SDK's delete success uses loose message matching. Only this
        # independent exact-code probe authorizes ledger release.
        if await self._sandbox_cls.lookup_provider_session(provider_id) is not None:
            raise SandboxUnavailableError(
                "AgentBay session still exists after deletion"
            )

        session.sandbox_id = None
        if not preserve_task:
            session.task_id = None
            session.task_sandbox_id = None
        # Keep the allocator marker until the cost-ledger CAS succeeds. If the
        # release is unavailable, a deployment-wide provider switch must not
        # overwrite the only signal that AgentBay cleanup is still required.
        session.sandbox_provider = self._PROVIDER_NAME
        await self._finish_destroy(
            session,
            expected_sandbox_id=previous_id,
            expected_task_id=previous_task_id,
            expected_task_sandbox_id=previous_task_sandbox_id,
            expected_sandbox_provider=previous_provider,
        )
        await self._release(session, reservation)
        previous_task_id = session.task_id
        session.sandbox_provider = None
        await self._persist_runtime_before_cancellation(
            session,
            expected_sandbox_id=None,
            expected_task_id=previous_task_id,
            expected_sandbox_provider=self._PROVIDER_NAME,
        )
