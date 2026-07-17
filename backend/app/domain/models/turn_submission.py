"""Durable acceptance state for one logical user chat turn.

``submission_id`` is the logical turn identifier supplied by the client.  It
must never be replaced by a Redis Stream entry ID; Redis IDs are transport
cursors only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TurnSubmissionState(str, Enum):
    PENDING = "pending"
    ENQUEUED = "enqueued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_UNKNOWN = "failed_unknown"
    CANCELLED = "cancelled"


TERMINAL_TURN_STATES = frozenset(
    {
        TurnSubmissionState.COMPLETED,
        TurnSubmissionState.FAILED,
        TurnSubmissionState.FAILED_UNKNOWN,
        TurnSubmissionState.CANCELLED,
    }
)

ACTIVE_TURN_STATES = frozenset(
    {
        TurnSubmissionState.PENDING,
        TurnSubmissionState.ENQUEUED,
        TurnSubmissionState.RUNNING,
    }
)


class TurnSubmission(BaseModel):
    """Mongo-backed source of truth for one accepted chat submission."""

    session_id: str
    submission_id: str
    user_id: str
    agent_id: str
    task_id: Optional[str] = None
    request_hash: str
    input_json: str = Field(repr=False)
    # Captured once, while the session lifecycle lease still exposes whether
    # this user message is answering a prior ``message_ask_user`` wait.  The
    # legacy Session status is only a projection of durable turn activity and
    # becomes RUNNING before the worker enters the flow, so it cannot safely
    # carry this resume intent to another process.
    resumes_waiting: bool = False
    state: TurnSubmissionState = TurnSubmissionState.PENDING
    stream_id: Optional[str] = None
    claim_owner: Optional[str] = None
    claim_until: Optional[datetime] = None
    attempt: int = 0
    # Allocated atomically in Mongo for strict per-turn output replay order.
    output_sequence: int = 0
    terminal_event_id: Optional[str] = None
    terminal_error: Optional[str] = None
    # Set only after a terminal state. Mongo's TTL index preserves active work
    # indefinitely while bounding the durable idempotency/replay window.
    expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_TURN_STATES

    @property
    def quota_key(self) -> str:
        return f"{self.session_id}:{self.submission_id}"


class TurnClaimDecision(str, Enum):
    EXECUTE = "execute"
    ACK = "ack"
    RETRY = "retry"


class TurnClaimResult(BaseModel):
    decision: TurnClaimDecision
    turn: TurnSubmission


class TurnSubmissionConflictError(RuntimeError):
    """The same submission UUID was reused for a different canonical input."""


class TurnSubmissionCapacityError(RuntimeError):
    """A durable per-session or per-user active-turn limit was reached."""


class TurnSubmissionUnavailableError(RuntimeError):
    """Durable turn acceptance/claim state could not be confirmed."""
