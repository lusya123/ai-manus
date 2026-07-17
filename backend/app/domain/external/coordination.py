"""Cross-process coordination contracts for session lifecycle mutations."""

from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar


T = TypeVar("T")


class SessionLifecycleLeaseError(RuntimeError):
    """Base error for a session lifecycle operation that could not run safely."""


class SessionLifecycleLeaseBusyError(SessionLifecycleLeaseError):
    """Another replica retained the lease beyond the bounded wait period."""


class SessionLifecycleLeaseUnavailableError(SessionLifecycleLeaseError):
    """The coordination backend failed or lease ownership was lost."""


class SessionLifecycleLease(Protocol):
    """Run one session mutation while holding a renewable distributed lease."""

    async def run_exclusive(
        self,
        session_id: str,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        """Run ``operation`` only while this caller owns the session lease."""
        ...
