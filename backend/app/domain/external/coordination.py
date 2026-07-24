"""Cross-process coordination contracts for runtime lifecycle mutations."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar


T = TypeVar("T")


_LEASE_LOST_TASK_ATTRIBUTE = "_ai_manus_runtime_lifecycle_lease_lost"


def mark_lifecycle_task_lease_lost(task: asyncio.Future[object]) -> None:
    """Fence a cancelled operation from provider-side rollback."""

    setattr(task, _LEASE_LOST_TASK_ATTRIBUTE, True)


def current_lifecycle_task_lost_lease() -> bool:
    """Return whether the current task was cancelled after losing its lease."""

    task = asyncio.current_task()
    return bool(task and getattr(task, _LEASE_LOST_TASK_ATTRIBUTE, False))


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
