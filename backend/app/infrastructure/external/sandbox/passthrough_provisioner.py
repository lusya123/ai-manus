"""Unbilled sandbox lifecycle used by Docker and fixed local sandboxes."""

from typing import Type

from app.domain.external.sandbox import Sandbox, SandboxProvisioningError
from app.domain.external.sandbox_provisioner import SandboxProvisioner
from app.domain.models.session import Session
from app.domain.repositories.session_repository import SessionRepository


class PassthroughSandboxProvisioner(SandboxProvisioner):
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

    async def _persist(self, session: Session) -> None:
        update = getattr(
            self._session_repository, "update_runtime_ownership", None
        )
        if callable(update):
            updated = await update(
                session.id,
                session.sandbox_id,
                session.task_id,
                session.sandbox_provider,
            )
            if updated is False:
                raise RuntimeError("Session disappeared during sandbox lifecycle")
            return
        await self._session_repository.save(session)

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
        await self._persist(session)
        return sandbox

    async def ensure_locked(self, session: Session) -> Sandbox:
        if session.sandbox_provider not in (None, self._provider_name):
            raise SandboxProvisioningError(
                session.sandbox_id or "",
                "Session sandbox belongs to a different provider",
            )
        legacy_sandbox = await self._adopt_legacy_if_exact(session)
        if legacy_sandbox is not None:
            return legacy_sandbox
        sandbox = (
            await self._sandbox_cls.get(session.sandbox_id)
            if session.sandbox_id
            else None
        )
        if sandbox is not None:
            return sandbox
        try:
            sandbox = await self._sandbox_cls.create()
        except SandboxProvisioningError as exc:
            session.sandbox_id = exc.sandbox_id
            session.sandbox_provider = self._provider_name
            await self._persist(session)
            raise
        session.sandbox_id = sandbox.id
        session.sandbox_provider = self._provider_name
        try:
            await self._persist(session)
        except BaseException:
            try:
                destroyed = await sandbox.destroy()
            except Exception:
                destroyed = False
            if not destroyed:
                # Retain the only cleanup pointer if the provider could not
                # confirm rollback. A second persistence attempt may recover
                # from a transient first failure.
                await self._persist(session)
            raise
        return sandbox

    async def destroy_locked(self, session: Session) -> None:
        if session.sandbox_provider not in (None, self._provider_name):
            raise SandboxProvisioningError(
                session.sandbox_id or "",
                "Session sandbox belongs to a different provider",
            )
        legacy_sandbox = await self._adopt_legacy_if_exact(session)
        if not session.sandbox_id:
            if session.sandbox_provider is not None:
                session.sandbox_provider = None
                session.task_id = None
                await self._persist(session)
            return
        sandbox = (
            legacy_sandbox
            if legacy_sandbox is not None
            else await self._sandbox_cls.get(session.sandbox_id)
        )
        if sandbox is not None and await sandbox.destroy() is not True:
            raise RuntimeError("Sandbox provider did not confirm deletion")
        session.sandbox_id = None
        session.sandbox_provider = None
        session.task_id = None
        await self._persist(session)
