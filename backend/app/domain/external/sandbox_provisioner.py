"""Lifecycle boundary for persisted sandbox ownership.

Callers must already hold the session's distributed lifecycle lease. Keeping
the lease outside this interface avoids attempting to re-enter the same
non-reentrant Redis lease from create/delete paths.
"""

from typing import Protocol

from app.domain.external.sandbox import Sandbox
from app.domain.models.session import Session


class SandboxProvisioningRequiredError(RuntimeError):
    """A worker found an exact-missing sandbox and may not allocate one."""


class SandboxProvisioner(Protocol):
    async def ensure_locked(self, session: Session) -> Sandbox:
        """Return an owned live handle, provisioning only under the caller's lease."""
        ...

    async def destroy_locked(self, session: Session) -> None:
        """Confirm provider deletion and persist cleanup before returning."""
        ...

    async def terminate_shell_processes_locked(self, session: Session) -> bool:
        """Terminate shell processes only after proving exclusive ownership.

        Returns ``False`` for an explicitly shared sandbox where broad process
        cleanup must be skipped, and ``True`` when no process can remain.
        """
        ...
