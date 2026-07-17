"""Durable cost-ledger contract for externally billed AgentBay sessions.

The ledger deliberately distinguishes normal idempotent outcomes from backend
failures.  Callers must inspect the returned outcome; a stale or retryable
operation is never equivalent to a successful reservation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Protocol, Sequence


class AgentBayReservationPhase(str, Enum):
    RESERVED = "reserved"
    PROVISIONED = "provisioned"


class AgentBayBootstrapState(str, Enum):
    REQUIRED = "required"
    RECONCILING = "reconciling"
    READY = "ready"


class AgentBayQuotaOutcome(str, Enum):
    LEDGER_CREATED = "ledger_created"
    LEDGER_EXISTS = "ledger_exists"
    RECONCILING = "reconciling"
    ALREADY_RECONCILING = "already_reconciling"
    RECONCILED = "reconciled"
    RESERVED = "reserved"
    EXISTING_RESERVED = "existing_reserved"
    EXISTING_PROVISIONED = "existing_provisioned"
    ADOPTED = "adopted"
    PROVISIONED = "provisioned"
    ALREADY_PROVISIONED = "already_provisioned"
    REPLACED = "replaced"
    ALREADY_REPLACED = "already_replaced"
    RELEASED = "released"
    ALREADY_RELEASED = "already_released"
    STALE_OPERATION = "stale_operation"
    RETRYABLE = "retryable"
    RECONFIGURED = "reconfigured"
    ALREADY_CONFIGURED = "already_configured"


class AgentBayQuotaScope(str, Enum):
    GLOBAL = "global"
    USER = "user"


class AgentBayQuotaError(RuntimeError):
    """Base class for ledger failures that must fail provisioning closed."""


class AgentBayQuotaUnavailableError(AgentBayQuotaError):
    """MongoDB could not establish the authoritative postcondition."""


class AgentBayQuotaNotInitializedError(AgentBayQuotaError):
    """The fixed global ledger document has not been created yet."""


class AgentBayQuotaConfigurationError(AgentBayQuotaError):
    """A replica's schema, deployment, version, or caps do not match MongoDB."""


class AgentBayQuotaBootstrapError(AgentBayQuotaError):
    """Provisioning was attempted before an explicit inventory reconcile."""

    def __init__(self, state: AgentBayBootstrapState) -> None:
        self.state = state
        super().__init__(
            "AgentBay quota inventory is not ready; provisioning was refused"
        )


class AgentBayQuotaExceededError(AgentBayQuotaError):
    """The authoritative global or per-user limit has been reached."""

    def __init__(self, scope: AgentBayQuotaScope) -> None:
        self.scope = scope
        super().__init__(f"The AgentBay {scope.value} session limit has been reached")


class AgentBayQuotaInconsistentError(AgentBayQuotaError):
    """A logical session, operation, provider, or counter is inconsistent."""


@dataclass(frozen=True, slots=True)
class AgentBayQuotaInventoryEntry:
    """One provider-backed session supplied by an explicit reconciliation."""

    session_id: str
    user_id: str
    operation_id: str
    provider_id: str


@dataclass(frozen=True, slots=True)
class AgentBayQuotaReservation:
    """Stored reservation view.

    User and logical-session identifiers remain one-way digests.  The stable
    operation ID and provider cleanup handle remain available to recovery code.
    """

    session_key: str
    user_key: str
    operation_key: str
    operation_id: str
    phase: AgentBayReservationPhase
    provider_id: Optional[str] = None
    provider_key: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True, slots=True)
class AgentBayQuotaResult:
    outcome: AgentBayQuotaOutcome
    reservation: Optional[AgentBayQuotaReservation] = None
    bootstrap_state: Optional[AgentBayBootstrapState] = None
    revision: Optional[int] = None
    total: Optional[int] = None


@dataclass(frozen=True, slots=True)
class AgentBayQuotaSnapshot:
    bootstrap_state: AgentBayBootstrapState
    revision: int
    total: int
    reservations: tuple[AgentBayQuotaReservation, ...]


class AgentBayQuotaLedger(Protocol):
    async def snapshot(self) -> AgentBayQuotaSnapshot:
        """Read and validate the complete bounded global inventory."""
        ...

    async def get_reservation_for_cleanup(
        self, session_id: str, user_id: str
    ) -> Optional[AgentBayQuotaReservation]:
        """Recover an exact cleanup handle without requiring config parity."""
        ...

    async def ensure_ledger(self) -> AgentBayQuotaResult:
        """Create the fixed document in ``required`` state, never ready."""
        ...

    async def begin_reconciliation(self) -> AgentBayQuotaResult:
        """Block new reservations and return the revision to reconcile."""
        ...

    async def reconcile_inventory(
        self,
        inventory: Sequence[AgentBayQuotaInventoryEntry],
        *,
        expected_revision: int,
    ) -> AgentBayQuotaResult:
        """Atomically replace the full inventory and mark it ready."""
        ...

    async def reserve(
        self, session_id: str, user_id: str, operation_id: str
    ) -> AgentBayQuotaResult:
        ...

    async def adopt_existing(
        self,
        session_id: str,
        user_id: str,
        operation_id: str,
        provider_id: str,
    ) -> AgentBayQuotaResult:
        """Record one known provider session while reconciliation is blocked."""
        ...

    async def mark_provisioned(
        self,
        session_id: str,
        user_id: str,
        operation_id: str,
        provider_id: str,
    ) -> AgentBayQuotaResult:
        ...

    async def replace_operation(
        self,
        session_id: str,
        user_id: str,
        expected_operation_id: str,
        replacement_operation_id: str,
        *,
        expected_provider_id: Optional[str] = None,
    ) -> AgentBayQuotaResult:
        ...

    async def release(
        self,
        session_id: str,
        user_id: str,
        operation_id: str,
        *,
        provider_id: Optional[str] = None,
    ) -> AgentBayQuotaResult:
        ...
