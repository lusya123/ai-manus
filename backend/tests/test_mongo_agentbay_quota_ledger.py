from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from datetime import datetime
from typing import Any, Mapping

import pytest
from pymongo.errors import DuplicateKeyError

from app.domain.external.agentbay_quota import (
    AgentBayBootstrapState,
    AgentBayQuotaBootstrapError,
    AgentBayQuotaConfigurationError,
    AgentBayQuotaExceededError,
    AgentBayQuotaInconsistentError,
    AgentBayQuotaInventoryEntry,
    AgentBayQuotaNotInitializedError,
    AgentBayQuotaOutcome,
    AgentBayQuotaScope,
    AgentBayReservationPhase,
)
from app.infrastructure.repositories.external.sandbox.mongo_agentbay_quota import (
    LEDGER_COLLECTION,
    MongoAgentBayQuotaLedger,
)


_MISSING = object()


def _get_path(document: Mapping[str, Any], path: str) -> Any:
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _set_path(document: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    target = document
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = deepcopy(value)


def _unset_path(document: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    target: Any = document
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target:
            return
        target = target[part]
    if isinstance(target, dict):
        target.pop(parts[-1], None)


def _matches(document: Mapping[str, Any], selector: Mapping[str, Any]) -> bool:
    for key, expected in selector.items():
        if key == "$or":
            if not any(_matches(document, branch) for branch in expected):
                return False
            continue
        if key == "$and":
            if not all(_matches(document, branch) for branch in expected):
                return False
            continue
        actual = _get_path(document, key)
        if isinstance(expected, Mapping):
            for operator, operand in expected.items():
                if operator == "$exists":
                    if (actual is not _MISSING) is not bool(operand):
                        return False
                elif operator == "$lt":
                    if actual is _MISSING or not actual < operand:
                        return False
                elif operator == "$gt":
                    if actual is _MISSING or not actual > operand:
                        return False
                else:  # pragma: no cover - catches accidental query expansion
                    raise AssertionError(f"unsupported fake operator: {operator}")
        elif actual is _MISSING or actual != expected:
            return False
    return True


class InMemoryAtomicCollection:
    """Small locked Mongo subset that executes the repository's real CAS shape."""

    def __init__(self) -> None:
        self.document: dict[str, Any] | None = None
        self.options: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self.failures: dict[str, list[str]] = {}
        self.update_calls: list[tuple[str | None, dict[str, Any]]] = []

    def with_options(self, **options):
        self.options = options
        return self

    def fail_once(self, comment: str, when: str) -> None:
        self.failures.setdefault(comment, []).append(when)

    def _failure(self, comment: str | None) -> str | None:
        values = self.failures.get(comment or "")
        return values.pop(0) if values else None

    async def find_one(self, selector, **kwargs):
        async with self._lock:
            if self.document is None or not _matches(self.document, selector):
                return None
            return deepcopy(self.document)

    async def insert_one(self, document, comment=None, **kwargs):
        async with self._lock:
            failure = self._failure(comment)
            if failure == "before":
                raise ConnectionError("injected pre-write failure")
            if self.document is not None:
                raise DuplicateKeyError("duplicate fixed ledger")
            self.document = deepcopy(document)
            if failure == "after":
                raise ConnectionError("injected ambiguous insert")
            return object()

    async def find_one_and_update(
        self, selector, update, return_document=None, comment=None, **kwargs
    ):
        async with self._lock:
            self.update_calls.append((comment, deepcopy(selector)))
            failure = self._failure(comment)
            if failure == "before":
                raise ConnectionError("injected pre-write failure")
            if self.document is None or not _matches(self.document, selector):
                return None
            for path, value in update.get("$set", {}).items():
                _set_path(self.document, path, value)
            for path in update.get("$unset", {}):
                _unset_path(self.document, path)
            for path, amount in update.get("$inc", {}).items():
                current = _get_path(self.document, path)
                _set_path(
                    self.document,
                    path,
                    (0 if current is _MISSING else current) + amount,
                )
            result = deepcopy(self.document)
            if failure == "after":
                raise ConnectionError("injected ambiguous post-write failure")
            return result


class PauseAfterCommitCollection(InMemoryAtomicCollection):
    def __init__(self) -> None:
        super().__init__()
        self.pause_comment: str | None = None
        self.committed = asyncio.Event()
        self.resume = asyncio.Event()

    async def find_one_and_update(
        self, selector, update, return_document=None, comment=None, **kwargs
    ):
        result = await super().find_one_and_update(
            selector,
            update,
            return_document=return_document,
            comment=comment,
            **kwargs,
        )
        if result is not None and comment == self.pause_comment:
            self.committed.set()
            await self.resume.wait()
        return result


def ledger(
    collection: Any,
    *,
    deployment: str = "deployment-a",
    total: int = 3,
    per_user: int = 2,
    config_version: str = "1",
    timeout: float = 0.5,
) -> MongoAgentBayQuotaLedger:
    return MongoAgentBayQuotaLedger(
        deployment_id=deployment,
        max_total=total,
        max_per_user=per_user,
        config_version=config_version,
        command_timeout_seconds=timeout,
        collection=collection,
    )


def test_single_document_ledger_rejects_more_than_twenty_slots():
    with pytest.raises(ValueError, match="cannot exceed 20"):
        ledger(InMemoryAtomicCollection(), total=21, per_user=1)


async def ready(subject: MongoAgentBayQuotaLedger) -> None:
    await subject.ensure_ledger()
    transition = await subject.begin_reconciliation()
    await subject.reconcile_inventory([], expected_revision=transition.revision or -1)


async def test_ledger_uses_primary_majority_journal_and_explicit_bootstrap():
    collection = InMemoryAtomicCollection()
    subject = ledger(collection)

    initialized = await subject.ensure_ledger()
    assert initialized.outcome is AgentBayQuotaOutcome.LEDGER_CREATED
    assert initialized.bootstrap_state is AgentBayBootstrapState.REQUIRED
    with pytest.raises(AgentBayQuotaBootstrapError):
        await subject.reserve("session", "user", "operation")

    transition = await subject.begin_reconciliation()
    assert transition.bootstrap_state is AgentBayBootstrapState.RECONCILING
    reconciled = await subject.reconcile_inventory(
        [], expected_revision=transition.revision or -1
    )
    assert reconciled.bootstrap_state is AgentBayBootstrapState.READY

    assert collection.options is not None
    assert collection.options["read_preference"].name == "Primary"
    assert collection.options["read_concern"].level == "majority"
    write_concern = collection.options["write_concern"].document
    assert write_concern["w"] == "majority"
    assert write_concern["j"] is True
    assert write_concern["wtimeout"] == 500


async def test_atomic_concurrency_enforces_per_user_then_global_caps():
    collection = InMemoryAtomicCollection()
    subject = ledger(collection, total=3, per_user=2)
    await ready(subject)

    attempts = await asyncio.gather(
        *(
            subject.reserve(f"session-a-{index}", "user-a", f"op-a-{index}")
            for index in range(12)
        ),
        return_exceptions=True,
    )
    assert sum(
        getattr(result, "outcome", None) is AgentBayQuotaOutcome.RESERVED
        for result in attempts
    ) == 2
    rejected = [
        result for result in attempts if isinstance(result, AgentBayQuotaExceededError)
    ]
    assert len(rejected) == 10
    assert all(result.scope is AgentBayQuotaScope.USER for result in rejected)

    third = await subject.reserve("session-b", "user-b", "op-b")
    assert third.outcome is AgentBayQuotaOutcome.RESERVED
    with pytest.raises(AgentBayQuotaExceededError) as exc_info:
        await subject.reserve("session-c", "user-c", "op-c")
    assert exc_info.value.scope is AgentBayQuotaScope.GLOBAL


async def test_reserve_marks_a_different_operation_stale_and_never_overwrites_it():
    collection = InMemoryAtomicCollection()
    subject = ledger(collection)
    await ready(subject)

    created = await subject.reserve("private-session", "private-user", "stable-op")
    existing = await subject.reserve("private-session", "private-user", "new-op")

    assert created.outcome is AgentBayQuotaOutcome.RESERVED
    assert existing.outcome is AgentBayQuotaOutcome.STALE_OPERATION
    assert existing.reservation is not None
    assert existing.reservation.operation_id == "stable-op"
    raw = repr(collection.document)
    assert "private-session" not in raw
    assert "private-user" not in raw
    assert "stable-op" in raw
    reserve_selector = next(
        selector
        for comment, selector in collection.update_calls
        if comment == "agentbay-quota:reserve"
    )
    assert isinstance(reserve_selector.get("revision"), int)


async def test_provision_replace_stales_old_cleanup_and_counts_never_go_negative():
    collection = InMemoryAtomicCollection()
    subject = ledger(collection, total=1, per_user=1)
    await ready(subject)
    await subject.reserve("session", "user", "operation-a")
    provisioned = await subject.mark_provisioned(
        "session", "user", "operation-a", "provider-a"
    )
    assert provisioned.outcome is AgentBayQuotaOutcome.PROVISIONED

    replacement = await subject.replace_operation(
        "session",
        "user",
        "operation-a",
        "operation-b",
        expected_provider_id="provider-a",
    )
    assert replacement.outcome is AgentBayQuotaOutcome.REPLACED
    assert replacement.reservation is not None
    assert replacement.reservation.phase is AgentBayReservationPhase.RESERVED

    stale_mark = await subject.mark_provisioned(
        "session", "user", "operation-a", "provider-a"
    )
    stale_release = await subject.release(
        "session", "user", "operation-a", provider_id="provider-a"
    )
    assert stale_mark.outcome is AgentBayQuotaOutcome.STALE_OPERATION
    assert stale_release.outcome is AgentBayQuotaOutcome.STALE_OPERATION

    released = await subject.release("session", "user", "operation-b")
    duplicate = await subject.release("session", "user", "operation-b")
    assert released.outcome is AgentBayQuotaOutcome.RELEASED
    assert duplicate.outcome is AgentBayQuotaOutcome.ALREADY_RELEASED
    assert collection.document is not None
    assert collection.document["total"] == 0
    assert min(collection.document["user_counts"].values(), default=0) >= 0


async def test_full_inventory_reconcile_is_all_or_nothing_and_revision_guarded():
    collection = InMemoryAtomicCollection()
    subject = ledger(collection, total=3, per_user=2)
    await subject.ensure_ledger()
    transition = await subject.begin_reconciliation()
    oversized = [
        AgentBayQuotaInventoryEntry(
            session_id=f"session-{index}",
            user_id="same-user",
            operation_id=f"operation-{index}",
            provider_id=f"provider-{index}",
        )
        for index in range(3)
    ]
    with pytest.raises(AgentBayQuotaExceededError) as exc_info:
        await subject.reconcile_inventory(
            oversized, expected_revision=transition.revision or -1
        )
    assert exc_info.value.scope is AgentBayQuotaScope.USER
    snapshot = await subject.snapshot()
    assert snapshot.bootstrap_state is AgentBayBootstrapState.RECONCILING
    assert snapshot.total == 0

    adopted = oversized[:2]
    reconciled = await subject.reconcile_inventory(
        adopted, expected_revision=transition.revision or -1
    )
    assert reconciled.outcome is AgentBayQuotaOutcome.RECONCILED
    assert reconciled.total == 2
    raw = repr(collection.document)
    assert "session-0" not in raw
    assert "same-user" not in raw


async def test_reconcile_refuses_to_overwrite_an_inflight_reservation():
    collection = InMemoryAtomicCollection()
    subject = ledger(collection)
    await ready(subject)
    await subject.reserve("session", "user", "operation")
    transition = await subject.begin_reconciliation()

    with pytest.raises(AgentBayQuotaInconsistentError, match="in-flight"):
        await subject.reconcile_inventory(
            [], expected_revision=transition.revision or -1
        )
    snapshot = await subject.snapshot()
    assert snapshot.bootstrap_state is AgentBayBootstrapState.RECONCILING
    assert snapshot.total == 1


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda document, session_key, user_key: document.__setitem__("total", 0),
        lambda document, session_key, user_key: document["user_counts"].__setitem__(
            user_key, 0
        ),
        lambda document, session_key, user_key: document[
            "operation_sessions"
        ].clear(),
        lambda document, session_key, user_key: document["reservations"][
            session_key
        ].__setitem__("operation_key", "a" * 64),
    ],
    ids=["total", "user-count", "operation-index", "operation-digest"],
)
async def test_corrupt_counts_and_indexes_fail_closed_before_reserve(corrupt):
    collection = InMemoryAtomicCollection()
    subject = ledger(collection)
    await ready(subject)
    await subject.reserve("session", "user", "operation")
    assert collection.document is not None
    session_key = subject.session_key("session")
    user_key = subject.user_key("user")
    corrupt(collection.document, session_key, user_key)

    with pytest.raises(AgentBayQuotaInconsistentError):
        await subject.reserve("another-session", "another-user", "another-op")
    assert len(collection.document["reservations"]) == 1


@pytest.mark.parametrize("corruption", ["provider-index", "provider-digest"])
async def test_corrupt_provider_metadata_fails_closed(corruption):
    collection = InMemoryAtomicCollection()
    subject = ledger(collection)
    await ready(subject)
    await subject.reserve("session", "user", "operation")
    await subject.mark_provisioned(
        "session", "user", "operation", "provider"
    )
    assert collection.document is not None
    session_key = subject.session_key("session")
    reservation = collection.document["reservations"][session_key]
    if corruption == "provider-index":
        collection.document["provider_sessions"].clear()
    else:
        old_key = reservation["provider_key"]
        collection.document["provider_sessions"].pop(old_key)
        reservation["provider_key"] = "b" * 64
        collection.document["provider_sessions"]["b" * 64] = session_key

    with pytest.raises(AgentBayQuotaInconsistentError):
        await subject.reserve("another-session", "another-user", "another-op")
    assert len(collection.document["reservations"]) == 1


async def test_incremental_adopt_is_revision_cas_and_never_marks_ready():
    collection = InMemoryAtomicCollection()
    subject = ledger(collection)
    await subject.ensure_ledger()
    transition = await subject.begin_reconciliation()

    adopted = await subject.adopt_existing(
        "session", "user", "operation", "provider"
    )

    assert adopted.outcome is AgentBayQuotaOutcome.ADOPTED
    assert adopted.bootstrap_state is AgentBayBootstrapState.RECONCILING
    adopt_selector = next(
        selector
        for comment, selector in collection.update_calls
        if comment == "agentbay-quota:adopt"
    )
    assert adopt_selector["revision"] == transition.revision


async def test_config_mismatch_blocks_create_paths_but_exact_cleanup_still_works():
    collection = InMemoryAtomicCollection()
    original = ledger(collection, deployment="deployment-a")
    await ready(original)
    await original.reserve("session", "user", "operation")
    await original.mark_provisioned(
        "session", "user", "operation", "provider"
    )

    mismatched = ledger(collection, deployment="deployment-b")
    with pytest.raises(AgentBayQuotaConfigurationError):
        await mismatched.reserve("another", "user", "another-operation")
    with pytest.raises(AgentBayQuotaConfigurationError):
        await mismatched.adopt_existing(
            "another", "user", "another-operation", "another-provider"
        )

    recovered = await mismatched.get_reservation_for_cleanup("session", "user")
    assert recovered is not None
    assert recovered.operation_id == "operation"
    assert recovered.provider_id == "provider"
    with pytest.raises(AgentBayQuotaInconsistentError, match="ownership"):
        await mismatched.get_reservation_for_cleanup("session", "wrong-user")

    cleanup = await mismatched.release(
        "session", "user", "operation", provider_id="provider"
    )
    assert cleanup.outcome is AgentBayQuotaOutcome.RELEASED
    assert collection.document is not None
    assert collection.document["total"] == 0


async def test_release_fails_closed_when_the_authoritative_ledger_is_missing():
    subject = ledger(InMemoryAtomicCollection())

    with pytest.raises(AgentBayQuotaNotInitializedError):
        await subject.release("session", "user", "operation")


async def test_release_refuses_to_mutate_a_corrupt_ledger():
    collection = InMemoryAtomicCollection()
    subject = ledger(collection)
    await ready(subject)
    await subject.reserve("session", "user", "operation")
    collection.document["total"] = 2

    with pytest.raises(AgentBayQuotaInconsistentError):
        await subject.release("session", "user", "operation")

    assert collection.document["total"] == 2


async def test_ambiguous_writes_use_postconditions_and_exact_retries():
    collection = InMemoryAtomicCollection()
    subject = ledger(collection)
    await ready(subject)

    collection.fail_once("agentbay-quota:reserve", "after")
    reserved = await subject.reserve("session", "user", "operation")
    assert reserved.outcome is AgentBayQuotaOutcome.RESERVED

    collection.fail_once("agentbay-quota:provision", "after")
    provisioned = await subject.mark_provisioned(
        "session", "user", "operation", "provider"
    )
    assert provisioned.outcome is AgentBayQuotaOutcome.PROVISIONED

    collection.fail_once("agentbay-quota:release", "before")
    retryable = await subject.release(
        "session", "user", "operation", provider_id="provider"
    )
    assert retryable.outcome is AgentBayQuotaOutcome.RETRYABLE

    collection.fail_once("agentbay-quota:release", "after")
    released = await subject.release(
        "session", "user", "operation", provider_id="provider"
    )
    assert released.outcome is AgentBayQuotaOutcome.RELEASED
    assert collection.document is not None
    assert collection.document["total"] == 0


async def test_timeout_after_commit_is_recovered_by_majority_postread():
    collection = PauseAfterCommitCollection()
    subject = ledger(collection, timeout=0.01)
    await ready(subject)
    collection.pause_comment = "agentbay-quota:reserve"

    result = await subject.reserve("session", "user", "operation")

    assert result.outcome is AgentBayQuotaOutcome.RESERVED
    assert collection.committed.is_set()
    assert collection.document is not None
    assert collection.document["total"] == 1


async def test_cancellation_after_commit_propagates_and_stable_retry_recovers():
    collection = PauseAfterCommitCollection()
    subject = ledger(collection, timeout=1.0)
    await ready(subject)
    collection.pause_comment = "agentbay-quota:reserve"
    task = asyncio.create_task(subject.reserve("session", "user", "operation"))
    await asyncio.wait_for(collection.committed.wait(), timeout=0.5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    collection.pause_comment = None
    recovered = await subject.reserve("session", "user", "operation")
    assert recovered.outcome is AgentBayQuotaOutcome.EXISTING_RESERVED
    assert recovered.reservation is not None
    assert recovered.reservation.operation_id == "operation"


async def test_explicit_configuration_migration_checks_existing_usage():
    collection = InMemoryAtomicCollection()
    old = ledger(collection, deployment="old", total=3, per_user=2)
    await ready(old)
    await old.reserve("session-1", "user", "operation-1")
    await old.reserve("session-2", "user", "operation-2")
    await old.mark_provisioned(
        "session-1", "user", "operation-1", "provider-1"
    )
    await old.mark_provisioned(
        "session-2", "user", "operation-2", "provider-2"
    )
    old_snapshot = await old.snapshot()

    too_small = ledger(collection, deployment="new", total=3, per_user=1)
    with pytest.raises(AgentBayQuotaExceededError) as exc_info:
        await too_small.migrate_configuration(
            from_deployment_id="old",
            from_config_version="1",
            from_max_total=3,
            from_max_per_user=2,
            expected_revision=old_snapshot.revision,
        )
    assert exc_info.value.scope is AgentBayQuotaScope.USER

    migrated_repo = ledger(collection, deployment="new", total=4, per_user=2)
    migrated = await migrated_repo.migrate_configuration(
        from_deployment_id="old",
        from_config_version="1",
        from_max_total=3,
        from_max_per_user=2,
        expected_revision=old_snapshot.revision,
    )
    assert migrated.outcome is AgentBayQuotaOutcome.RECONFIGURED
    assert (await migrated_repo.snapshot()).total == 2


_REAL_MONGO_URI = os.getenv("AGENTBAY_QUOTA_TEST_MONGODB_URI")


@pytest.mark.skipif(
    not _REAL_MONGO_URI,
    reason="set AGENTBAY_QUOTA_TEST_MONGODB_URI for real Mongo CAS coverage",
)
async def test_real_mongo_concurrency_and_stale_operation_guards():
    from pymongo.asynchronous.mongo_client import AsyncMongoClient

    client = AsyncMongoClient(_REAL_MONGO_URI, serverSelectionTimeoutMS=2000)
    database_name = (
        "test_agentbay_quota_ledger_"
        + datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    )
    database = client[database_name]
    try:
        subject = ledger(
            database[LEDGER_COLLECTION], total=3, per_user=2, timeout=2.0
        )
        await ready(subject)
        attempts = await asyncio.gather(
            *(
                subject.reserve(
                    f"real-session-{index}", "real-user", f"real-op-{index}"
                )
                for index in range(10)
            ),
            return_exceptions=True,
        )
        accepted = [
            result
            for result in attempts
            if getattr(result, "outcome", None) is AgentBayQuotaOutcome.RESERVED
        ]
        assert len(accepted) == 2
        assert sum(
            isinstance(result, AgentBayQuotaExceededError) for result in attempts
        ) == 8

        first = accepted[0].reservation
        assert first is not None
        # Use a separate slot for a complete replacement lifecycle.
        await subject.reserve("replace-session", "replace-user", "operation-a")
        await subject.mark_provisioned(
            "replace-session", "replace-user", "operation-a", "provider-a"
        )
        await subject.replace_operation(
            "replace-session",
            "replace-user",
            "operation-a",
            "operation-b",
            expected_provider_id="provider-a",
        )
        stale = await subject.release(
            "replace-session",
            "replace-user",
            "operation-a",
            provider_id="provider-a",
        )
        assert stale.outcome is AgentBayQuotaOutcome.STALE_OPERATION
        snapshot = await subject.snapshot()
        replacement = next(
            entry
            for entry in snapshot.reservations
            if entry.operation_id == "operation-b"
        )
        assert replacement.phase is AgentBayReservationPhase.RESERVED
    finally:
        await client.drop_database(database_name)
        await client.close()
