"""Repository contract for durable, idempotent chat-turn acceptance."""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol, Tuple

from app.domain.models.turn_submission import (
    TurnClaimResult,
    TurnSubmission,
    TurnSubmissionState,
)
from app.domain.models.event import BaseEvent


class TurnSubmissionRepository(Protocol):
    async def accept(
        self, turn: TurnSubmission
    ) -> Tuple[TurnSubmission, bool]:
        """Insert once and return ``(persisted_turn, was_created)``."""
        ...

    async def find(
        self, session_id: str, submission_id: str
    ) -> Optional[TurnSubmission]:
        ...

    async def list_active(
        self, session_id: str, *, user_id: Optional[str] = None
    ) -> list[TurnSubmission]:
        """Return nonterminal turns in durable submission order."""
        ...

    async def mark_enqueued(
        self,
        session_id: str,
        submission_id: str,
        *,
        task_id: str,
        stream_id: str,
    ) -> TurnSubmission:
        """CAS ``pending -> enqueued``; otherwise return current state."""
        ...

    async def claim_for_execution(
        self,
        session_id: str,
        submission_id: str,
        *,
        task_id: str,
        owner: str,
        claim_until: datetime,
    ) -> TurnClaimResult:
        """Claim an enqueued turn, or classify a duplicate delivery."""
        ...

    async def renew_claim(
        self,
        session_id: str,
        submission_id: str,
        *,
        owner: str,
        claim_until: datetime,
    ) -> bool:
        ...

    async def mark_terminal(
        self,
        session_id: str,
        submission_id: str,
        *,
        owner: str,
        state: TurnSubmissionState,
        terminal_event_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> bool:
        """Persist terminal state before acknowledging Redis input."""
        ...

    async def mark_unclaimed_terminal(
        self,
        session_id: str,
        submission_id: str,
        *,
        state: TurnSubmissionState,
        error: str,
    ) -> bool:
        """Terminalize a pending/enqueued turn after a known dispatch failure."""
        ...

    async def fail_enqueued_for_task(
        self,
        session_id: str,
        *,
        user_id: str,
        task_id: str,
        error: str,
    ) -> int:
        """Fail only unclaimed turns bound to one worker task.

        Used when a remote worker cannot construct its runner. Running turns
        and turns belonging to another task must never be changed here.
        """
        ...

    async def recover_factory_failure_for_task(
        self,
        session_id: str,
        *,
        user_id: str,
        task_id: str,
        error: str,
    ) -> bool:
        """Recover exact work after runner construction fails.

        Unclaimed work is failed, expired running claims become
        ``failed_unknown``, and ``True`` is returned only when no live running
        claim for this task remains.  A worker must not acknowledge its Redis
        lifecycle complete while this method returns ``False``.
        """
        ...

    async def cancel_outstanding(
        self, session_id: str, *, user_id: Optional[str] = None
    ) -> int:
        """Compatibility alias that cancels only pending/enqueued turns."""
        ...

    async def cancel_queued(
        self,
        session_id: str,
        *,
        user_id: Optional[str] = None,
        exclude_submission_id: Optional[str] = None,
    ) -> int:
        """Cancel pending/enqueued turns after task execution has stopped.

        ``exclude_submission_id`` lets lifecycle recovery retire an obsolete
        task stream while preserving the newly accepted, not-yet-enqueued
        turn that will be dispatched to the replacement task.
        """
        ...

    async def count_running(
        self, session_id: str, *, user_id: Optional[str] = None
    ) -> int:
        ...

    async def append_output(
        self, session_id: str, submission_id: str, event: BaseEvent
    ) -> BaseEvent:
        """Persist one bounded output event idempotently in the turn outbox."""
        ...

    async def list_outputs(
        self, session_id: str, submission_id: str
    ) -> list[BaseEvent]:
        ...

    async def update_output_transport_cursor(
        self,
        session_id: str,
        submission_id: str,
        event_id: str,
        transport_id: str,
    ) -> None:
        ...
