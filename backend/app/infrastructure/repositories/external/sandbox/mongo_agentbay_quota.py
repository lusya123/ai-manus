"""Single-document MongoDB ledger for billable AgentBay sessions.

This repository is intentionally independent from Beanie registration.  All
quota-changing operations are conditional updates against one fixed document,
using primary/majority reads and majority+journaled writes.  A document is
created in ``required`` state and cannot accept ordinary reservations until an
explicit, full inventory reconciliation marks it ready.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Awaitable, Mapping, Optional, Sequence, TypeVar

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

from app.core.config import get_settings
from app.domain.external.agentbay_quota import (
    AgentBayBootstrapState,
    AgentBayQuotaBootstrapError,
    AgentBayQuotaConfigurationError,
    AgentBayQuotaExceededError,
    AgentBayQuotaInconsistentError,
    AgentBayQuotaInventoryEntry,
    AgentBayQuotaNotInitializedError,
    AgentBayQuotaOutcome,
    AgentBayQuotaReservation,
    AgentBayQuotaResult,
    AgentBayQuotaScope,
    AgentBayQuotaSnapshot,
    AgentBayQuotaUnavailableError,
    AgentBayReservationPhase,
)
from app.infrastructure.storage.mongodb import get_mongodb


T = TypeVar("T")

LEDGER_COLLECTION = "agentbay_session_quota_ledger"
LEDGER_DOCUMENT_ID = "agentbay-session-quota-v1"
LEDGER_SCHEMA_VERSION = 1
LEDGER_HARD_MAX_TOTAL = 20


class MongoAgentBayQuotaLedger:
    """Durable, replica-safe cost ledger for AgentBay session allocations."""

    def __init__(
        self,
        *,
        deployment_id: str,
        max_total: int,
        max_per_user: int,
        config_version: str = "1",
        command_timeout_seconds: float = 2.0,
        collection: Any | None = None,
    ) -> None:
        self._deployment_key = self._digest(
            "deployment", self._identifier(deployment_id, "deployment_id", 512)
        )
        self._config_version = self._identifier(
            config_version, "config_version", 128
        )
        self._max_total = int(max_total)
        self._max_per_user = int(max_per_user)
        self._command_timeout_seconds = float(command_timeout_seconds)
        if self._max_total < 1 or self._max_per_user < 1:
            raise ValueError("AgentBay quota limits must be positive")
        if self._max_total > LEDGER_HARD_MAX_TOTAL:
            raise ValueError(
                f"AgentBay global quota cannot exceed {LEDGER_HARD_MAX_TOTAL}"
            )
        if self._max_per_user > self._max_total:
            raise ValueError("AgentBay per-user quota cannot exceed global quota")
        if self._command_timeout_seconds <= 0:
            raise ValueError("AgentBay quota command timeout must be positive")

        self._max_time_ms = max(1, int(self._command_timeout_seconds * 1000))
        self._collection_override = collection
        self._configured_collection: Any | None = None

    @staticmethod
    def _identifier(value: str, name: str, maximum: int = 1024) -> str:
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise ValueError(f"{name} must be a non-empty bounded string")
        return value

    @staticmethod
    def _digest(namespace: str, value: str) -> str:
        return hashlib.sha256(
            f"agentbay-quota:{namespace}:v1\0{value}".encode("utf-8")
        ).hexdigest()

    @classmethod
    def session_key(cls, session_id: str) -> str:
        return cls._digest(
            "session", cls._identifier(session_id, "session_id")
        )

    @classmethod
    def user_key(cls, user_id: str) -> str:
        return cls._digest("user", cls._identifier(user_id, "user_id"))

    @classmethod
    def operation_key(cls, operation_id: str) -> str:
        return cls._digest(
            "operation", cls._identifier(operation_id, "operation_id", 256)
        )

    @classmethod
    def provider_key(cls, provider_id: str) -> str:
        return cls._digest(
            "provider", cls._identifier(provider_id, "provider_id", 1024)
        )

    def _collection(self) -> Any:
        if self._configured_collection is None:
            if self._collection_override is not None:
                collection = self._collection_override
            else:
                settings = get_settings()
                collection = get_mongodb().client[settings.mongodb_database][
                    LEDGER_COLLECTION
                ]
            self._configured_collection = collection.with_options(
                read_preference=ReadPreference.PRIMARY,
                read_concern=ReadConcern("majority"),
                write_concern=WriteConcern(
                    w="majority", j=True, wtimeout=self._max_time_ms
                ),
            )
        return self._configured_collection

    def _config_filter(self) -> dict[str, Any]:
        return {
            "_id": LEDGER_DOCUMENT_ID,
            "schema_version": LEDGER_SCHEMA_VERSION,
            "config_version": self._config_version,
            "deployment_key": self._deployment_key,
            "max_total": self._max_total,
            "max_per_user": self._max_per_user,
        }

    def _new_document(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        return {
            **self._config_filter(),
            "bootstrap_state": AgentBayBootstrapState.REQUIRED.value,
            "reservations": {},
            "user_counts": {},
            "operation_sessions": {},
            "provider_sessions": {},
            "total": 0,
            "revision": 0,
            "created_at": now,
            "updated_at": now,
        }

    async def _bounded(self, awaitable: Awaitable[T]) -> T:
        return await asyncio.wait_for(
            awaitable, timeout=self._command_timeout_seconds
        )

    async def _read_raw(self) -> Optional[dict[str, Any]]:
        try:
            return await self._bounded(
                self._collection().find_one(
                    {"_id": LEDGER_DOCUMENT_ID},
                    max_time_ms=self._max_time_ms,
                    comment="agentbay-quota:read",
                )
            )
        except asyncio.CancelledError:
            raise
        except AgentBayQuotaUnavailableError:
            raise
        except Exception as exc:
            raise AgentBayQuotaUnavailableError(
                "AgentBay quota ledger is unavailable; provisioning was refused"
            ) from exc

    async def _best_effort_postread(self) -> Optional[dict[str, Any]]:
        """Read after a cancelled write without suppressing cancellation."""
        task = asyncio.create_task(self._read_raw())
        try:
            return await asyncio.shield(task)
        except BaseException:
            if not task.done():
                task.cancel()
            return None

    @staticmethod
    def _bootstrap_state(document: Mapping[str, Any]) -> AgentBayBootstrapState:
        try:
            return AgentBayBootstrapState(str(document.get("bootstrap_state")))
        except ValueError as exc:
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota bootstrap state is inconsistent"
            ) from exc

    def _assert_configuration(self, document: Mapping[str, Any]) -> None:
        expected = self._config_filter()
        if any(document.get(key) != value for key, value in expected.items()):
            raise AgentBayQuotaConfigurationError(
                "AgentBay quota schema, deployment, version, or caps do not match"
            )
        self._validate_invariants(document)

    @classmethod
    def _validate_invariants(cls, document: Mapping[str, Any]) -> None:
        reservations = document.get("reservations")
        user_counts = document.get("user_counts")
        operation_sessions = document.get("operation_sessions")
        provider_sessions = document.get("provider_sessions")
        if not all(
            isinstance(value, Mapping)
            for value in (
                reservations,
                user_counts,
                operation_sessions,
                provider_sessions,
            )
        ):
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota maps are inconsistent"
            )
        assert isinstance(reservations, Mapping)
        assert isinstance(user_counts, Mapping)
        assert isinstance(operation_sessions, Mapping)
        assert isinstance(provider_sessions, Mapping)

        derived_users: Counter[str] = Counter()
        derived_operations: dict[str, str] = {}
        derived_providers: dict[str, str] = {}
        for session_key, raw in reservations.items():
            if not isinstance(session_key, str) or not isinstance(raw, Mapping):
                raise AgentBayQuotaInconsistentError(
                    "AgentBay quota reservation map is inconsistent"
                )
            reservation = cls._reservation_from_raw(session_key, raw)
            if reservation.operation_key in derived_operations:
                raise AgentBayQuotaInconsistentError(
                    "AgentBay quota operation index contains duplicates"
                )
            derived_users[reservation.user_key] += 1
            derived_operations[reservation.operation_key] = session_key
            if reservation.phase is AgentBayReservationPhase.PROVISIONED:
                assert reservation.provider_key is not None
                if reservation.provider_key in derived_providers:
                    raise AgentBayQuotaInconsistentError(
                        "AgentBay quota provider index contains duplicates"
                    )
                derived_providers[reservation.provider_key] = session_key

        try:
            normalized_counts = {
                str(key): int(value) for key, value in user_counts.items()
            }
            total = int(document.get("total", -1))
            stored_max = int(document.get("max_total", -1))
            stored_user_max = int(document.get("max_per_user", -1))
            revision = int(document.get("revision", -1))
        except (TypeError, ValueError) as exc:
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota counters are inconsistent"
            ) from exc
        if (
            any(value < 0 for value in normalized_counts.values())
            or sum(normalized_counts.values()) != len(reservations)
            or total != len(reservations)
            or total < 0
            or revision < 0
            or stored_max < total
            or any(value > stored_user_max for value in normalized_counts.values())
            or any(
                normalized_counts.get(user_key, 0) != count
                for user_key, count in derived_users.items()
            )
            or any(
                count != 0 and user_key not in derived_users
                for user_key, count in normalized_counts.items()
            )
            or dict(operation_sessions) != derived_operations
            or dict(provider_sessions) != derived_providers
        ):
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota counters or indexes are inconsistent"
            )

    @staticmethod
    def _is_digest(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @classmethod
    def _reservation_from_raw(
        cls, session_key: str, raw: Mapping[str, Any]
    ) -> AgentBayQuotaReservation:
        try:
            phase = AgentBayReservationPhase(str(raw.get("phase")))
            user_key = str(raw["user_key"])
            operation_key = str(raw["operation_key"])
            operation_id = str(raw["operation_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota reservation is inconsistent"
            ) from exc
        provider_id = raw.get("provider_id")
        provider_key = raw.get("provider_key")
        try:
            expected_operation_key = cls.operation_key(operation_id)
            expected_provider_key = (
                cls.provider_key(provider_id)
                if isinstance(provider_id, str)
                else None
            )
        except ValueError as exc:
            raise AgentBayQuotaInconsistentError(
                "AgentBay reservation identifiers are inconsistent"
            ) from exc
        if (
            not cls._is_digest(session_key)
            or not cls._is_digest(user_key)
            or not cls._is_digest(operation_key)
            or operation_key != expected_operation_key
        ):
            raise AgentBayQuotaInconsistentError(
                "AgentBay reservation digests are inconsistent"
            )
        if phase is AgentBayReservationPhase.PROVISIONED and (
            not isinstance(provider_id, str)
            or not provider_id
            or not isinstance(provider_key, str)
            or not provider_key
            or not cls._is_digest(provider_key)
            or provider_key != expected_provider_key
        ):
            raise AgentBayQuotaInconsistentError(
                "AgentBay provisioned reservation has no cleanup handle"
            )
        if phase is AgentBayReservationPhase.RESERVED and (
            provider_id is not None or provider_key is not None
        ):
            raise AgentBayQuotaInconsistentError(
                "AgentBay reserved operation unexpectedly has a provider handle"
            )
        return AgentBayQuotaReservation(
            session_key=session_key,
            user_key=user_key,
            operation_key=operation_key,
            operation_id=operation_id,
            phase=phase,
            provider_id=provider_id if isinstance(provider_id, str) else None,
            provider_key=provider_key if isinstance(provider_key, str) else None,
            created_at=raw.get("created_at"),
            updated_at=raw.get("updated_at"),
        )

    @classmethod
    def _find_reservation(
        cls, document: Mapping[str, Any], session_key: str
    ) -> Optional[AgentBayQuotaReservation]:
        reservations = document.get("reservations")
        if reservations is None:
            return None
        if not isinstance(reservations, Mapping):
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota reservation map is inconsistent"
            )
        raw = reservations.get(session_key)
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota reservation is inconsistent"
            )
        return cls._reservation_from_raw(session_key, raw)

    @staticmethod
    def _result(
        outcome: AgentBayQuotaOutcome,
        document: Optional[Mapping[str, Any]],
        reservation: Optional[AgentBayQuotaReservation] = None,
    ) -> AgentBayQuotaResult:
        if document is None:
            return AgentBayQuotaResult(outcome=outcome, reservation=reservation)
        state: Optional[AgentBayBootstrapState]
        try:
            state = AgentBayBootstrapState(str(document.get("bootstrap_state")))
        except ValueError:
            state = None
        return AgentBayQuotaResult(
            outcome=outcome,
            reservation=reservation,
            bootstrap_state=state,
            revision=int(document.get("revision", 0)),
            total=int(document.get("total", 0)),
        )

    @staticmethod
    def _existing_outcome(
        reservation: AgentBayQuotaReservation,
    ) -> AgentBayQuotaOutcome:
        if reservation.phase is AgentBayReservationPhase.PROVISIONED:
            return AgentBayQuotaOutcome.EXISTING_PROVISIONED
        return AgentBayQuotaOutcome.EXISTING_RESERVED

    def _classify_ready_document(
        self,
        document: Optional[Mapping[str, Any]],
        *,
        session_key: str,
        user_key: str,
        operation_key: Optional[str] = None,
    ) -> Optional[AgentBayQuotaReservation]:
        if document is None:
            raise AgentBayQuotaNotInitializedError(
                "AgentBay quota ledger has not been initialized"
            )
        self._assert_configuration(document)
        state = self._bootstrap_state(document)
        if state is not AgentBayBootstrapState.READY:
            raise AgentBayQuotaBootstrapError(state)
        existing = self._find_reservation(document, session_key)
        if existing is not None:
            if existing.user_key != user_key:
                raise AgentBayQuotaInconsistentError(
                    "AgentBay reservation ownership is inconsistent"
                )
            return existing
        if operation_key is not None:
            operation_sessions = document.get("operation_sessions") or {}
            if not isinstance(operation_sessions, Mapping):
                raise AgentBayQuotaInconsistentError(
                    "AgentBay operation index is inconsistent"
                )
            bound_session = operation_sessions.get(operation_key)
            if bound_session is not None and bound_session != session_key:
                raise AgentBayQuotaInconsistentError(
                    "AgentBay operation is already bound to another session"
                )
        return None

    async def ensure_ledger(self) -> AgentBayQuotaResult:
        existing = await self._read_raw()
        if existing is not None:
            self._assert_configuration(existing)
            return self._result(AgentBayQuotaOutcome.LEDGER_EXISTS, existing)

        try:
            await self._bounded(
                self._collection().insert_one(
                    self._new_document(), comment="agentbay-quota:initialize"
                )
            )
            created = await self._read_raw()
            if created is None:
                raise AgentBayQuotaUnavailableError(
                    "AgentBay quota initialization could not be verified"
                )
            self._assert_configuration(created)
            return self._result(AgentBayQuotaOutcome.LEDGER_CREATED, created)
        except asyncio.CancelledError:
            await self._best_effort_postread()
            raise
        except DuplicateKeyError:
            raced = await self._read_raw()
            if raced is None:
                raise AgentBayQuotaUnavailableError(
                    "AgentBay quota initialization could not be verified"
                )
            self._assert_configuration(raced)
            return self._result(AgentBayQuotaOutcome.LEDGER_EXISTS, raced)
        except AgentBayQuotaConfigurationError:
            raise
        except AgentBayQuotaUnavailableError:
            raise
        except Exception as exc:
            post = await self._read_raw()
            if post is not None:
                self._assert_configuration(post)
                return self._result(AgentBayQuotaOutcome.LEDGER_EXISTS, post)
            raise AgentBayQuotaUnavailableError(
                "AgentBay quota initialization could not be verified"
            ) from exc

    async def snapshot(self) -> AgentBayQuotaSnapshot:
        document = await self._read_raw()
        if document is None:
            raise AgentBayQuotaNotInitializedError(
                "AgentBay quota ledger has not been initialized"
            )
        self._assert_configuration(document)
        reservations = document.get("reservations") or {}
        if not isinstance(reservations, Mapping):
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota reservation map is inconsistent"
            )
        parsed = tuple(
            self._reservation_from_raw(key, raw)
            for key, raw in reservations.items()
            if isinstance(key, str) and isinstance(raw, Mapping)
        )
        if len(parsed) != len(reservations):
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota reservation map is inconsistent"
            )
        total = int(document.get("total", -1))
        if total != len(parsed):
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota total is inconsistent"
            )
        return AgentBayQuotaSnapshot(
            bootstrap_state=self._bootstrap_state(document),
            revision=int(document.get("revision", 0)),
            total=total,
            reservations=parsed,
        )

    async def get_reservation_for_cleanup(
        self, session_id: str, user_id: str
    ) -> Optional[AgentBayQuotaReservation]:
        """Return a validated cleanup handle despite replica config mismatch.

        Cleanup and recovery must remain possible during a deployment/cap
        migration or when the Session pointer was lost.  The read still uses
        the collection's primary/majority concern and validates the complete
        stored document before returning an exact owner-bound reservation.
        """

        session_key = self.session_key(session_id)
        user_key = self.user_key(user_id)
        document = await self._read_raw()
        if document is None:
            return None
        # Deliberately do not call _assert_configuration: the stored schema,
        # deployment, config version, and caps may belong to another replica.
        self._validate_invariants(document)
        reservation = self._find_reservation(document, session_key)
        if reservation is None:
            return None
        if reservation.user_key != user_key:
            raise AgentBayQuotaInconsistentError(
                "AgentBay cleanup reservation ownership is inconsistent"
            )
        return reservation

    async def begin_reconciliation(self) -> AgentBayQuotaResult:
        before = await self._read_raw()
        if before is None:
            raise AgentBayQuotaNotInitializedError(
                "AgentBay quota ledger has not been initialized"
            )
        self._assert_configuration(before)
        state = self._bootstrap_state(before)
        if state is AgentBayBootstrapState.RECONCILING:
            return self._result(
                AgentBayQuotaOutcome.ALREADY_RECONCILING, before
            )
        expected_revision = int(before.get("revision", 0))
        update = {
            "$set": {
                "bootstrap_state": AgentBayBootstrapState.RECONCILING.value,
                "updated_at": datetime.now(UTC),
            },
            "$inc": {"revision": 1},
        }
        try:
            document = await self._bounded(
                self._collection().find_one_and_update(
                    {
                        **self._config_filter(),
                        "bootstrap_state": state.value,
                        "revision": expected_revision,
                    },
                    update,
                    return_document=ReturnDocument.AFTER,
                    maxTimeMS=self._max_time_ms,
                    comment="agentbay-quota:begin-reconcile",
                )
            )
        except asyncio.CancelledError:
            await self._best_effort_postread()
            raise
        except Exception as exc:
            post = await self._read_raw()
            if post is not None:
                self._assert_configuration(post)
                if self._bootstrap_state(post) is AgentBayBootstrapState.RECONCILING:
                    return self._result(AgentBayQuotaOutcome.RECONCILING, post)
            raise AgentBayQuotaUnavailableError(
                "AgentBay reconciliation transition could not be verified"
            ) from exc
        if document is not None:
            return self._result(AgentBayQuotaOutcome.RECONCILING, document)
        post = await self._read_raw()
        if post is None:
            raise AgentBayQuotaNotInitializedError(
                "AgentBay quota ledger has not been initialized"
            )
        self._assert_configuration(post)
        if self._bootstrap_state(post) is AgentBayBootstrapState.RECONCILING:
            return self._result(AgentBayQuotaOutcome.ALREADY_RECONCILING, post)
        raise AgentBayQuotaUnavailableError(
            "AgentBay reconciliation lost an atomic compare-and-set race"
        )

    def _build_inventory(
        self, inventory: Sequence[AgentBayQuotaInventoryEntry]
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, int],
        dict[str, str],
        dict[str, str],
    ]:
        if len(inventory) > self._max_total:
            raise AgentBayQuotaExceededError(AgentBayQuotaScope.GLOBAL)
        now = datetime.now(UTC)
        reservations: dict[str, dict[str, Any]] = {}
        operation_sessions: dict[str, str] = {}
        provider_sessions: dict[str, str] = {}
        user_keys: list[str] = []
        for item in inventory:
            session_key = self.session_key(item.session_id)
            user_key = self.user_key(item.user_id)
            operation_id = self._identifier(
                item.operation_id, "operation_id", 256
            )
            provider_id = self._identifier(item.provider_id, "provider_id", 1024)
            operation_key = self.operation_key(operation_id)
            provider_key = self.provider_key(provider_id)
            if session_key in reservations:
                raise AgentBayQuotaInconsistentError(
                    "Reconciled AgentBay inventory contains duplicate sessions"
                )
            if operation_key in operation_sessions:
                raise AgentBayQuotaInconsistentError(
                    "Reconciled AgentBay inventory contains duplicate operations"
                )
            if provider_key in provider_sessions:
                raise AgentBayQuotaInconsistentError(
                    "Reconciled AgentBay inventory contains duplicate providers"
                )
            reservations[session_key] = {
                "user_key": user_key,
                "operation_key": operation_key,
                "operation_id": operation_id,
                "phase": AgentBayReservationPhase.PROVISIONED.value,
                "provider_id": provider_id,
                "provider_key": provider_key,
                "created_at": now,
                "updated_at": now,
            }
            operation_sessions[operation_key] = session_key
            provider_sessions[provider_key] = session_key
            user_keys.append(user_key)
        user_counts = dict(Counter(user_keys))
        if any(count > self._max_per_user for count in user_counts.values()):
            raise AgentBayQuotaExceededError(AgentBayQuotaScope.USER)
        return reservations, user_counts, operation_sessions, provider_sessions

    @staticmethod
    def _inventory_matches(
        document: Mapping[str, Any],
        reservations: Mapping[str, Mapping[str, Any]],
        user_counts: Mapping[str, int],
        operation_sessions: Mapping[str, str],
        provider_sessions: Mapping[str, str],
    ) -> bool:
        current = document.get("reservations") or {}
        if not isinstance(current, Mapping) or set(current) != set(reservations):
            return False
        core_fields = (
            "user_key",
            "operation_key",
            "operation_id",
            "phase",
            "provider_id",
            "provider_key",
        )
        for key, expected in reservations.items():
            actual = current.get(key)
            if not isinstance(actual, Mapping):
                return False
            if any(actual.get(field) != expected.get(field) for field in core_fields):
                return False
        return (
            document.get("user_counts") == dict(user_counts)
            and document.get("operation_sessions") == dict(operation_sessions)
            and document.get("provider_sessions") == dict(provider_sessions)
            and int(document.get("total", -1)) == len(reservations)
            and document.get("bootstrap_state")
            == AgentBayBootstrapState.READY.value
        )

    async def reconcile_inventory(
        self,
        inventory: Sequence[AgentBayQuotaInventoryEntry],
        *,
        expected_revision: int,
    ) -> AgentBayQuotaResult:
        before = await self._read_raw()
        if before is None:
            raise AgentBayQuotaNotInitializedError(
                "AgentBay quota ledger has not been initialized"
            )
        self._assert_configuration(before)
        state = self._bootstrap_state(before)
        if state is not AgentBayBootstrapState.RECONCILING:
            raise AgentBayQuotaBootstrapError(state)
        if int(before.get("revision", -1)) != int(expected_revision):
            raise AgentBayQuotaUnavailableError(
                "AgentBay inventory changed during reconciliation; rescan is required"
            )
        current_reservations = before.get("reservations") or {}
        assert isinstance(current_reservations, Mapping)
        if any(
            self._reservation_from_raw(session_key, raw).phase
            is AgentBayReservationPhase.RESERVED
            for session_key, raw in current_reservations.items()
        ):
            # A reserve may commit just before reconciliation blocks new ones.
            # Never overwrite that in-flight operation: wait for provision or
            # exact cleanup, then rescan from the new revision.
            raise AgentBayQuotaInconsistentError(
                "AgentBay reconciliation cannot replace an in-flight reservation"
            )
        (
            reservations,
            user_counts,
            operation_sessions,
            provider_sessions,
        ) = self._build_inventory(inventory)
        update = {
            "$set": {
                "reservations": reservations,
                "user_counts": user_counts,
                "operation_sessions": operation_sessions,
                "provider_sessions": provider_sessions,
                "total": len(reservations),
                "bootstrap_state": AgentBayBootstrapState.READY.value,
                "updated_at": datetime.now(UTC),
            },
            "$inc": {"revision": 1},
        }
        selector = {
            **self._config_filter(),
            "bootstrap_state": AgentBayBootstrapState.RECONCILING.value,
            "revision": int(expected_revision),
        }
        try:
            document = await self._bounded(
                self._collection().find_one_and_update(
                    selector,
                    update,
                    return_document=ReturnDocument.AFTER,
                    maxTimeMS=self._max_time_ms,
                    comment="agentbay-quota:replace-inventory",
                )
            )
        except asyncio.CancelledError:
            await self._best_effort_postread()
            raise
        except Exception as exc:
            post = await self._read_raw()
            if post is not None:
                self._assert_configuration(post)
                if self._inventory_matches(
                    post,
                    reservations,
                    user_counts,
                    operation_sessions,
                    provider_sessions,
                ):
                    return self._result(AgentBayQuotaOutcome.RECONCILED, post)
            raise AgentBayQuotaUnavailableError(
                "AgentBay inventory replacement could not be verified"
            ) from exc
        if document is not None:
            return self._result(AgentBayQuotaOutcome.RECONCILED, document)
        post = await self._read_raw()
        if post is None:
            raise AgentBayQuotaNotInitializedError(
                "AgentBay quota ledger has not been initialized"
            )
        self._assert_configuration(post)
        if self._inventory_matches(
            post,
            reservations,
            user_counts,
            operation_sessions,
            provider_sessions,
        ):
            return self._result(AgentBayQuotaOutcome.RECONCILED, post)
        if self._bootstrap_state(post) is not AgentBayBootstrapState.RECONCILING:
            raise AgentBayQuotaBootstrapError(self._bootstrap_state(post))
        raise AgentBayQuotaUnavailableError(
            "AgentBay inventory changed during reconciliation; rescan is required"
        )

    async def adopt_existing(
        self,
        session_id: str,
        user_id: str,
        operation_id: str,
        provider_id: str,
    ) -> AgentBayQuotaResult:
        """Add one known provider session while reconciliation remains blocked.

        Full bootstrap should prefer :meth:`reconcile_inventory`, which is
        all-or-nothing.  This method exists for incremental discovery but never
        transitions the ledger to ready.
        """

        session_key = self.session_key(session_id)
        user_key = self.user_key(user_id)
        operation_id = self._identifier(operation_id, "operation_id", 256)
        provider_id = self._identifier(provider_id, "provider_id", 1024)
        operation_key = self.operation_key(operation_id)
        provider_key = self.provider_key(provider_id)
        # CAS retries reuse the exact same stable operation.  Transport-
        # ambiguous writes are not retried here; _insert_reservation raises
        # unavailable unless its majority post-read proves the postcondition.
        for _ in range(self._max_total + 2):
            before = await self._read_raw()
            if before is None:
                raise AgentBayQuotaNotInitializedError(
                    "AgentBay quota ledger has not been initialized"
                )
            self._assert_configuration(before)
            state = self._bootstrap_state(before)
            if state is not AgentBayBootstrapState.RECONCILING:
                raise AgentBayQuotaBootstrapError(state)
            existing = self._find_reservation(before, session_key)
            if existing is not None:
                if (
                    existing.user_key == user_key
                    and existing.operation_id == operation_id
                    and existing.phase is AgentBayReservationPhase.PROVISIONED
                    and existing.provider_id == provider_id
                ):
                    return self._result(
                        AgentBayQuotaOutcome.EXISTING_PROVISIONED,
                        before,
                        existing,
                    )
                raise AgentBayQuotaInconsistentError(
                    "Adopted AgentBay reservation conflicts with stored inventory"
                )
            now = datetime.now(UTC)
            reservation = {
                "user_key": user_key,
                "operation_key": operation_key,
                "operation_id": operation_id,
                "phase": AgentBayReservationPhase.PROVISIONED.value,
                "provider_id": provider_id,
                "provider_key": provider_key,
                "created_at": now,
                "updated_at": now,
            }
            selector = {
                **self._config_filter(),
                "bootstrap_state": AgentBayBootstrapState.RECONCILING.value,
                "revision": int(before["revision"]),
                "total": {"$lt": self._max_total},
                f"reservations.{session_key}": {"$exists": False},
                f"operation_sessions.{operation_key}": {"$exists": False},
                f"provider_sessions.{provider_key}": {"$exists": False},
                "$or": [
                    {f"user_counts.{user_key}": {"$exists": False}},
                    {f"user_counts.{user_key}": {"$lt": self._max_per_user}},
                ],
            }
            update = {
                "$set": {
                    f"reservations.{session_key}": reservation,
                    f"operation_sessions.{operation_key}": session_key,
                    f"provider_sessions.{provider_key}": session_key,
                    "updated_at": now,
                },
                "$inc": {
                    "total": 1,
                    f"user_counts.{user_key}": 1,
                    "revision": 1,
                },
            }
            result = await self._insert_reservation(
                selector=selector,
                update=update,
                session_key=session_key,
                user_key=user_key,
                operation_key=operation_key,
                success_outcome=AgentBayQuotaOutcome.ADOPTED,
                comment="agentbay-quota:adopt",
                require_ready=False,
                expected_provider_id=provider_id,
                expected_provider_key=provider_key,
            )
            if result.outcome is not AgentBayQuotaOutcome.RETRYABLE:
                return result
        raise AgentBayQuotaUnavailableError(
            "AgentBay adoption remained contended; reconciliation must retry"
        )

    async def reserve(
        self, session_id: str, user_id: str, operation_id: str
    ) -> AgentBayQuotaResult:
        session_key = self.session_key(session_id)
        user_key = self.user_key(user_id)
        operation_id = self._identifier(operation_id, "operation_id", 256)
        operation_key = self.operation_key(operation_id)

        for _ in range(self._max_total + 2):
            before = await self._read_raw()
            existing = self._classify_ready_document(
                before,
                session_key=session_key,
                user_key=user_key,
                operation_key=operation_key,
            )
            if existing is not None:
                outcome = (
                    self._existing_outcome(existing)
                    if existing.operation_id == operation_id
                    else AgentBayQuotaOutcome.STALE_OPERATION
                )
                return self._result(outcome, before, existing)
            assert before is not None
            now = datetime.now(UTC)
            reservation = {
                "user_key": user_key,
                "operation_key": operation_key,
                "operation_id": operation_id,
                "phase": AgentBayReservationPhase.RESERVED.value,
                "created_at": now,
                "updated_at": now,
            }
            selector = {
                **self._config_filter(),
                "bootstrap_state": AgentBayBootstrapState.READY.value,
                "revision": int(before["revision"]),
                "total": {"$lt": self._max_total},
                f"reservations.{session_key}": {"$exists": False},
                f"operation_sessions.{operation_key}": {"$exists": False},
                "$or": [
                    {f"user_counts.{user_key}": {"$exists": False}},
                    {f"user_counts.{user_key}": {"$lt": self._max_per_user}},
                ],
            }
            update = {
                "$set": {
                    f"reservations.{session_key}": reservation,
                    f"operation_sessions.{operation_key}": session_key,
                    "updated_at": now,
                },
                "$inc": {
                    "total": 1,
                    f"user_counts.{user_key}": 1,
                    "revision": 1,
                },
            }
            result = await self._insert_reservation(
                selector=selector,
                update=update,
                session_key=session_key,
                user_key=user_key,
                operation_key=operation_key,
                success_outcome=AgentBayQuotaOutcome.RESERVED,
                comment="agentbay-quota:reserve",
                require_ready=True,
            )
            if result.outcome is not AgentBayQuotaOutcome.RETRYABLE:
                return result
        raise AgentBayQuotaUnavailableError(
            "AgentBay reservation remained contended; caller must retry the same operation"
        )

    async def _insert_reservation(
        self,
        *,
        selector: Mapping[str, Any],
        update: Mapping[str, Any],
        session_key: str,
        user_key: str,
        operation_key: str,
        success_outcome: AgentBayQuotaOutcome,
        comment: str,
        require_ready: bool,
        expected_provider_id: Optional[str] = None,
        expected_provider_key: Optional[str] = None,
    ) -> AgentBayQuotaResult:
        ambiguous = False
        try:
            document = await self._bounded(
                self._collection().find_one_and_update(
                    selector,
                    update,
                    return_document=ReturnDocument.AFTER,
                    maxTimeMS=self._max_time_ms,
                    comment=comment,
                )
            )
        except asyncio.CancelledError:
            await self._best_effort_postread()
            raise
        except Exception:
            ambiguous = True
            document = None

        post = document if document is not None else await self._read_raw()
        if post is None:
            if ambiguous:
                raise AgentBayQuotaUnavailableError(
                    "AgentBay reservation outcome could not be verified"
                )
            raise AgentBayQuotaNotInitializedError(
                "AgentBay quota ledger has not been initialized"
            )
        self._assert_configuration(post)
        state = self._bootstrap_state(post)
        required_state = (
            AgentBayBootstrapState.READY
            if require_ready
            else AgentBayBootstrapState.RECONCILING
        )
        if state is not required_state:
            raise AgentBayQuotaBootstrapError(state)
        existing = self._find_reservation(post, session_key)
        if existing is not None:
            if existing.user_key != user_key:
                raise AgentBayQuotaInconsistentError(
                    "AgentBay reservation ownership is inconsistent"
                )
            if existing.operation_key == operation_key:
                if (
                    expected_provider_id is not None
                    and existing.provider_id != expected_provider_id
                ):
                    raise AgentBayQuotaInconsistentError(
                        "AgentBay provider adoption is inconsistent"
                    )
                outcome = (
                    success_outcome
                    if document is not None or ambiguous
                    else self._existing_outcome(existing)
                )
                return self._result(outcome, post, existing)
            if require_ready:
                return self._result(
                    AgentBayQuotaOutcome.STALE_OPERATION, post, existing
                )
            raise AgentBayQuotaInconsistentError(
                "Adopted AgentBay reservation operation is inconsistent"
            )
        if ambiguous:
            raise AgentBayQuotaUnavailableError(
                "AgentBay reservation outcome could not be verified"
            )

        operation_sessions = post.get("operation_sessions") or {}
        if not isinstance(operation_sessions, Mapping):
            raise AgentBayQuotaInconsistentError(
                "AgentBay operation index is inconsistent"
            )
        if operation_sessions.get(operation_key) not in (None, session_key):
            raise AgentBayQuotaInconsistentError(
                "AgentBay operation is already bound to another session"
            )
        if expected_provider_key is not None:
            provider_sessions = post.get("provider_sessions") or {}
            if not isinstance(provider_sessions, Mapping):
                raise AgentBayQuotaInconsistentError(
                    "AgentBay provider index is inconsistent"
                )
            if provider_sessions.get(expected_provider_key) not in (
                None,
                session_key,
            ):
                raise AgentBayQuotaInconsistentError(
                    "AgentBay provider is already bound to another session"
                )
        if int(post.get("total", -1)) >= self._max_total:
            raise AgentBayQuotaExceededError(AgentBayQuotaScope.GLOBAL)
        user_counts = post.get("user_counts") or {}
        if not isinstance(user_counts, Mapping):
            raise AgentBayQuotaInconsistentError(
                "AgentBay user counters are inconsistent"
            )
        if int(user_counts.get(user_key, 0)) >= self._max_per_user:
            raise AgentBayQuotaExceededError(AgentBayQuotaScope.USER)
        return self._result(
            AgentBayQuotaOutcome.RETRYABLE,
            post,
        )

    async def mark_provisioned(
        self,
        session_id: str,
        user_id: str,
        operation_id: str,
        provider_id: str,
    ) -> AgentBayQuotaResult:
        session_key = self.session_key(session_id)
        user_key = self.user_key(user_id)
        operation_id = self._identifier(operation_id, "operation_id", 256)
        provider_id = self._identifier(provider_id, "provider_id", 1024)
        operation_key = self.operation_key(operation_id)
        provider_key = self.provider_key(provider_id)
        before = await self._read_raw()
        if before is None:
            raise AgentBayQuotaNotInitializedError(
                "AgentBay quota ledger has not been initialized"
            )
        self._assert_configuration(before)
        existing = self._find_reservation(before, session_key)
        classified = self._classify_phase_target(
            existing,
            user_key=user_key,
            operation_key=operation_key,
            provider_id=provider_id,
        )
        if classified is not None:
            return self._result(classified, before, existing)
        provider_sessions = before.get("provider_sessions") or {}
        if not isinstance(provider_sessions, Mapping):
            raise AgentBayQuotaInconsistentError(
                "AgentBay provider index is inconsistent"
            )
        if provider_sessions.get(provider_key) not in (None, session_key):
            raise AgentBayQuotaInconsistentError(
                "AgentBay provider is already bound to another session"
            )

        now = datetime.now(UTC)
        selector = {
            **self._config_filter(),
            f"reservations.{session_key}.user_key": user_key,
            f"reservations.{session_key}.operation_key": operation_key,
            f"reservations.{session_key}.operation_id": operation_id,
            f"reservations.{session_key}.phase": AgentBayReservationPhase.RESERVED.value,
            f"provider_sessions.{provider_key}": {"$exists": False},
        }
        update = {
            "$set": {
                f"reservations.{session_key}.phase": AgentBayReservationPhase.PROVISIONED.value,
                f"reservations.{session_key}.provider_id": provider_id,
                f"reservations.{session_key}.provider_key": provider_key,
                f"reservations.{session_key}.updated_at": now,
                f"provider_sessions.{provider_key}": session_key,
                "updated_at": now,
            },
            "$inc": {"revision": 1},
        }
        return await self._phase_mutation(
            selector=selector,
            update=update,
            session_key=session_key,
            user_key=user_key,
            operation_key=operation_key,
            operation_id=operation_id,
            provider_id=provider_id,
            success=AgentBayQuotaOutcome.PROVISIONED,
            comment="agentbay-quota:provision",
        )

    @staticmethod
    def _classify_phase_target(
        existing: Optional[AgentBayQuotaReservation],
        *,
        user_key: str,
        operation_key: str,
        provider_id: str,
    ) -> Optional[AgentBayQuotaOutcome]:
        if existing is None:
            return AgentBayQuotaOutcome.STALE_OPERATION
        if existing.user_key != user_key:
            raise AgentBayQuotaInconsistentError(
                "AgentBay reservation ownership is inconsistent"
            )
        if existing.operation_key != operation_key:
            return AgentBayQuotaOutcome.STALE_OPERATION
        if existing.phase is AgentBayReservationPhase.PROVISIONED:
            if existing.provider_id == provider_id:
                return AgentBayQuotaOutcome.ALREADY_PROVISIONED
            raise AgentBayQuotaInconsistentError(
                "AgentBay operation is bound to a different provider"
            )
        return None

    async def _phase_mutation(
        self,
        *,
        selector: Mapping[str, Any],
        update: Mapping[str, Any],
        session_key: str,
        user_key: str,
        operation_key: str,
        operation_id: str,
        provider_id: str,
        success: AgentBayQuotaOutcome,
        comment: str,
    ) -> AgentBayQuotaResult:
        ambiguous = False
        try:
            document = await self._bounded(
                self._collection().find_one_and_update(
                    selector,
                    update,
                    return_document=ReturnDocument.AFTER,
                    maxTimeMS=self._max_time_ms,
                    comment=comment,
                )
            )
        except asyncio.CancelledError:
            await self._best_effort_postread()
            raise
        except Exception:
            ambiguous = True
            document = None
        post = document if document is not None else await self._read_raw()
        if post is None:
            raise AgentBayQuotaUnavailableError(
                "AgentBay phase transition could not be verified"
            )
        self._assert_configuration(post)
        existing = self._find_reservation(post, session_key)
        classification = self._classify_phase_target(
            existing,
            user_key=user_key,
            operation_key=operation_key,
            provider_id=provider_id,
        )
        if classification is AgentBayQuotaOutcome.ALREADY_PROVISIONED:
            return self._result(
                success if document is not None or ambiguous else classification,
                post,
                existing,
            )
        if classification is not None:
            return self._result(classification, post, existing)
        if ambiguous:
            return self._result(AgentBayQuotaOutcome.RETRYABLE, post, existing)
        raise AgentBayQuotaInconsistentError(
            "AgentBay phase transition failed its atomic compare-and-set"
        )

    async def replace_operation(
        self,
        session_id: str,
        user_id: str,
        expected_operation_id: str,
        replacement_operation_id: str,
        *,
        expected_provider_id: Optional[str] = None,
    ) -> AgentBayQuotaResult:
        session_key = self.session_key(session_id)
        user_key = self.user_key(user_id)
        expected_operation_id = self._identifier(
            expected_operation_id, "expected_operation_id", 256
        )
        replacement_operation_id = self._identifier(
            replacement_operation_id, "replacement_operation_id", 256
        )
        old_operation_key = self.operation_key(expected_operation_id)
        new_operation_key = self.operation_key(replacement_operation_id)
        if old_operation_key == new_operation_key:
            raise ValueError("Replacement operation ID must be different")
        before = await self._read_raw()
        existing = self._classify_ready_document(
            before,
            session_key=session_key,
            user_key=user_key,
            operation_key=new_operation_key,
        )
        if existing is None:
            return self._result(AgentBayQuotaOutcome.STALE_OPERATION, before)
        if existing.operation_key == new_operation_key:
            outcome = (
                AgentBayQuotaOutcome.ALREADY_REPLACED
                if existing.phase is AgentBayReservationPhase.RESERVED
                else AgentBayQuotaOutcome.EXISTING_PROVISIONED
            )
            return self._result(outcome, before, existing)
        if existing.operation_key != old_operation_key:
            return self._result(
                AgentBayQuotaOutcome.STALE_OPERATION, before, existing
            )
        if existing.phase is AgentBayReservationPhase.PROVISIONED:
            if expected_provider_id is None:
                raise AgentBayQuotaInconsistentError(
                    "Replacing a provisioned session requires its provider handle"
                )
            expected_provider_id = self._identifier(
                expected_provider_id, "expected_provider_id", 1024
            )
            if existing.provider_id != expected_provider_id:
                return self._result(
                    AgentBayQuotaOutcome.STALE_OPERATION, before, existing
                )
        elif expected_provider_id is not None:
            raise AgentBayQuotaInconsistentError(
                "Reserved AgentBay operation unexpectedly supplied a provider handle"
            )

        operation_sessions = before.get("operation_sessions") or {}
        if not isinstance(operation_sessions, Mapping):
            raise AgentBayQuotaInconsistentError(
                "AgentBay operation index is inconsistent"
            )
        if operation_sessions.get(new_operation_key) not in (None, session_key):
            raise AgentBayQuotaInconsistentError(
                "Replacement operation is already bound to another session"
            )
        phase = existing.phase.value
        now = datetime.now(UTC)
        selector: dict[str, Any] = {
            **self._config_filter(),
            "bootstrap_state": AgentBayBootstrapState.READY.value,
            f"reservations.{session_key}.user_key": user_key,
            f"reservations.{session_key}.operation_key": old_operation_key,
            f"reservations.{session_key}.operation_id": expected_operation_id,
            f"reservations.{session_key}.phase": phase,
            f"operation_sessions.{old_operation_key}": session_key,
            f"operation_sessions.{new_operation_key}": {"$exists": False},
        }
        unset: dict[str, Any] = {
            f"operation_sessions.{old_operation_key}": "",
            f"reservations.{session_key}.provider_id": "",
            f"reservations.{session_key}.provider_key": "",
        }
        if existing.phase is AgentBayReservationPhase.PROVISIONED:
            assert expected_provider_id is not None
            assert existing.provider_key is not None
            selector[f"reservations.{session_key}.provider_id"] = expected_provider_id
            selector[f"reservations.{session_key}.provider_key"] = existing.provider_key
            selector[f"provider_sessions.{existing.provider_key}"] = session_key
            unset[f"provider_sessions.{existing.provider_key}"] = ""
        update = {
            "$set": {
                f"reservations.{session_key}.operation_key": new_operation_key,
                f"reservations.{session_key}.operation_id": replacement_operation_id,
                f"reservations.{session_key}.phase": AgentBayReservationPhase.RESERVED.value,
                f"reservations.{session_key}.updated_at": now,
                f"operation_sessions.{new_operation_key}": session_key,
                "updated_at": now,
            },
            "$unset": unset,
            "$inc": {"revision": 1},
        }
        ambiguous = False
        try:
            document = await self._bounded(
                self._collection().find_one_and_update(
                    selector,
                    update,
                    return_document=ReturnDocument.AFTER,
                    maxTimeMS=self._max_time_ms,
                    comment="agentbay-quota:replace-operation",
                )
            )
        except asyncio.CancelledError:
            await self._best_effort_postread()
            raise
        except Exception:
            ambiguous = True
            document = None
        post = document if document is not None else await self._read_raw()
        if post is None:
            raise AgentBayQuotaUnavailableError(
                "AgentBay replacement outcome could not be verified"
            )
        self._assert_configuration(post)
        current = self._find_reservation(post, session_key)
        if current is None or current.user_key != user_key:
            return self._result(AgentBayQuotaOutcome.STALE_OPERATION, post, current)
        if current.operation_key == new_operation_key:
            outcome = (
                AgentBayQuotaOutcome.REPLACED
                if document is not None or ambiguous
                else AgentBayQuotaOutcome.ALREADY_REPLACED
            )
            return self._result(outcome, post, current)
        if current.operation_key != old_operation_key:
            return self._result(AgentBayQuotaOutcome.STALE_OPERATION, post, current)
        if ambiguous:
            return self._result(AgentBayQuotaOutcome.RETRYABLE, post, current)
        raise AgentBayQuotaInconsistentError(
            "AgentBay replacement failed its atomic compare-and-set"
        )

    async def release(
        self,
        session_id: str,
        user_id: str,
        operation_id: str,
        *,
        provider_id: Optional[str] = None,
    ) -> AgentBayQuotaResult:
        """Release one exact operation even when this replica's config mismatches."""

        session_key = self.session_key(session_id)
        user_key = self.user_key(user_id)
        operation_id = self._identifier(operation_id, "operation_id", 256)
        operation_key = self.operation_key(operation_id)
        before = await self._read_raw()
        if before is None:
            raise AgentBayQuotaNotInitializedError(
                "AgentBay quota ledger disappeared during cleanup"
            )
        # Cleanup deliberately tolerates deployment/config drift, but it must
        # never mutate an internally corrupt document.  Validate the complete
        # bounded inventory before using one reservation as an exact cleanup
        # authority.
        self._validate_invariants(before)
        existing = self._find_reservation(before, session_key)
        if existing is None:
            return self._result(AgentBayQuotaOutcome.ALREADY_RELEASED, before)
        if existing.user_key != user_key:
            raise AgentBayQuotaInconsistentError(
                "AgentBay release ownership is inconsistent"
            )
        if existing.operation_key != operation_key:
            return self._result(
                AgentBayQuotaOutcome.STALE_OPERATION, before, existing
            )
        expected_provider_key: Optional[str] = None
        if existing.phase is AgentBayReservationPhase.PROVISIONED:
            if provider_id is None:
                raise AgentBayQuotaInconsistentError(
                    "Provisioned AgentBay release requires the provider handle"
                )
            provider_id = self._identifier(provider_id, "provider_id", 1024)
            if existing.provider_id != provider_id:
                return self._result(
                    AgentBayQuotaOutcome.STALE_OPERATION, before, existing
                )
            expected_provider_key = self.provider_key(provider_id)
            if existing.provider_key != expected_provider_key:
                raise AgentBayQuotaInconsistentError(
                    "AgentBay provider index is inconsistent"
                )
        elif provider_id is not None:
            raise AgentBayQuotaInconsistentError(
                "Reserved AgentBay release unexpectedly supplied a provider handle"
            )

        selector: dict[str, Any] = {
            "_id": LEDGER_DOCUMENT_ID,
            "total": {"$gt": 0},
            f"user_counts.{user_key}": {"$gt": 0},
            f"reservations.{session_key}.user_key": user_key,
            f"reservations.{session_key}.operation_key": operation_key,
            f"reservations.{session_key}.operation_id": operation_id,
            f"reservations.{session_key}.phase": existing.phase.value,
            f"operation_sessions.{operation_key}": session_key,
        }
        unset = {
            f"reservations.{session_key}": "",
            f"operation_sessions.{operation_key}": "",
        }
        if existing.phase is AgentBayReservationPhase.PROVISIONED:
            assert provider_id is not None and expected_provider_key is not None
            selector[f"reservations.{session_key}.provider_id"] = provider_id
            selector[f"reservations.{session_key}.provider_key"] = expected_provider_key
            selector[f"provider_sessions.{expected_provider_key}"] = session_key
            unset[f"provider_sessions.{expected_provider_key}"] = ""
        update = {
            "$unset": unset,
            "$inc": {
                "total": -1,
                f"user_counts.{user_key}": -1,
                "revision": 1,
            },
            "$set": {"updated_at": datetime.now(UTC)},
        }
        ambiguous = False
        try:
            document = await self._bounded(
                self._collection().find_one_and_update(
                    selector,
                    update,
                    return_document=ReturnDocument.AFTER,
                    maxTimeMS=self._max_time_ms,
                    comment="agentbay-quota:release",
                )
            )
        except asyncio.CancelledError:
            await self._best_effort_postread()
            raise
        except Exception:
            ambiguous = True
            document = None
        post = document if document is not None else await self._read_raw()
        if post is None:
            raise AgentBayQuotaUnavailableError(
                "AgentBay quota release could not be verified because the "
                "ledger disappeared"
            )
        self._validate_invariants(post)
        current = self._find_reservation(post, session_key)
        if current is None:
            return self._result(AgentBayQuotaOutcome.RELEASED, post)
        if current.user_key != user_key:
            raise AgentBayQuotaInconsistentError(
                "AgentBay release ownership is inconsistent"
            )
        if current.operation_key != operation_key:
            return self._result(
                AgentBayQuotaOutcome.STALE_OPERATION, post, current
            )
        if ambiguous:
            return self._result(AgentBayQuotaOutcome.RETRYABLE, post, current)
        raise AgentBayQuotaInconsistentError(
            "AgentBay release failed its atomic compare-and-set"
        )

    async def migrate_configuration(
        self,
        *,
        from_deployment_id: str,
        from_config_version: str,
        from_max_total: int,
        from_max_per_user: int,
        expected_revision: int,
    ) -> AgentBayQuotaResult:
        """Explicit admin CAS for deployment/version/cap changes.

        Ordinary reserve/adopt/provision calls never perform this migration.
        Existing totals must fit the new limits before the stored configuration
        is changed.
        """

        document = await self._read_raw()
        if document is None:
            raise AgentBayQuotaNotInitializedError(
                "AgentBay quota ledger has not been initialized"
            )
        try:
            self._assert_configuration(document)
        except AgentBayQuotaConfigurationError:
            pass
        else:
            return self._result(AgentBayQuotaOutcome.ALREADY_CONFIGURED, document)
        self._validate_invariants(document)
        reservations = document.get("reservations") or {}
        assert isinstance(reservations, Mapping)
        if any(
            self._reservation_from_raw(session_key, raw).phase
            is AgentBayReservationPhase.RESERVED
            for session_key, raw in reservations.items()
        ):
            raise AgentBayQuotaInconsistentError(
                "AgentBay configuration cannot change during provisioning"
            )

        old_filter = {
            "_id": LEDGER_DOCUMENT_ID,
            "schema_version": LEDGER_SCHEMA_VERSION,
            "deployment_key": self._digest(
                "deployment",
                self._identifier(from_deployment_id, "from_deployment_id", 512),
            ),
            "config_version": self._identifier(
                from_config_version, "from_config_version", 128
            ),
            "max_total": int(from_max_total),
            "max_per_user": int(from_max_per_user),
            "revision": int(expected_revision),
        }
        if any(document.get(key) != value for key, value in old_filter.items()):
            raise AgentBayQuotaConfigurationError(
                "AgentBay quota source configuration does not match"
            )
        total = int(document.get("total", -1))
        user_counts = document.get("user_counts") or {}
        if not isinstance(user_counts, Mapping) or total < 0:
            raise AgentBayQuotaInconsistentError(
                "AgentBay quota counters are inconsistent"
            )
        if total > self._max_total:
            raise AgentBayQuotaExceededError(AgentBayQuotaScope.GLOBAL)
        if any(int(count) > self._max_per_user for count in user_counts.values()):
            raise AgentBayQuotaExceededError(AgentBayQuotaScope.USER)

        update = {
            "$set": {
                "deployment_key": self._deployment_key,
                "config_version": self._config_version,
                "max_total": self._max_total,
                "max_per_user": self._max_per_user,
                "updated_at": datetime.now(UTC),
            },
            "$inc": {"revision": 1},
        }
        try:
            migrated = await self._bounded(
                self._collection().find_one_and_update(
                    old_filter,
                    update,
                    return_document=ReturnDocument.AFTER,
                    maxTimeMS=self._max_time_ms,
                    comment="agentbay-quota:migrate-config",
                )
            )
        except asyncio.CancelledError:
            await self._best_effort_postread()
            raise
        except Exception as exc:
            post = await self._read_raw()
            if post is not None:
                try:
                    self._assert_configuration(post)
                except AgentBayQuotaConfigurationError:
                    pass
                else:
                    return self._result(AgentBayQuotaOutcome.RECONFIGURED, post)
            raise AgentBayQuotaUnavailableError(
                "AgentBay quota configuration migration could not be verified"
            ) from exc
        if migrated is not None:
            return self._result(AgentBayQuotaOutcome.RECONFIGURED, migrated)
        post = await self._read_raw()
        if post is not None:
            try:
                self._assert_configuration(post)
            except AgentBayQuotaConfigurationError:
                pass
            else:
                return self._result(
                    AgentBayQuotaOutcome.ALREADY_CONFIGURED, post
                )
        raise AgentBayQuotaUnavailableError(
            "AgentBay quota configuration migration lost its CAS race"
        )
