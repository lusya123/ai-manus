"""Unbilled sandbox lifecycle used by Docker and fixed local sandboxes."""

import asyncio
import logging
from typing import Type

from app.domain.external.sandbox import (
    Sandbox,
    SandboxProvisioningError,
    SandboxShellCleanupUnsupportedError,
    SandboxShellProcessScope,
    SandboxUnavailableError,
)
from app.domain.external.sandbox_provisioner import SandboxProvisioner
from app.domain.external.coordination import current_lifecycle_task_lost_lease
from app.domain.models.session import Session
from app.domain.repositories.session_repository import SessionRepository


_EXPECTED_SANDBOX_ID_UNSET = object()
_EXPECTED_TASK_ID_UNSET = object()
_EXPECTED_TASK_SANDBOX_ID_UNSET = object()

logger = logging.getLogger(__name__)


class PassthroughSandboxProvisioner(SandboxProvisioner):
    _OWNERSHIP_RECONCILE_INTERVAL_SECONDS = 1.0
    _OWNERSHIP_RECONCILE_MAX_ATTEMPTS = 3
    _SHELL_CLEANUP_TIMEOUT_SECONDS = 20.0

    def __init__(
        self,
        sandbox_cls: Type[Sandbox],
        session_repository: SessionRepository,
        provider_name: str = "docker",
    ) -> None:
        if not provider_name or provider_name != provider_name.strip():
            raise ValueError("sandbox provider_name is required")
        self._sandbox_cls = sandbox_cls
        self._session_repository = session_repository
        self._provider_name = provider_name

    @staticmethod
    async def _close_sandbox_handle(sandbox: Sandbox | None) -> None:
        close = getattr(sandbox, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception:
                pass

    async def _persist(
        self,
        session: Session,
        *,
        expected_sandbox_id: object = _EXPECTED_SANDBOX_ID_UNSET,
        expected_sandbox_provider: str | None,
        expected_task_id: object = _EXPECTED_TASK_ID_UNSET,
        expected_task_sandbox_id: object = _EXPECTED_TASK_SANDBOX_ID_UNSET,
    ) -> None:
        compare_and_set = getattr(
            self._session_repository,
            "compare_and_set_runtime_ownership",
            None,
        )
        if (
            expected_sandbox_id is not _EXPECTED_SANDBOX_ID_UNSET
            and callable(compare_and_set)
        ):
            updated = await compare_and_set(
                session.id,
                expected_sandbox_id,
                (
                    session.task_id
                    if expected_task_id is _EXPECTED_TASK_ID_UNSET
                    else expected_task_id
                ),
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
            # A previous write may have succeeded even though its reply was
            # lost. Treat an exact durable match as idempotent, but never let a
            # stale generation overwrite a newer one.
            find_by_id = getattr(self._session_repository, "find_by_id", None)
            current = await find_by_id(session.id) if callable(find_by_id) else None
            if (
                current is not None
                and current.sandbox_id == session.sandbox_id
                and current.sandbox_provider == session.sandbox_provider
                and current.task_id == session.task_id
                and current.task_sandbox_id == session.task_sandbox_id
                and not current.sandbox_destroying
                and not current.deleting
            ):
                return
            raise RuntimeError(
                "Session sandbox ownership changed during lifecycle operation"
            )
        update = getattr(
            self._session_repository, "update_runtime_ownership", None
        )
        if callable(update):
            updated = await update(
                session.id,
                session.sandbox_id,
                session.task_id,
                session.sandbox_provider,
                session.task_sandbox_id,
            )
            if updated is False:
                raise RuntimeError("Session disappeared during sandbox lifecycle")
            return
        await self._session_repository.save(session)

    async def _claim_destroy(self, session: Session) -> None:
        """Make the exact runtime/task projection non-adoptable before delete."""

        if session.sandbox_destroying:
            return
        claim = getattr(
            self._session_repository, "claim_runtime_destroy", None
        )
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
            current = await self._session_repository.find_by_id(session.id)
            if not (
                current is not None
                and current.sandbox_id == session.sandbox_id
                and current.task_id == session.task_id
                and current.task_sandbox_id == session.task_sandbox_id
                and current.sandbox_provider == session.sandbox_provider
                and current.sandbox_destroying
            ):
                raise RuntimeError(
                    "Session runtime changed before provider deletion"
                )
        session.sandbox_destroying = True

    async def _finish_destroy(
        self,
        session: Session,
        *,
        expected_sandbox_id: str,
        expected_task_id: str | None,
        expected_task_sandbox_id: str | None,
        expected_sandbox_provider: str | None,
    ) -> None:
        finish = getattr(
            self._session_repository, "finish_runtime_destroy", None
        )
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
                current = await self._session_repository.find_by_id(session.id)
                if not (
                    current is not None
                    and current.sandbox_id == session.sandbox_id
                    and current.task_id == session.task_id
                    and current.sandbox_provider == session.sandbox_provider
                    and current.task_sandbox_id == session.task_sandbox_id
                    and not current.sandbox_destroying
                    and current.deleting == session.deleting
                ):
                    raise RuntimeError(
                        "Session runtime changed while finishing provider deletion"
                    )
            session.sandbox_destroying = False
            return
        session.sandbox_destroying = False
        await self._persist(
            session,
            expected_sandbox_id=expected_sandbox_id,
            expected_task_id=expected_task_id,
            expected_task_sandbox_id=expected_task_sandbox_id,
            expected_sandbox_provider=expected_sandbox_provider,
        )

    @staticmethod
    async def _await_task_to_known_outcome(task: asyncio.Task) -> object:
        """Ignore repeated caller cancellation until lifecycle I/O replies."""

        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    break
                continue
        return task.result()

    async def _cleanup_claim_state(
        self, session: Session, sandbox_id: str
    ) -> str:
        """Classify an ambiguous pointer and claim it before provider delete.

        ``claimed`` means the exact durable runtime/task projection is now
        non-adoptable. An absent pointer is atomically published as a tombstone
        before provider deletion starts. ``unpublished`` is reserved for an
        absent/deleting Session or a different generation, where this exact
        provider ID is detached and must never be published as adoptable.
        ``adopted`` means another task/provider transition owns the same
        pointer and this stale operation must not touch it.
        """

        claim = getattr(
            self._session_repository, "claim_runtime_destroy", None
        )
        find_by_id = getattr(self._session_repository, "find_by_id", None)
        publish_claim = getattr(
            self._session_repository,
            "publish_runtime_destroy_claim",
            None,
        )
        if not callable(claim) or not callable(find_by_id):
            return "unpublished"
        current = await find_by_id(session.id)
        if current is None:
            return "unpublished"
        if current.sandbox_id is None:
            if (
                current.deleting
                or current.task_id != session.task_id
                or current.task_sandbox_id != session.task_sandbox_id
                or current.sandbox_provider is not None
                or not callable(publish_claim)
            ):
                return "unpublished"
            try:
                claimed = await publish_claim(
                    session.id,
                    session.task_id,
                    session.task_sandbox_id,
                    None,
                    sandbox_id,
                    self._provider_name,
                )
            except Exception:
                # Mongo may have committed even when the driver lost the
                # response. The authoritative re-read below distinguishes a
                # durable tombstone from a still-detached generation.
                claimed = False
            if not claimed:
                # The update response may be lost, or another lifecycle owner
                # may have moved the projection. Re-read before classifying.
                current = await find_by_id(session.id)
                if current is None or current.sandbox_id != sandbox_id:
                    return "unpublished"
                if (
                    current.sandbox_provider != self._provider_name
                    or current.task_id != session.task_id
                    or current.task_sandbox_id != session.task_sandbox_id
                    or not current.sandbox_destroying
                ):
                    return "adopted"
            session.sandbox_id = sandbox_id
            session.sandbox_provider = self._provider_name
            session.sandbox_destroying = True
            session.deleting = current.deleting if current is not None else False
            return "claimed"
        if current.sandbox_id != sandbox_id:
            return "unpublished"
        if (
            current.sandbox_provider != self._provider_name
            or current.task_id != session.task_id
            or current.task_sandbox_id != session.task_sandbox_id
        ):
            return "adopted"
        session.sandbox_id = current.sandbox_id
        session.sandbox_provider = current.sandbox_provider
        session.sandbox_destroying = current.sandbox_destroying
        session.deleting = current.deleting
        await self._claim_destroy(session)
        return "claimed"

    async def _persist_failed_create_ownership(
        self,
        session: Session,
        sandbox_id: str,
    ) -> None:
        """Persist an indeterminate create or reconcile its exact provider ID."""

        async def state_machine() -> None:
            for attempt in range(self._OWNERSHIP_RECONCILE_MAX_ATTEMPTS):
                # A failed/indeterminate create must never become adoptable.
                # Publish an absent pointer directly as a tombstone, or claim
                # an ambiguous successful write, before provider deletion.
                claim_state = await self._cleanup_claim_state(
                    session, sandbox_id
                )
                if claim_state == "adopted":
                    return
                destroyed = False
                sandbox = None
                try:
                    exact_destroyed = await self._destroy_exact_planned_id(
                        session, sandbox_id
                    )
                    if exact_destroyed is not None:
                        destroyed = exact_destroyed
                    else:
                        sandbox = await self._sandbox_cls.get(sandbox_id)
                        if sandbox is None:
                            destroyed = True
                        elif getattr(sandbox, "id", None) == sandbox_id:
                            destroyed = await sandbox.destroy() is True
                except Exception:
                    destroyed = False
                finally:
                    if sandbox is not None and not destroyed:
                        close = getattr(sandbox, "aclose", None)
                        if callable(close):
                            try:
                                await close()
                            except Exception:
                                pass

                if destroyed:
                    session.sandbox_id = None
                    session.sandbox_provider = None
                    if claim_state == "claimed":
                        await self._finish_destroy(
                            session,
                            expected_sandbox_id=sandbox_id,
                            expected_task_id=session.task_id,
                            expected_task_sandbox_id=session.task_sandbox_id,
                            expected_sandbox_provider=self._provider_name,
                        )
                    return

                if claim_state == "claimed":
                    # The tombstone is durable. Retrying the exact generation
                    # is safe even if this coroutine later loses its lease.
                    if attempt + 1 < self._OWNERSHIP_RECONCILE_MAX_ATTEMPTS:
                        await asyncio.sleep(
                            self._OWNERSHIP_RECONCILE_INTERVAL_SECONDS
                        )
                    continue

                if attempt + 1 < self._OWNERSHIP_RECONCILE_MAX_ATTEMPTS:
                    await asyncio.sleep(
                        self._OWNERSHIP_RECONCILE_INTERVAL_SECONDS
                    )

            raise SandboxProvisioningError(
                sandbox_id,
                "Sandbox ownership could not be persisted and exact cleanup "
                "could not be confirmed within the reconciliation budget",
            )

        state_task = asyncio.create_task(state_machine())
        await self._await_task_to_known_outcome(state_task)

    async def _rollback_unpublished_sandbox(
        self,
        session: Session,
        sandbox: Sandbox,
    ) -> None:
        """Resolve provider rollback, then persist the authoritative pointer."""

        async def state_machine() -> None:
            for attempt in range(self._OWNERSHIP_RECONCILE_MAX_ATTEMPTS):
                claim_state = await self._cleanup_claim_state(
                    session, sandbox.id
                )
                if claim_state == "adopted":
                    return
                try:
                    destroyed = await sandbox.destroy() is True
                except Exception:
                    destroyed = False
                if destroyed:
                    session.sandbox_id = None
                    session.sandbox_provider = None
                    if claim_state == "claimed":
                        await self._finish_destroy(
                            session,
                            expected_sandbox_id=sandbox.id,
                            expected_task_id=session.task_id,
                            expected_task_sandbox_id=session.task_sandbox_id,
                            expected_sandbox_provider=self._provider_name,
                        )
                    return

                # A failed delete remains either durably tombstoned or fully
                # detached. Never republish it through the ordinary ownership
                # CAS, which would make a late provider deletion adoptable.
                if attempt + 1 < self._OWNERSHIP_RECONCILE_MAX_ATTEMPTS:
                    await asyncio.sleep(
                        self._OWNERSHIP_RECONCILE_INTERVAL_SECONDS
                    )

            raise SandboxProvisioningError(
                sandbox.id,
                "Sandbox rollback and ownership persistence both remained "
                "unconfirmed within the reconciliation budget",
            )

        state_task = asyncio.create_task(state_machine())
        await self._await_task_to_known_outcome(state_task)

    async def _adopt_legacy_if_exact(self, session: Session) -> Sandbox | None:
        if not session.sandbox_id or session.sandbox_provider is not None:
            return None
        sandbox = await self._sandbox_cls.get(session.sandbox_id)
        exact_id = (
            sandbox is not None
            and getattr(sandbox, "id", None) == session.sandbox_id
        )
        if not exact_id:
            close = getattr(sandbox, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    pass
            raise SandboxProvisioningError(
                session.sandbox_id,
                "Legacy sandbox provider ownership is unknown; the current "
                "provider did not confirm the exact sandbox ID",
            )
        session.sandbox_provider = self._provider_name
        await self._persist(
            session,
            expected_sandbox_id=session.sandbox_id,
            expected_sandbox_provider=None,
        )
        return sandbox

    def _planned_id(self, session: Session) -> str | None:
        planned_id = getattr(self._sandbox_cls, "planned_id", None)
        create_owned = getattr(self._sandbox_cls, "create_owned", None)
        if not callable(planned_id) or not callable(create_owned):
            return None
        return planned_id(session.id)

    def _new_planned_id(self, session: Session) -> str | None:
        allocator = getattr(self._sandbox_cls, "plan_owned_id", None)
        if callable(allocator):
            return allocator(session.id)
        return self._planned_id(session)

    def _is_owned_id(self, session: Session, sandbox_id: str) -> bool:
        verifier = getattr(self._sandbox_cls, "is_owned_id", None)
        if callable(verifier):
            return bool(verifier(sandbox_id, session.id))
        return self._planned_id(session) == sandbox_id

    async def _destroy_exact_planned_id(
        self,
        session: Session,
        sandbox_id: str,
    ) -> bool | None:
        """Use provider-level cleanup when the deterministic ID is provable."""

        if not self._is_owned_id(session, sandbox_id):
            return None
        destroy_owned = getattr(
            self._sandbox_cls, "destroy_owned_by_id", None
        )
        if not callable(destroy_owned):
            return None
        return await destroy_owned(sandbox_id, session.id) is True

    async def _reconcile_failed_prepublished_create(
        self,
        session: Session,
        sandbox_id: str,
    ) -> None:
        """Remove networks/container left by a failed deterministic create."""

        async def state_machine() -> None:
            claim_state = await self._cleanup_claim_state(session, sandbox_id)
            if claim_state == "adopted":
                return
            try:
                destroyed = await self._destroy_exact_planned_id(
                    session, sandbox_id
                )
            except Exception:
                destroyed = False
            if destroyed is not True:
                # Retain the durable tombstone. A new lease owner will resume
                # exact cleanup instead of adopting this generation.
                return
            session.sandbox_id = None
            session.sandbox_provider = None
            if claim_state == "claimed":
                await self._finish_destroy(
                    session,
                    expected_sandbox_id=sandbox_id,
                    expected_task_id=session.task_id,
                    expected_task_sandbox_id=session.task_sandbox_id,
                    expected_sandbox_provider=self._provider_name,
                )

        state_task = asyncio.create_task(state_machine())
        await self._await_task_to_known_outcome(state_task)

    async def _get_persisted_sandbox(
        self, session: Session
    ) -> Sandbox | None:
        if not session.sandbox_id:
            return None
        get_owned = getattr(self._sandbox_cls, "get_owned", None)
        if self._is_owned_id(session, session.sandbox_id) and callable(get_owned):
            return await get_owned(session.sandbox_id, session.id)
        return await self._sandbox_cls.get(session.sandbox_id)

    async def _create_prepublished_sandbox(
        self,
        session: Session,
        planned_id: str,
    ) -> Sandbox:
        """Persist a deterministic cleanup pointer before provider mutation."""

        previous_id = session.sandbox_id
        previous_provider = session.sandbox_provider
        session.sandbox_id = planned_id
        session.sandbox_provider = self._provider_name
        try:
            await self._persist(
                session,
                expected_sandbox_id=previous_id,
                expected_sandbox_provider=previous_provider,
            )
        except BaseException:
            # No provider mutation has started, so reverting only the in-memory
            # view is safe. An ambiguous write may leave a harmless pointer to
            # an exact deterministic name that does not exist yet.
            session.sandbox_id = previous_id
            session.sandbox_provider = previous_provider
            raise

        create_owned_with_id = getattr(
            self._sandbox_cls,
            "create_owned_with_id",
            None,
        )
        create_owned = getattr(self._sandbox_cls, "create_owned")
        try:
            if callable(create_owned_with_id):
                sandbox = await create_owned_with_id(session.id, planned_id)
            else:
                sandbox = await create_owned(session.id)
        except BaseException as exc:
            if (
                isinstance(exc, asyncio.CancelledError)
                and current_lifecycle_task_lost_lease()
            ):
                # The pointer was published before provider I/O. A new lease
                # owner may already have adopted this exact generation, so the
                # fenced old owner must not perform provider cleanup.
                raise
            await self._reconcile_failed_prepublished_create(
                session, planned_id
            )
            raise
        if sandbox.id != planned_id:
            # First-party Docker implementations cannot reach this branch.
            # Retain the actual pointer before raising so an implementation
            # bug cannot silently orphan a differently named resource.
            session.sandbox_id = sandbox.id
            await self._persist(
                session,
                expected_sandbox_id=planned_id,
                expected_sandbox_provider=self._provider_name,
            )
            raise SandboxProvisioningError(
                sandbox.id,
                "Sandbox provider returned an ID different from its prepublished ID",
            )
        return sandbox

    async def ensure_locked(self, session: Session) -> Sandbox:
        if session.deleting:
            raise SandboxProvisioningError(
                session.sandbox_id or "",
                "Session deletion is in progress",
            )
        if session.sandbox_destroying:
            # A provider call may have timed out or the previous lease owner
            # may have crashed after publishing the durable deletion claim.
            # Under the new session lease, finish that exact generation before
            # allocating another one. Generation-scoped provider names keep a
            # late old delete from touching the replacement.
            await self.destroy_locked(session, preserve_task=True)
        if session.sandbox_provider not in (None, self._provider_name):
            raise SandboxProvisioningError(
                session.sandbox_id or "",
                "Session sandbox belongs to a different provider",
            )
        legacy_sandbox = await self._adopt_legacy_if_exact(session)
        if legacy_sandbox is not None:
            return legacy_sandbox
        sandbox = await self._get_persisted_sandbox(session)
        if sandbox is not None:
            return sandbox

        if session.sandbox_id:
            previous_sandbox_id = session.sandbox_id
            previous_task_id = session.task_id
            previous_task_sandbox_id = session.task_sandbox_id
            previous_provider = session.sandbox_provider
            await self._claim_destroy(session)
            exact_destroyed = await self._destroy_exact_planned_id(
                session,
                session.sandbox_id,
            )
            if exact_destroyed is False:
                raise SandboxProvisioningError(
                    session.sandbox_id,
                    "Previous sandbox creation is still indeterminate",
                )
            if exact_destroyed in (True, None):
                session.sandbox_id = None
                session.sandbox_provider = None
                await self._finish_destroy(
                    session,
                    expected_sandbox_id=previous_sandbox_id,
                    expected_task_id=previous_task_id,
                    expected_task_sandbox_id=previous_task_sandbox_id,
                    expected_sandbox_provider=previous_provider,
                )

        planned_id = self._new_planned_id(session)
        if planned_id is not None:
            # Docker names are deterministic and ownership is written before
            # ``containers.run``. A timeout/cancellation can therefore return
            # promptly while the retained worker finishes; the next call
            # reconciles this exact durable ID instead of allocating another.
            return await self._create_prepublished_sandbox(
                session, planned_id
            )

        # Compatibility path for non-mutating/custom passthrough providers
        # that do not expose deterministic prepublication.
        try:
            sandbox = await self._sandbox_cls.create()
        except BaseException as exc:
            # ``asyncio.to_thread`` cancellation cannot stop a Docker create
            # that has already entered the SDK. Providers attach the
            # deterministic cleanup ID to both provisioning failures and
            # cancellation so ownership is durable before control unwinds.
            sandbox_id = getattr(exc, "sandbox_id", None)
            if sandbox_id:
                session.sandbox_id = sandbox_id
                session.sandbox_provider = self._provider_name
                await self._persist_failed_create_ownership(
                    session, sandbox_id
                )
            raise
        session.sandbox_id = sandbox.id
        session.sandbox_provider = self._provider_name
        try:
            await self._persist(
                session,
                expected_sandbox_id=None,
                expected_sandbox_provider=None,
            )
        except BaseException:
            await self._rollback_unpublished_sandbox(session, sandbox)
            raise
        return sandbox

    async def terminate_shell_processes_locked(self, session: Session) -> bool:
        """Bound the complete lookup/cleanup/fallback lifecycle."""

        try:
            async with asyncio.timeout(self._SHELL_CLEANUP_TIMEOUT_SECONDS):
                return await self._terminate_shell_processes_locked(session)
        except TimeoutError as exc:
            raise SandboxUnavailableError(
                "Sandbox shell cleanup exceeded its safety deadline; retry stop"
            ) from exc

    async def _terminate_shell_processes_locked(
        self, session: Session
    ) -> bool:
        """Stop every shell only for an owner-verified private sandbox.

        A configured fixed development sandbox is deliberately shared by many
        logical sessions. Its handle reports ``SHARED`` and is left untouched;
        an unverified scope fails closed instead of risking another session's
        processes.
        """

        if session.sandbox_provider not in (None, self._provider_name):
            raise SandboxProvisioningError(
                session.sandbox_id or "",
                "Session sandbox belongs to a different provider",
            )
        if session.sandbox_destroying:
            await self.destroy_locked(session, preserve_task=True)
            return True
        if not session.sandbox_id:
            return True

        legacy_sandbox = await self._adopt_legacy_if_exact(session)
        if legacy_sandbox is not None:
            await self._close_sandbox_handle(legacy_sandbox)

        sandbox = await self._get_persisted_sandbox(session)
        if sandbox is None:
            return True
        try:
            scope = getattr(
                sandbox,
                "shell_process_scope",
                SandboxShellProcessScope.UNKNOWN,
            )
            if scope == SandboxShellProcessScope.SHARED:
                return False
            if scope != SandboxShellProcessScope.EXCLUSIVE:
                raise SandboxProvisioningError(
                    session.sandbox_id,
                    "Sandbox shell process ownership could not be verified",
                )
            cleanup = getattr(sandbox, "kill_all_shell_processes", None)
            if not callable(cleanup):
                raise SandboxProvisioningError(
                    session.sandbox_id,
                    "Sandbox does not support shell process cleanup",
                )
            try:
                result = await cleanup()
            except SandboxShellCleanupUnsupportedError:
                # A pre-deployment runtime may not yet expose kill-all. Because
                # this handle was owner-verified and exclusive, deleting this
                # exact generation is the only compatible fallback that cannot
                # kill another session's processes.
                logger.warning(
                    "Owned sandbox lacks scoped shell cleanup; deleting exact "
                    "runtime generation for compatibility: session_id=%s "
                    "sandbox_id=%s",
                    session.id,
                    session.sandbox_id,
                )
                await self.destroy_locked(session, preserve_task=True)
                return True
            if getattr(result, "success", None) is not True:
                raise SandboxUnavailableError(
                    "Sandbox did not confirm shell process cleanup"
                )
            return True
        finally:
            await self._close_sandbox_handle(sandbox)

    async def destroy_locked(
        self, session: Session, *, preserve_task: bool = False
    ) -> None:
        if session.sandbox_provider not in (None, self._provider_name):
            raise SandboxProvisioningError(
                session.sandbox_id or "",
                "Session sandbox belongs to a different provider",
            )
        legacy_sandbox = await self._adopt_legacy_if_exact(session)
        if not session.sandbox_id:
            if session.sandbox_provider is not None:
                previous_provider = session.sandbox_provider
                session.sandbox_provider = None
                if not preserve_task:
                    session.task_id = None
                    session.task_sandbox_id = None
                await self._persist(
                    session,
                    expected_sandbox_id=None,
                    expected_sandbox_provider=previous_provider,
                )
            return
        previous_sandbox_id = session.sandbox_id
        previous_task_id = session.task_id
        previous_task_sandbox_id = session.task_sandbox_id
        previous_provider = session.sandbox_provider
        await self._claim_destroy(session)
        exact_cleanup_available = (
            self._is_owned_id(session, session.sandbox_id)
            and callable(
                getattr(self._sandbox_cls, "destroy_owned_by_id", None)
            )
        )
        sandbox = legacy_sandbox
        if sandbox is None and not exact_cleanup_available:
            sandbox = await self._get_persisted_sandbox(session)
        exact_destroyed = await self._destroy_exact_planned_id(
            session, session.sandbox_id
        )
        if exact_destroyed is False:
            raise SandboxUnavailableError(
                "Sandbox provider did not confirm deletion"
            )
        if exact_destroyed is None:
            if sandbox is not None and await sandbox.destroy() is not True:
                raise SandboxUnavailableError(
                    "Sandbox provider did not confirm deletion"
                )
        session.sandbox_id = None
        session.sandbox_provider = None
        if not preserve_task:
            session.task_id = None
            session.task_sandbox_id = None
        await self._finish_destroy(
            session,
            expected_sandbox_id=previous_sandbox_id,
            expected_task_id=previous_task_id,
            expected_task_sandbox_id=previous_task_sandbox_id,
            expected_sandbox_provider=previous_provider,
        )
