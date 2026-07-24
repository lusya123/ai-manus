"""Mongo implementation of durable chat-turn acceptance and claiming."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Optional, Tuple
from pydantic import TypeAdapter

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.domain.models.turn_submission import (
    ACTIVE_TURN_STATES,
    TERMINAL_TURN_STATES,
    TurnClaimDecision,
    TurnClaimResult,
    TurnSubmission,
    TurnSubmissionCapacityError,
    TurnSubmissionConflictError,
    TurnSubmissionState,
    TurnSubmissionUnavailableError,
)
from app.domain.repositories.turn_submission_repository import (
    TurnSubmissionRepository,
)
from app.infrastructure.models.documents import (
    TurnQuotaDocument,
    TurnSubmissionDocument,
    TurnOutputEventDocument,
)
from app.domain.models.event import AgentEvent, BaseEvent, ErrorEvent

logger = logging.getLogger(__name__)


def _state_values(states) -> list[str]:
    return [state.value if hasattr(state, "value") else str(state) for state in states]


class MongoTurnSubmissionRepository(TurnSubmissionRepository):
    """Cross-replica turn state with fail-closed active-turn reservations.

    Each user owns one quota document whose reservations include ``session_id``.
    A single conditional ``$push`` enforces both the per-user total and the
    per-session subset atomically across replicas. Old orphan reservations are
    pruned only after a grace window.
    """

    _ORPHAN_RESERVATION_GRACE = timedelta(minutes=5)

    def __init__(
        self,
        *,
        max_active_per_session: Optional[int] = None,
        max_active_per_user: Optional[int] = None,
        max_payload_bytes: Optional[int] = None,
        terminal_retention_days: Optional[int] = None,
    ) -> None:
        settings = get_settings()
        self._max_active_per_session = max(
            1,
            int(
                max_active_per_session
                if max_active_per_session is not None
                else settings.chat_turn_max_active_per_session
            ),
        )
        self._max_active_per_user = max(
            1,
            int(
                max_active_per_user
                if max_active_per_user is not None
                else settings.chat_turn_max_active_per_user
            ),
        )
        self._max_payload_bytes = max(
            1,
            int(
                max_payload_bytes
                if max_payload_bytes is not None
                else settings.chat_turn_max_payload_bytes
            ),
        )
        self._terminal_retention = timedelta(
            days=max(
                1,
                int(
                    terminal_retention_days
                    if terminal_retention_days is not None
                    else settings.chat_turn_terminal_retention_days
                ),
            )
        )

    @staticmethod
    def _turn_collection():
        return TurnSubmissionDocument.get_pymongo_collection()

    @staticmethod
    def _quota_collection():
        return TurnQuotaDocument.get_pymongo_collection()

    @staticmethod
    def _output_collection():
        return TurnOutputEventDocument.get_pymongo_collection()

    @staticmethod
    def _to_domain(document: Optional[dict[str, Any]]) -> Optional[TurnSubmission]:
        if not document:
            return None
        return TurnSubmission.model_validate(
            {key: value for key, value in document.items() if key != "_id"}
        )

    async def list_active(
        self, session_id: str, *, user_id: Optional[str] = None
    ) -> list[TurnSubmission]:
        query: dict[str, Any] = {
            "session_id": session_id,
            "state": {"$in": _state_values(ACTIVE_TURN_STATES)},
        }
        if user_id is not None:
            query["user_id"] = user_id
        try:
            documents = await self._turn_collection().find(query).sort(
                [("created_at", ASCENDING), ("submission_id", ASCENDING)]
            ).to_list(length=None)
            return [
                turn
                for document in documents
                if (turn := self._to_domain(document)) is not None
            ]
        except Exception as exc:
            raise TurnSubmissionUnavailableError(
                "Could not list active durable turns"
            ) from exc

    @staticmethod
    def _validate_existing(
        existing: TurnSubmission, candidate: TurnSubmission
    ) -> TurnSubmission:
        if (
            existing.user_id != candidate.user_id
            or existing.agent_id != candidate.agent_id
            or existing.request_hash != candidate.request_hash
        ):
            raise TurnSubmissionConflictError(
                "submission_id is already bound to a different chat request"
            )
        return existing

    @staticmethod
    def _reservation(turn: TurnSubmission, now: datetime) -> dict[str, Any]:
        return {
            "key": turn.quota_key,
            "session_id": turn.session_id,
            "submission_id": turn.submission_id,
            "reserved_at": now,
        }

    async def _release_scope(self, scope_key: str, quota_key: str) -> None:
        await self._quota_collection().update_one(
            {"scope_key": scope_key},
            {
                "$pull": {"active_turns": {"key": quota_key}},
                "$set": {"updated_at": datetime.now(UTC)},
            },
        )

    async def _release_turn(self, turn: TurnSubmission) -> None:
        await self._release_scope(f"user:{turn.user_id}", turn.quota_key)

    async def _expire_outputs(self, turn: TurnSubmission) -> None:
        if turn.expires_at is None:
            raise TurnSubmissionUnavailableError(
                "Terminal turn is missing its retention expiry"
            )
        await self._output_collection().update_many(
            {
                "session_id": turn.session_id,
                "submission_id": turn.submission_id,
            },
            {"$set": {"expires_at": turn.expires_at}},
        )

    async def _repair_terminal_postconditions(self, turn: TurnSubmission) -> None:
        """Idempotently release quota and TTL every output before Redis ACK."""
        await self._release_turn(turn)
        await self._expire_outputs(turn)

    @staticmethod
    def _bounded_output(event: BaseEvent) -> BaseEvent:
        max_bytes = max(1024, int(get_settings().session_event_max_bytes))
        if len(event.model_dump_json().encode("utf-8")) <= max_bytes:
            return event
        return ErrorEvent(
            id=event.id,
            turn_id=event.turn_id,
            timestamp=event.timestamp,
            error=(
                "Event details were omitted because the serialized event "
                f"exceeded the {max_bytes}-byte persistence limit"
            ),
        )

    async def append_output(
        self, session_id: str, submission_id: str, event: BaseEvent
    ) -> BaseEvent:
        bounded = self._bounded_output(event)
        if bounded.turn_id != submission_id:
            raise ValueError("Output event turn_id does not match submission_id")
        try:
            # Return an already-persisted event without consuming another
            # sequence. The unique index remains the final concurrency arbiter.
            existing = await self._output_collection().find_one(
                {
                    "session_id": session_id,
                    "submission_id": submission_id,
                    "event_id": bounded.id,
                }
            )
            if existing:
                return TypeAdapter(AgentEvent).validate_python(existing["event"])

            turn_document = await self._turn_collection().find_one_and_update(
                {
                    "session_id": session_id,
                    "submission_id": submission_id,
                },
                {"$inc": {"output_sequence": 1}},
                return_document=ReturnDocument.AFTER,
            )
            turn = self._to_domain(turn_document)
            if turn is None:
                raise TurnSubmissionUnavailableError(
                    "Cannot append output for a missing turn"
                )
            document = {
                "session_id": session_id,
                "submission_id": submission_id,
                "event_id": bounded.id,
                "sequence": turn.output_sequence,
                "event": bounded.model_dump(),
                "created_at": bounded.timestamp,
                "expires_at": turn.expires_at if turn.is_terminal else None,
            }
            try:
                await self._output_collection().insert_one(document)
                return bounded
            except DuplicateKeyError:
                existing = await self._output_collection().find_one(
                    {
                        "session_id": session_id,
                        "submission_id": submission_id,
                        "event_id": bounded.id,
                    }
                )
                if not existing:
                    raise TurnSubmissionUnavailableError(
                        "Output idempotency conflict could not be confirmed"
                    )
                return TypeAdapter(AgentEvent).validate_python(existing["event"])
        except (TurnSubmissionUnavailableError, ValueError):
            raise
        except Exception as exc:
            raise TurnSubmissionUnavailableError(
                "Durable turn output is unavailable"
            ) from exc

    async def list_outputs(
        self, session_id: str, submission_id: str
    ) -> list[BaseEvent]:
        try:
            cursor = self._output_collection().find(
                {"session_id": session_id, "submission_id": submission_id}
            ).sort([("sequence", ASCENDING), ("event_id", ASCENDING)])
            result: list[BaseEvent] = []
            async for document in cursor:
                result.append(
                    TypeAdapter(AgentEvent).validate_python(document["event"])
                )
            return result
        except Exception as exc:
            raise TurnSubmissionUnavailableError(
                "Durable turn output replay is unavailable"
            ) from exc

    async def update_output_transport_cursor(
        self,
        session_id: str,
        submission_id: str,
        event_id: str,
        transport_id: str,
    ) -> None:
        try:
            await self._output_collection().update_one(
                {
                    "session_id": session_id,
                    "submission_id": submission_id,
                    "event_id": event_id,
                },
                {"$set": {"event.transport_id": transport_id}},
            )
        except Exception as exc:
            raise TurnSubmissionUnavailableError(
                "Could not persist output transport cursor"
            ) from exc

    async def _reconcile_scope(self, scope_key: str) -> bool:
        """Remove terminal and old orphan reservations; return whether changed."""
        quota = await self._quota_collection().find_one({"scope_key": scope_key})
        reservations = list((quota or {}).get("active_turns") or [])
        if not reservations:
            return False

        now = datetime.now(UTC)
        removable: list[str] = []
        for reservation in reservations:
            key = reservation.get("key")
            session_id = reservation.get("session_id")
            submission_id = reservation.get("submission_id")
            if not key or not session_id or not submission_id:
                removable.append(key)
                continue
            turn = await self._turn_collection().find_one(
                {
                    "session_id": session_id,
                    "submission_id": submission_id,
                },
                {"state": 1},
            )
            if turn and turn.get("state") in _state_values(TERMINAL_TURN_STATES):
                removable.append(key)
                continue
            reserved_at = reservation.get("reserved_at")
            if (
                turn is None
                and isinstance(reserved_at, datetime)
                and reserved_at <= now - self._ORPHAN_RESERVATION_GRACE
            ):
                removable.append(key)

        removable = [key for key in removable if key]
        if not removable:
            return False
        await self._quota_collection().update_one(
            {"scope_key": scope_key},
            {
                "$pull": {"active_turns": {"key": {"$in": removable}}},
                "$set": {"updated_at": now},
            },
        )
        return True

    async def _reserve_turn(
        self,
        turn: TurnSubmission,
        *,
        allow_reconcile: bool = True,
    ) -> bool:
        """Atomically enforce both user and session limits in one document.

        One user owns one quota document, and every reservation carries its
        session ID. The single conditional ``$push`` therefore checks total
        active turns and the active subset for this session without relying on
        cross-document transactions (which standalone Mongo does not provide).
        """
        scope_key = f"user:{turn.user_id}"
        collection = self._quota_collection()
        now = datetime.now(UTC)
        try:
            await collection.update_one(
                {"scope_key": scope_key},
                {
                    "$setOnInsert": {
                        "scope_key": scope_key,
                        "active_turns": [],
                        "updated_at": now,
                    }
                },
                upsert=True,
            )
        except DuplicateKeyError:
            # Another replica created the bucket after our equality lookup.
            pass

        result = await collection.update_one(
            {
                "scope_key": scope_key,
                "active_turns": {"$not": {"$elemMatch": {"key": turn.quota_key}}},
                "$expr": {
                    "$and": [
                        {
                            "$lt": [
                                {"$size": {"$ifNull": ["$active_turns", []]}},
                                self._max_active_per_user,
                            ]
                        },
                        {
                            "$lt": [
                                {
                                    "$size": {
                                        "$filter": {
                                            "input": {"$ifNull": ["$active_turns", []]},
                                            "as": "active_turn",
                                            "cond": {
                                                "$eq": [
                                                    "$$active_turn.session_id",
                                                    turn.session_id,
                                                ]
                                            },
                                        }
                                    }
                                },
                                self._max_active_per_session,
                            ]
                        },
                    ]
                },
            },
            {
                "$push": {"active_turns": self._reservation(turn, now)},
                "$set": {"updated_at": now},
            },
        )
        if result.modified_count:
            return True

        existing = await collection.find_one(
            {"scope_key": scope_key}, {"active_turns": 1}
        )
        reservations = (existing or {}).get("active_turns") or []
        if any(item.get("key") == turn.quota_key for item in reservations):
            return False
        if allow_reconcile and await self._reconcile_scope(scope_key):
            return await self._reserve_turn(turn, allow_reconcile=False)
        session_active = sum(
            1 for item in reservations if item.get("session_id") == turn.session_id
        )
        scope = (
            "session"
            if session_active >= self._max_active_per_session
            else "user"
        )
        raise TurnSubmissionCapacityError(
            f"Too many active chat turns for {scope}"
        )

    async def find(
        self, session_id: str, submission_id: str
    ) -> Optional[TurnSubmission]:
        try:
            document = await self._turn_collection().find_one(
                {"session_id": session_id, "submission_id": submission_id}
            )
        except Exception as exc:
            raise TurnSubmissionUnavailableError(
                "Durable turn state is unavailable"
            ) from exc
        return self._to_domain(document)

    async def accept(
        self, turn: TurnSubmission
    ) -> Tuple[TurnSubmission, bool]:
        payload_size = len(turn.input_json.encode("utf-8"))
        if payload_size > self._max_payload_bytes:
            raise ValueError(
                f"Serialized chat turn exceeds {self._max_payload_bytes} bytes"
            )

        try:
            existing = await self.find(turn.session_id, turn.submission_id)
            if existing is not None:
                return self._validate_existing(existing, turn), False

            await self._reserve_turn(turn)

            # Raw collection writes keep acceptance to one explicit insert and
            # avoid any ODM save/upsert behavior. The domain model is already
            # fully validated.
            document = turn.model_dump()
            try:
                await self._turn_collection().insert_one(document)
                return turn, True
            except DuplicateKeyError:
                # The unique index is the final cross-replica arbiter. Keep the
                # shared reservations: the winner owns the same quota key.
                existing = await self.find(turn.session_id, turn.submission_id)
                if existing is None:
                    raise TurnSubmissionUnavailableError(
                        "Durable turn acceptance could not be confirmed"
                    )
                return self._validate_existing(existing, turn), False
            except Exception:
                # An acknowledged timeout can mean the insert committed. Do not
                # release reservations when the outcome is unknown; that would
                # permit oversubscription. Reconciliation removes old orphans.
                raise
        except (
            TurnSubmissionCapacityError,
            TurnSubmissionConflictError,
            TurnSubmissionUnavailableError,
            ValueError,
        ):
            raise
        except Exception as exc:
            raise TurnSubmissionUnavailableError(
                "Durable turn acceptance is unavailable"
            ) from exc

    async def mark_enqueued(
        self,
        session_id: str,
        submission_id: str,
        *,
        task_id: str,
        stream_id: str,
    ) -> TurnSubmission:
        now = datetime.now(UTC)
        try:
            document = await self._turn_collection().find_one_and_update(
                {
                    "session_id": session_id,
                    "submission_id": submission_id,
                    "state": TurnSubmissionState.PENDING.value,
                },
                {
                    "$set": {
                        "state": TurnSubmissionState.ENQUEUED.value,
                        "task_id": task_id,
                        "stream_id": stream_id,
                        "updated_at": now,
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
            if document is None:
                document = await self._turn_collection().find_one(
                    {"session_id": session_id, "submission_id": submission_id}
                )
            turn = self._to_domain(document)
            if turn is None:
                raise TurnSubmissionUnavailableError("Accepted turn disappeared")
            return turn
        except TurnSubmissionUnavailableError:
            raise
        except Exception as exc:
            raise TurnSubmissionUnavailableError(
                "Could not confirm queued turn state"
            ) from exc

    async def claim_for_execution(
        self,
        session_id: str,
        submission_id: str,
        *,
        task_id: str,
        owner: str,
        claim_until: datetime,
    ) -> TurnClaimResult:
        now = datetime.now(UTC)
        collection = self._turn_collection()
        try:
            try:
                document = await collection.find_one_and_update(
                    {
                        "session_id": session_id,
                        "submission_id": submission_id,
                        "task_id": task_id,
                        "state": TurnSubmissionState.ENQUEUED.value,
                    },
                    {
                        "$set": {
                            "state": TurnSubmissionState.RUNNING.value,
                            "claim_owner": owner,
                            "claim_until": claim_until,
                            "updated_at": now,
                        },
                        "$inc": {"attempt": 1},
                    },
                    return_document=ReturnDocument.AFTER,
                )
            except Exception as claim_error:
                # A socket timeout or lost Mongo response does not prove that
                # find_one_and_update failed. Re-read the exact owner before
                # allowing cancellation/retry to release this worker. Without
                # this reconciliation a committed RUNNING row could outlive
                # its task forever and pin the session's runtime deletion.
                try:
                    reconciled = self._to_domain(
                        await collection.find_one(
                            {
                                "session_id": session_id,
                                "submission_id": submission_id,
                            }
                        )
                    )
                except Exception as verify_error:
                    raise TurnSubmissionUnavailableError(
                        "Durable execution claim outcome is unknown"
                    ) from verify_error
                if (
                    reconciled is not None
                    and reconciled.state == TurnSubmissionState.RUNNING
                    and reconciled.task_id == task_id
                    and reconciled.claim_owner == owner
                ):
                    return TurnClaimResult(
                        decision=TurnClaimDecision.EXECUTE,
                        turn=reconciled,
                    )
                if (
                    reconciled is not None
                    and reconciled.state in TERMINAL_TURN_STATES
                ):
                    await self._repair_terminal_postconditions(reconciled)
                    return TurnClaimResult(
                        decision=TurnClaimDecision.ACK,
                        turn=reconciled,
                    )
                if (
                    reconciled is not None
                    and reconciled.task_id
                    and reconciled.task_id != task_id
                ):
                    return TurnClaimResult(
                        decision=TurnClaimDecision.ACK,
                        turn=reconciled,
                    )
                if (
                    reconciled is not None
                    and reconciled.state == TurnSubmissionState.RUNNING
                ):
                    return TurnClaimResult(
                        decision=TurnClaimDecision.RETRY,
                        turn=reconciled,
                    )
                raise TurnSubmissionUnavailableError(
                    "Could not confirm durable execution claim"
                ) from claim_error
            if document is not None:
                return TurnClaimResult(
                    decision=TurnClaimDecision.EXECUTE,
                    turn=self._to_domain(document),
                )

            current = self._to_domain(
                await collection.find_one(
                    {"session_id": session_id, "submission_id": submission_id}
                )
            )
            if current is None:
                raise TurnSubmissionUnavailableError("Queued turn does not exist")
            if current.state in TERMINAL_TURN_STATES:
                await self._repair_terminal_postconditions(current)
                return TurnClaimResult(decision=TurnClaimDecision.ACK, turn=current)
            if current.task_id and current.task_id != task_id:
                # A stale entry from an abandoned task stream must not execute.
                return TurnClaimResult(decision=TurnClaimDecision.ACK, turn=current)
            if current.state == TurnSubmissionState.RUNNING:
                current_until = current.claim_until
                if current_until is None or current_until <= now:
                    expired = await collection.find_one_and_update(
                        {
                            "session_id": session_id,
                            "submission_id": submission_id,
                            "state": TurnSubmissionState.RUNNING.value,
                            "$or": [
                                {"claim_until": {"$lte": now}},
                                {"claim_until": None},
                            ],
                        },
                        {
                            "$set": {
                                "state": TurnSubmissionState.FAILED_UNKNOWN.value,
                                "terminal_error": (
                                    "Execution ownership expired; external side-effect "
                                    "status is unknown and the turn will not be replayed"
                                ),
                                "claim_owner": None,
                                "claim_until": None,
                                "expires_at": now + self._terminal_retention,
                                "updated_at": now,
                            }
                        },
                        return_document=ReturnDocument.AFTER,
                    )
                    if expired is not None:
                        expired_turn = self._to_domain(expired)
                        await self._repair_terminal_postconditions(expired_turn)
                        return TurnClaimResult(
                            decision=TurnClaimDecision.ACK, turn=expired_turn
                        )
                # The current owner may still be performing external work. Keep
                # the Redis entry pending so expiry can later become
                # failed_unknown; do not acknowledge it early.
                return TurnClaimResult(
                    decision=TurnClaimDecision.RETRY, turn=current
                )
            return TurnClaimResult(decision=TurnClaimDecision.RETRY, turn=current)
        except TurnSubmissionUnavailableError:
            raise
        except Exception as exc:
            raise TurnSubmissionUnavailableError(
                "Durable execution claim is unavailable"
            ) from exc

    async def renew_claim(
        self,
        session_id: str,
        submission_id: str,
        *,
        owner: str,
        claim_until: datetime,
    ) -> bool:
        try:
            result = await self._turn_collection().update_one(
                {
                    "session_id": session_id,
                    "submission_id": submission_id,
                    "state": TurnSubmissionState.RUNNING.value,
                    "claim_owner": owner,
                },
                {
                    "$set": {
                        "claim_until": claim_until,
                        "updated_at": datetime.now(UTC),
                    }
                },
            )
            return bool(result.modified_count)
        except Exception as exc:
            raise TurnSubmissionUnavailableError(
                "Durable execution claim renewal is unavailable"
            ) from exc

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
        if state not in TERMINAL_TURN_STATES:
            raise ValueError("mark_terminal requires a terminal turn state")
        now = datetime.now(UTC)
        try:
            document = await self._turn_collection().find_one_and_update(
                {
                    "session_id": session_id,
                    "submission_id": submission_id,
                    "state": TurnSubmissionState.RUNNING.value,
                    "claim_owner": owner,
                },
                {
                    "$set": {
                        "state": state.value,
                        "terminal_event_id": terminal_event_id,
                        "terminal_error": error,
                        "claim_owner": None,
                        "claim_until": None,
                        "expires_at": now + self._terminal_retention,
                        "updated_at": now,
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
            if document is None:
                current = await self.find(session_id, submission_id)
                if current is None or current.state not in TERMINAL_TURN_STATES:
                    return False
                await self._repair_terminal_postconditions(current)
                return True
            await self._repair_terminal_postconditions(self._to_domain(document))
            return True
        except Exception as terminal_error:
            # The owner-CAS may have committed even when its Mongo response
            # was lost. Confirm the authoritative terminal postcondition
            # before forcing the task backend into recovery; otherwise an
            # explicit delete can wait on work that is already safely done.
            try:
                current = await self.find(session_id, submission_id)
            except Exception as verify_error:
                raise TurnSubmissionUnavailableError(
                    "Terminal turn commit outcome is unknown"
                ) from verify_error
            if current is not None and current.state in TERMINAL_TURN_STATES:
                await self._repair_terminal_postconditions(current)
                return True
            raise TurnSubmissionUnavailableError(
                "Could not persist terminal turn state"
            ) from terminal_error

    async def mark_unclaimed_terminal(
        self,
        session_id: str,
        submission_id: str,
        *,
        state: TurnSubmissionState,
        error: str,
    ) -> bool:
        if state not in TERMINAL_TURN_STATES:
            raise ValueError("state must be terminal")
        now = datetime.now(UTC)
        try:
            document = await self._turn_collection().find_one_and_update(
                {
                    "session_id": session_id,
                    "submission_id": submission_id,
                    "state": {
                        "$in": [
                            TurnSubmissionState.PENDING.value,
                            TurnSubmissionState.ENQUEUED.value,
                        ]
                    },
                },
                {
                    "$set": {
                        "state": state.value,
                        "terminal_error": error,
                        "expires_at": now + self._terminal_retention,
                        "updated_at": now,
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
            if document is None:
                current = await self.find(session_id, submission_id)
                if current is None or current.state not in TERMINAL_TURN_STATES:
                    return False
                await self._repair_terminal_postconditions(current)
                return True
            await self._repair_terminal_postconditions(self._to_domain(document))
            return True
        except Exception as exc:
            raise TurnSubmissionUnavailableError(
                "Could not persist pre-execution terminal state"
            ) from exc

    async def fail_enqueued_for_task(
        self,
        session_id: str,
        *,
        user_id: str,
        task_id: str,
        error: str,
    ) -> int:
        """Terminalize the exact unclaimed work owned by a failed worker.

        The fixed task/user predicates prevent an obsolete Celery delivery
        from failing work that has been rebound to a replacement task. Turns
        already changed to the same failure are included on retry so quota
        and output TTL repair remains idempotent after a partial outage.
        """
        now = datetime.now(UTC)
        terminal_expiry = now + self._terminal_retention
        base_query: dict[str, Any] = {
            "session_id": session_id,
            "user_id": user_id,
            "task_id": task_id,
        }
        try:
            documents = await self._turn_collection().find(
                {
                    **base_query,
                    "$or": [
                        {"state": TurnSubmissionState.ENQUEUED.value},
                        {
                            "state": TurnSubmissionState.FAILED.value,
                            "terminal_error": error,
                        },
                    ],
                }
            ).to_list(length=None)
            repaired = 0
            for original in documents:
                document = original
                if original.get("state") == TurnSubmissionState.ENQUEUED.value:
                    updated = await self._turn_collection().find_one_and_update(
                        {
                            **base_query,
                            "submission_id": original["submission_id"],
                            "state": TurnSubmissionState.ENQUEUED.value,
                        },
                        {
                            "$set": {
                                "state": TurnSubmissionState.FAILED.value,
                                "terminal_error": error,
                                "claim_owner": None,
                                "claim_until": None,
                                "expires_at": terminal_expiry,
                                "updated_at": now,
                            }
                        },
                        return_document=ReturnDocument.AFTER,
                    )
                    if updated is None:
                        # A concurrent claimant or lifecycle action won. It is
                        # responsible for that turn's terminal state.
                        continue
                    document = updated
                turn = self._to_domain(document)
                if turn is None:
                    continue
                await self._repair_terminal_postconditions(turn)
                repaired += 1

            remaining = await self._turn_collection().count_documents(
                {
                    **base_query,
                    "state": TurnSubmissionState.ENQUEUED.value,
                }
            )
            if remaining:
                raise TurnSubmissionUnavailableError(
                    "Worker-owned enqueued turns could not be terminalized"
                )
            return repaired
        except TurnSubmissionUnavailableError:
            raise
        except Exception as exc:
            raise TurnSubmissionUnavailableError(
                "Could not terminalize worker-owned queued turns"
            ) from exc

    async def recover_factory_failure_for_task(
        self,
        session_id: str,
        *,
        user_id: str,
        task_id: str,
        error: str,
    ) -> bool:
        """Fail unstarted work and fence any prior live execution claim.

        A Redis worker claim can be taken over before the corresponding Mongo
        turn claim expires.  In that interval a replacement worker whose
        factory fails must keep Redis RUNNING: marking the task done would
        orphan the still-owned Mongo turn and its quota reservation.  Once the
        Mongo lease expires, it is terminalized as ``failed_unknown`` because
        external side effects from the previous owner cannot be ruled out.
        """
        await self.fail_enqueued_for_task(
            session_id,
            user_id=user_id,
            task_id=task_id,
            error=error,
        )

        now = datetime.now(UTC)
        terminal_expiry = now + self._terminal_retention
        expired_error = (
            f"{error}; a previous execution lease expired and its external "
            "side-effect status is unknown"
        )
        base_query: dict[str, Any] = {
            "session_id": session_id,
            "user_id": user_id,
            "task_id": task_id,
        }
        try:
            documents = await self._turn_collection().find(
                {
                    **base_query,
                    "$or": [
                        {
                            "state": TurnSubmissionState.RUNNING.value,
                            "$or": [
                                {"claim_until": {"$lte": now}},
                                {"claim_until": None},
                            ],
                        },
                        {
                            "state": TurnSubmissionState.FAILED_UNKNOWN.value,
                            "terminal_error": expired_error,
                        },
                    ],
                }
            ).to_list(length=None)
            for original in documents:
                document = original
                if original.get("state") == TurnSubmissionState.RUNNING.value:
                    updated = await self._turn_collection().find_one_and_update(
                        {
                            **base_query,
                            "submission_id": original["submission_id"],
                            "state": TurnSubmissionState.RUNNING.value,
                            "$or": [
                                {"claim_until": {"$lte": now}},
                                {"claim_until": None},
                            ],
                        },
                        {
                            "$set": {
                                "state": TurnSubmissionState.FAILED_UNKNOWN.value,
                                "terminal_error": expired_error,
                                "claim_owner": None,
                                "claim_until": None,
                                "expires_at": terminal_expiry,
                                "updated_at": now,
                            }
                        },
                        return_document=ReturnDocument.AFTER,
                    )
                    if updated is None:
                        continue
                    document = updated
                turn = self._to_domain(document)
                if turn is not None:
                    await self._repair_terminal_postconditions(turn)

            live_running = await self._turn_collection().count_documents(
                {
                    **base_query,
                    "state": TurnSubmissionState.RUNNING.value,
                }
            )
            return live_running == 0
        except TurnSubmissionUnavailableError:
            raise
        except Exception as exc:
            raise TurnSubmissionUnavailableError(
                "Could not recover worker factory failure"
            ) from exc

    async def cancel_outstanding(
        self, session_id: str, *, user_id: Optional[str] = None
    ) -> int:
        # Compatibility alias retained for older callers. A running turn must
        # be terminalized by its owning worker after cancellation is observed;
        # releasing its quota here would allow the sandbox/model/tool work to
        # continue after the system has declared the turn finished.
        return await self.cancel_queued(session_id, user_id=user_id)

    async def cancel_queued(
        self,
        session_id: str,
        *,
        user_id: Optional[str] = None,
        exclude_submission_id: Optional[str] = None,
    ) -> int:
        query: dict[str, Any] = {
            "session_id": session_id,
            "state": {
                "$in": [
                    TurnSubmissionState.PENDING.value,
                    TurnSubmissionState.ENQUEUED.value,
                ]
            },
        }
        if user_id is not None:
            query["user_id"] = user_id
        if exclude_submission_id is not None:
            query["submission_id"] = {"$ne": exclude_submission_id}
        try:
            documents = await self._turn_collection().find(query).to_list(length=None)
            count = 0
            for document in documents:
                if await self.mark_unclaimed_terminal(
                    document["session_id"],
                    document["submission_id"],
                    state=TurnSubmissionState.CANCELLED,
                    error="Session was stopped or deleted before execution",
                ):
                    count += 1
            return count
        except Exception as exc:
            raise TurnSubmissionUnavailableError(
                "Could not cancel queued turns"
            ) from exc

    async def count_running(
        self, session_id: str, *, user_id: Optional[str] = None
    ) -> int:
        query: dict[str, Any] = {
            "session_id": session_id,
            "state": TurnSubmissionState.RUNNING.value,
        }
        if user_id is not None:
            query["user_id"] = user_id
        try:
            return int(await self._turn_collection().count_documents(query))
        except Exception as exc:
            raise TurnSubmissionUnavailableError(
                "Could not verify stopped turn state"
            ) from exc
