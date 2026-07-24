"""Failure-injection tests for the durable AgentBay lifecycle coordinator."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from app.domain.external.agentbay_quota import (
    AgentBayBootstrapState,
    AgentBayQuotaBootstrapError,
    AgentBayQuotaConfigurationError,
    AgentBayQuotaExceededError,
    AgentBayQuotaInconsistentError,
    AgentBayQuotaOutcome,
    AgentBayQuotaReservation,
    AgentBayQuotaResult,
    AgentBayQuotaScope,
    AgentBayQuotaSnapshot,
    AgentBayQuotaUnavailableError,
    AgentBayReservationPhase,
)
from app.domain.external.sandbox import SandboxUnavailableError
from app.domain.models.session import Session
from app.infrastructure.external.sandbox.agentbay_provisioner import (
    AgentBayProvisioner,
)


def quota_result(
    outcome: AgentBayQuotaOutcome,
    *,
    reservation: AgentBayQuotaReservation | None = None,
    state: AgentBayBootstrapState = AgentBayBootstrapState.READY,
    revision: int | None = 1,
) -> AgentBayQuotaResult:
    return AgentBayQuotaResult(
        outcome=outcome,
        reservation=reservation,
        bootstrap_state=state,
        revision=revision,
    )


def reservation(
    operation_id: str = "operation-1",
    *,
    phase: AgentBayReservationPhase = AgentBayReservationPhase.RESERVED,
    provider_id: str | None = None,
) -> AgentBayQuotaReservation:
    return AgentBayQuotaReservation(
        session_key="session-key",
        user_key="user-key",
        operation_key=f"operation-key:{operation_id}",
        operation_id=operation_id,
        phase=phase,
        provider_id=provider_id,
        provider_key=f"provider-key:{provider_id}" if provider_id else None,
    )


class FakeRepository:
    def __init__(self, events: list[str], sessions: list[Session] | None = None):
        self.events = events
        self.sessions = list(sessions or [])
        self.updates: list[
            tuple[str, str | None, str | None, str | None]
        ] = []
        self.fail_updates = 0
        self.block_updates = False
        self.update_started = asyncio.Event()
        self.allow_update = asyncio.Event()

    async def get_all(self) -> list[Session]:
        self.events.append("mongo-inventory")
        return self.sessions

    async def update_runtime_ownership(
        self,
        session_id: str,
        sandbox_id: str | None,
        task_id: str | None,
        sandbox_provider: str | None = None,
        task_sandbox_id: str | None = None,
    ) -> None:
        self.events.append("session-save-start")
        self.update_started.set()
        if self.block_updates:
            await self.allow_update.wait()
        if self.fail_updates:
            self.fail_updates -= 1
            self.events.append("session-save-failed")
            raise ConnectionError("Mongo projection unavailable")
        self.updates.append(
            (session_id, sandbox_id, task_id, sandbox_provider)
        )
        self.events.append("session-save")


class FakeProvider:
    def __init__(
        self,
        provider_id: str,
        adapter: type["FakeAgentBay"],
        *,
        delete_success: bool = True,
        remove_on_delete: bool = True,
    ) -> None:
        self.session_id = provider_id
        self._adapter = adapter
        self.delete_success = delete_success
        self.remove_on_delete = remove_on_delete
        self.delete_calls = 0

    async def delete(self):
        self.delete_calls += 1
        self._adapter.events.append(f"delete:{self.session_id}")
        if self.delete_success and self.remove_on_delete:
            self._adapter.providers.pop(self.session_id, None)
        return SimpleNamespace(success=self.delete_success)


class FakeAgentBay:
    events: list[str] = []
    providers: dict[str, FakeProvider] = {}
    labels: dict[str, dict[str, str]] = {}
    allocate_count = 0
    allocate_id = "provider-new"
    connect_failures = 0
    lookup_results: dict[str, list[object]] = {}
    list_results: list[list[str] | Exception] = []

    @classmethod
    def reset(cls, events: list[str]) -> None:
        cls.events = events
        cls.providers = {}
        cls.labels = {}
        cls.allocate_count = 0
        cls.allocate_id = "provider-new"
        cls.connect_failures = 0
        cls.lookup_results = {}
        cls.list_results = []

    @classmethod
    def add_provider(
        cls,
        provider_id: str,
        labels: dict[str, str] | None = None,
        *,
        delete_success: bool = True,
        remove_on_delete: bool = True,
    ) -> FakeProvider:
        provider = FakeProvider(
            provider_id,
            cls,
            delete_success=delete_success,
            remove_on_delete=remove_on_delete,
        )
        cls.providers[provider_id] = provider
        cls.labels[provider_id] = dict(labels or {})
        return provider

    @classmethod
    async def allocate(cls, *, labels=None):
        cls.events.append("allocate")
        cls.allocate_count += 1
        return cls.add_provider(cls.allocate_id, dict(labels or {}))

    @classmethod
    async def connect(cls, provider):
        cls.events.append(f"connect:{provider.session_id}")
        if cls.connect_failures:
            cls.connect_failures -= 1
            raise SandboxUnavailableError("gateway links unavailable")
        return SimpleNamespace(id=provider.session_id)

    @classmethod
    async def lookup_provider_session(cls, provider_id: str):
        cls.events.append(f"lookup:{provider_id}")
        scripted = cls.lookup_results.get(provider_id)
        if scripted:
            value = scripted.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return cls.providers.get(provider_id)

    @classmethod
    async def list_provider_session_ids(cls, labels):
        cls.events.append(f"list:{dict(labels)}")
        if cls.list_results:
            value = cls.list_results.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        wanted = dict(labels)
        return [
            provider_id
            for provider_id, actual in cls.labels.items()
            if all(actual.get(key) == value for key, value in wanted.items())
        ]


class FakeLedger:
    def __init__(self, events: list[str]):
        self.events = events
        self.ensure_result = quota_result(AgentBayQuotaOutcome.LEDGER_EXISTS)
        self.begin_result = quota_result(
            AgentBayQuotaOutcome.RECONCILING,
            state=AgentBayBootstrapState.RECONCILING,
            revision=7,
        )
        self.reconcile_result = quota_result(
            AgentBayQuotaOutcome.RECONCILED,
            state=AgentBayBootstrapState.READY,
            revision=8,
        )
        self.current: AgentBayQuotaReservation | None = None
        self.ensure_error: Exception | None = None
        self.reserve_error: Exception | None = None
        self.release_error: Exception | None = None
        self.release_result = quota_result(AgentBayQuotaOutcome.RELEASED)
        self.mark_result: AgentBayQuotaResult | None = None
        self.replace_result: AgentBayQuotaResult | None = None
        self.cleanup_reservation: AgentBayQuotaReservation | None = None
        self.reserve_calls: list[tuple[str, str, str]] = []
        self.mark_calls: list[tuple[str, str, str, str]] = []
        self.replace_calls: list[tuple[str, str, str, str, str | None]] = []
        self.release_calls: list[tuple[str, str, str, str | None]] = []
        self.reconcile_calls: list[tuple[list[object], int]] = []
        self.block_mark = False
        self.mark_started = asyncio.Event()
        self.allow_mark = asyncio.Event()

    async def ensure_ledger(self):
        self.events.append("ledger-ensure")
        if self.ensure_error:
            raise self.ensure_error
        return self.ensure_result

    async def begin_reconciliation(self):
        self.events.append("ledger-begin")
        return self.begin_result

    async def reconcile_inventory(self, inventory, *, expected_revision):
        self.events.append("ledger-reconcile")
        self.reconcile_calls.append((list(inventory), expected_revision))
        return self.reconcile_result

    async def snapshot(self):
        self.events.append("ledger-snapshot")
        return AgentBayQuotaSnapshot(
            bootstrap_state=AgentBayBootstrapState.READY,
            revision=8,
            total=0,
            reservations=(),
        )

    async def reserve(self, session_id, user_id, operation_id):
        self.events.append("ledger-reserve")
        self.reserve_calls.append((session_id, user_id, operation_id))
        if self.reserve_error:
            raise self.reserve_error
        if self.current is None:
            self.current = reservation(operation_id)
            outcome = AgentBayQuotaOutcome.RESERVED
        else:
            outcome = (
                AgentBayQuotaOutcome.EXISTING_PROVISIONED
                if self.current.phase is AgentBayReservationPhase.PROVISIONED
                else AgentBayQuotaOutcome.EXISTING_RESERVED
            )
        return quota_result(outcome, reservation=self.current)

    async def mark_provisioned(
        self, session_id, user_id, operation_id, provider_id
    ):
        self.events.append("ledger-mark-start")
        self.mark_calls.append((session_id, user_id, operation_id, provider_id))
        self.mark_started.set()
        if self.block_mark:
            await self.allow_mark.wait()
        if self.mark_result is not None:
            return self.mark_result
        self.current = reservation(
            operation_id,
            phase=AgentBayReservationPhase.PROVISIONED,
            provider_id=provider_id,
        )
        self.events.append("ledger-mark")
        return quota_result(
            AgentBayQuotaOutcome.PROVISIONED, reservation=self.current
        )

    async def replace_operation(
        self,
        session_id,
        user_id,
        expected_operation_id,
        replacement_operation_id,
        *,
        expected_provider_id=None,
    ):
        self.events.append("ledger-replace")
        self.replace_calls.append(
            (
                session_id,
                user_id,
                expected_operation_id,
                replacement_operation_id,
                expected_provider_id,
            )
        )
        if self.replace_result is not None:
            return self.replace_result
        self.current = reservation(replacement_operation_id)
        return quota_result(AgentBayQuotaOutcome.REPLACED, reservation=self.current)

    async def get_reservation_for_cleanup(self, session_id, user_id):
        self.events.append("ledger-cleanup-get")
        return self.cleanup_reservation

    async def release(
        self, session_id, user_id, operation_id, *, provider_id=None
    ):
        self.events.append("ledger-release")
        self.release_calls.append((session_id, user_id, operation_id, provider_id))
        if self.release_error:
            raise self.release_error
        if self.release_result.outcome in {
            AgentBayQuotaOutcome.RELEASED,
            AgentBayQuotaOutcome.ALREADY_RELEASED,
        }:
            self.current = None
        return self.release_result


_DEFAULT_SANDBOX_PROVIDER = object()


def make_session(
    *,
    sandbox_id=None,
    task_id=None,
    sandbox_provider: str | None | object = _DEFAULT_SANDBOX_PROVIDER,
) -> Session:
    if sandbox_provider is _DEFAULT_SANDBOX_PROVIDER:
        sandbox_provider = "agentbay" if sandbox_id else None
    assert isinstance(sandbox_provider, str) or sandbox_provider is None
    return Session(
        id="private-session-id",
        user_id="private-user-id",
        agent_id="agent-1",
        sandbox_id=sandbox_id,
        sandbox_provider=sandbox_provider,
        task_id=task_id,
    )


def subject(
    events: list[str],
    *,
    ledger: FakeLedger | None = None,
    repository: FakeRepository | None = None,
):
    FakeAgentBay.reset(events)
    ledger = ledger or FakeLedger(events)
    repository = repository or FakeRepository(events)
    return (
        AgentBayProvisioner(
            ledger=ledger,
            session_repository=repository,
            deployment_id="production-cluster-a",
            sandbox_cls=FakeAgentBay,
        ),
        ledger,
        repository,
    )


def labels_for(provisioner: AgentBayProvisioner, operation_id: str) -> dict[str, str]:
    return provisioner._labels(
        "private-session-id", "private-user-id", operation_id
    )


def test_deployment_id_is_required():
    with pytest.raises(ValueError, match="deployment_id"):
        AgentBayProvisioner(
            ledger=object(),
            session_repository=object(),
            deployment_id=" ",
            sandbox_cls=FakeAgentBay,
        )


@pytest.mark.parametrize("revision", [None, True, -1])
async def test_bootstrap_invalid_revision_fails_closed(revision):
    events: list[str] = []
    provisioner, ledger, _ = subject(events)
    ledger.ensure_result = quota_result(
        AgentBayQuotaOutcome.LEDGER_CREATED,
        state=AgentBayBootstrapState.REQUIRED,
        revision=0,
    )
    ledger.begin_result = quota_result(
        AgentBayQuotaOutcome.RECONCILING,
        state=AgentBayBootstrapState.RECONCILING,
        revision=revision,
    )

    with pytest.raises(AgentBayQuotaInconsistentError, match="valid revision"):
        await provisioner.ensure_locked(make_session())

    assert FakeAgentBay.allocate_count == 0
    assert ledger.reserve_calls == []
    assert ledger.reconcile_calls == []


async def test_agentbay_rejects_docker_owned_session_before_ledger_or_provider_calls():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    session = make_session(
        sandbox_id="docker-sandbox",
        sandbox_provider="docker",
    )

    with pytest.raises(AgentBayQuotaInconsistentError, match="different provider"):
        await provisioner.ensure_locked(session)

    assert events == []
    assert ledger.reserve_calls == []
    assert FakeAgentBay.allocate_count == 0
    assert repository.updates == []


async def test_agentbay_adopts_legacy_id_only_from_exact_provisioned_ledger_owner():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    owned = reservation(
        "legacy-operation",
        phase=AgentBayReservationPhase.PROVISIONED,
        provider_id="legacy-provider",
    )
    ledger.cleanup_reservation = owned
    ledger.current = owned
    provider = FakeAgentBay.add_provider("legacy-provider")
    session = make_session(
        sandbox_id="legacy-provider",
        sandbox_provider=None,
    )

    sandbox = await provisioner.ensure_locked(session)

    assert sandbox.id == provider.session_id
    assert session.sandbox_provider == "agentbay"
    assert repository.updates == [
        ("private-session-id", "legacy-provider", None, "agentbay")
    ]
    assert events.index("ledger-cleanup-get") < events.index("session-save")
    assert events.index("session-save") < events.index("ledger-reserve")
    assert FakeAgentBay.allocate_count == 0


@pytest.mark.parametrize(
    "cleanup_reservation",
    [
        None,
        reservation(
            "wrong-provider-operation",
            phase=AgentBayReservationPhase.PROVISIONED,
            provider_id="different-provider",
        ),
        reservation("still-reserved-operation"),
    ],
)
async def test_agentbay_legacy_id_fails_closed_without_exact_ledger_owner(
    cleanup_reservation,
):
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    ledger.cleanup_reservation = cleanup_reservation
    session = make_session(
        sandbox_id="legacy-unknown",
        sandbox_provider=None,
    )

    with pytest.raises(AgentBayQuotaInconsistentError, match="does not exactly match"):
        await provisioner.ensure_locked(session)

    assert events == ["ledger-ensure", "ledger-cleanup-get"]
    assert ledger.reserve_calls == []
    assert FakeAgentBay.allocate_count == 0
    assert repository.updates == []
    assert session.sandbox_provider is None


async def test_agentbay_never_deletes_docker_owned_session():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    session = make_session(
        sandbox_id="not-agentbay",
        sandbox_provider="docker",
    )

    with pytest.raises(AgentBayQuotaInconsistentError, match="different provider"):
        await provisioner.destroy_locked(session)

    assert events == []
    assert ledger.release_calls == []
    assert repository.updates == []


async def test_agentbay_never_deletes_legacy_id_without_exact_ledger_owner():
    for cleanup_reservation in [
        None,
        reservation(
            "wrong-provider-operation",
            phase=AgentBayReservationPhase.PROVISIONED,
            provider_id="different-provider",
        ),
    ]:
        events: list[str] = []
        provisioner, ledger, repository = subject(events)
        ledger.cleanup_reservation = cleanup_reservation
        session = make_session(
            sandbox_id="not-confirmed-agentbay",
            sandbox_provider=None,
        )

        with pytest.raises(
            AgentBayQuotaInconsistentError,
            match="does not exactly match",
        ):
            await provisioner.destroy_locked(session)

        assert events == ["ledger-cleanup-get"]
        assert ledger.release_calls == []
        assert repository.updates == []


async def test_agentbay_adopts_exact_legacy_id_before_deleting():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    ledger.cleanup_reservation = reservation(
        "legacy-delete-operation",
        phase=AgentBayReservationPhase.PROVISIONED,
        provider_id="legacy-provider",
    )
    provider = FakeAgentBay.add_provider("legacy-provider")
    session = make_session(
        sandbox_id="legacy-provider",
        sandbox_provider=None,
        task_id="task-1",
    )

    await provisioner.destroy_locked(session)

    assert provider.delete_calls == 1
    assert session.sandbox_provider is None
    assert repository.updates == [
        ("private-session-id", "legacy-provider", "task-1", "agentbay"),
        ("private-session-id", None, None, "agentbay"),
        ("private-session-id", None, None, None),
    ]


async def test_bootstrap_scans_all_provider_sessions_including_legacy_labels():
    events: list[str] = []
    provisioner, ledger, _ = subject(events)
    ledger.ensure_result = quota_result(
        AgentBayQuotaOutcome.LEDGER_CREATED,
        state=AgentBayBootstrapState.REQUIRED,
        revision=0,
    )
    # This legacy billable session has no new deployment label.
    FakeAgentBay.add_provider("legacy-billable", {"app": "ai-manus"})

    with pytest.raises(AgentBayQuotaBootstrapError) as exc:
        await provisioner.ensure_locked(make_session())

    assert exc.value.state is AgentBayBootstrapState.RECONCILING
    assert "list:{}" in events
    assert ledger.reconcile_calls == []
    assert ledger.reserve_calls == []
    assert FakeAgentBay.allocate_count == 0


async def test_bootstrap_refuses_mongo_ownership_even_when_provider_list_is_empty():
    events: list[str] = []
    old = make_session(sandbox_id="legacy-provider")
    repository = FakeRepository(events, [old])
    provisioner, ledger, _ = subject(events, repository=repository)
    ledger.ensure_result = quota_result(
        AgentBayQuotaOutcome.LEDGER_CREATED,
        state=AgentBayBootstrapState.REQUIRED,
        revision=0,
    )

    with pytest.raises(AgentBayQuotaBootstrapError):
        await provisioner.ensure_locked(make_session())

    assert "list:{}" in events
    assert ledger.reconcile_calls == []
    assert FakeAgentBay.allocate_count == 0


async def test_authoritatively_empty_bootstrap_reconciles_before_reserve():
    events: list[str] = []
    provisioner, ledger, _ = subject(events)
    ledger.ensure_result = quota_result(
        AgentBayQuotaOutcome.LEDGER_CREATED,
        state=AgentBayBootstrapState.REQUIRED,
        revision=0,
    )

    sandbox = await provisioner.ensure_locked(make_session())

    assert sandbox.id == "provider-new"
    assert ledger.reconcile_calls == [([], 7)]
    assert events.index("ledger-reconcile") < events.index("ledger-reserve")
    assert events.index("ledger-reserve") < events.index("allocate")


@pytest.mark.parametrize(
    "failure",
    [
        AgentBayQuotaUnavailableError("Mongo unavailable"),
        AgentBayQuotaExceededError(AgentBayQuotaScope.GLOBAL),
        AgentBayQuotaExceededError(AgentBayQuotaScope.USER),
    ],
)
async def test_ledger_or_quota_failure_allocates_nothing(failure):
    events: list[str] = []
    provisioner, ledger, _ = subject(events)
    ledger.reserve_error = failure

    with pytest.raises(type(failure)):
        await provisioner.ensure_locked(make_session())

    assert FakeAgentBay.allocate_count == 0
    assert not any(event.startswith("list:") for event in events)


async def test_bootstrap_mongo_failure_allocates_nothing():
    events: list[str] = []
    provisioner, ledger, _ = subject(events)
    ledger.ensure_error = AgentBayQuotaUnavailableError("Mongo unavailable")

    with pytest.raises(AgentBayQuotaUnavailableError):
        await provisioner.ensure_locked(make_session())

    assert ledger.reserve_calls == []
    assert FakeAgentBay.allocate_count == 0
    assert not any(event.startswith("list:") for event in events)


async def test_retryable_reservation_never_reaches_provider_inventory():
    events: list[str] = []
    provisioner, ledger, _ = subject(events)

    async def retryable_reserve(*_args):
        return quota_result(
            AgentBayQuotaOutcome.RETRYABLE,
            reservation=reservation("ambiguous-operation"),
        )

    ledger.reserve = retryable_reserve

    with pytest.raises(AgentBayQuotaInconsistentError, match="not confirmed"):
        await provisioner.ensure_locked(make_session())

    assert FakeAgentBay.allocate_count == 0
    assert not any(event.startswith("list:") for event in events)


async def test_fresh_reservation_orders_ledger_session_and_connect_and_hashes_labels():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    session = make_session()

    sandbox = await provisioner.ensure_locked(session)

    operation_id = ledger.current.operation_id
    expected_labels = labels_for(provisioner, operation_id)
    assert FakeAgentBay.labels["provider-new"] == expected_labels
    assert expected_labels["manus_session"] == hashlib.sha256(
        b"ai-manus-agentbay-session-v1\0private-session-id"
    ).hexdigest()
    assert expected_labels["manus_user"] == hashlib.sha256(
        b"ai-manus-agentbay-user-v1\0private-user-id"
    ).hexdigest()
    assert "private-session-id" not in repr(expected_labels)
    assert "private-user-id" not in repr(expected_labels)
    assert sandbox.id == "provider-new"
    assert session.sandbox_id == "provider-new"
    assert repository.updates == [
        ("private-session-id", "provider-new", None, "agentbay")
    ]
    assert events.index("ledger-reserve") < events.index("allocate")
    assert events.index("allocate") < events.index("ledger-mark-start")
    assert events.index("ledger-mark") < events.index("session-save")
    assert events.index("session-save") < events.index("connect:provider-new")


async def test_existing_reserved_labeled_provider_is_reused_without_allocation():
    events: list[str] = []
    provisioner, ledger, _ = subject(events)
    ledger.current = reservation("recoverable-operation")
    provider = FakeAgentBay.add_provider(
        "provider-recovered", labels_for(provisioner, "recoverable-operation")
    )

    sandbox = await provisioner.ensure_locked(make_session())

    assert sandbox.id == provider.session_id
    assert FakeAgentBay.allocate_count == 0
    assert ledger.mark_calls[-1][-1] == "provider-recovered"


async def test_existing_reserved_empty_inventory_never_allocates_again():
    events: list[str] = []
    provisioner, ledger, _ = subject(events)
    ledger.current = reservation("ambiguous-operation")

    with pytest.raises(AgentBayQuotaInconsistentError, match="outcome is unknown"):
        await provisioner.ensure_locked(make_session())

    assert FakeAgentBay.allocate_count == 0
    assert ledger.mark_calls == []


async def test_duplicate_labeled_providers_fail_closed_before_allocation():
    events: list[str] = []
    provisioner, ledger, _ = subject(events)
    ledger.current = reservation("duplicate-operation")
    labels = labels_for(provisioner, "duplicate-operation")
    FakeAgentBay.add_provider("provider-a", labels)
    FakeAgentBay.add_provider("provider-b", labels)

    with pytest.raises(AgentBayQuotaInconsistentError, match="Multiple"):
        await provisioner.ensure_locked(make_session())

    assert FakeAgentBay.allocate_count == 0
    assert ledger.mark_calls == []


async def test_duplicate_discovered_after_allocate_is_not_marked_owned():
    events: list[str] = []
    provisioner, ledger, _ = subject(events)
    other = FakeAgentBay.add_provider("provider-other", {})
    # First inventory is empty; the post-create inventory races in another ID.
    FakeAgentBay.list_results = [[], [other.session_id]]

    with pytest.raises(AgentBayQuotaInconsistentError, match="allocated"):
        await provisioner.ensure_locked(make_session())

    assert FakeAgentBay.allocate_count == 1
    assert ledger.mark_calls == []


async def test_mark_success_without_exact_reservation_never_writes_session_pointer():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    ledger.mark_result = quota_result(
        AgentBayQuotaOutcome.PROVISIONED, reservation=None
    )

    with pytest.raises(AgentBayQuotaInconsistentError, match="no reservation"):
        await provisioner.ensure_locked(make_session())

    assert FakeAgentBay.allocate_count == 1
    assert repository.updates == []
    assert "connect:provider-new" not in events


async def test_link_failure_leaves_both_durable_pointers_and_retry_reuses_provider():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    FakeAgentBay.connect_failures = 1
    first_session = make_session()

    with pytest.raises(SandboxUnavailableError, match="gateway"):
        await provisioner.ensure_locked(first_session)

    assert ledger.current.phase is AgentBayReservationPhase.PROVISIONED
    assert ledger.current.provider_id == "provider-new"
    assert repository.updates[-1][1] == "provider-new"
    assert repository.updates[-1][3] == "agentbay"

    reloaded = make_session(sandbox_id="provider-new")
    sandbox = await provisioner.ensure_locked(reloaded)

    assert sandbox.id == "provider-new"
    assert FakeAgentBay.allocate_count == 1


async def test_exact_missing_provider_replaces_operation_before_allocating():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    ledger.current = reservation(
        "old-operation",
        phase=AgentBayReservationPhase.PROVISIONED,
        provider_id="provider-gone",
    )
    session = make_session(sandbox_id="provider-gone")

    sandbox = await provisioner.ensure_locked(session)

    assert sandbox.id == "provider-new"
    assert len(ledger.replace_calls) == 1
    assert ledger.replace_calls[0][2] == "old-operation"
    assert ledger.replace_calls[0][4] == "provider-gone"
    assert repository.updates[0][1] is None
    assert repository.updates[0][3] == "agentbay"
    assert repository.updates[-1][1] == "provider-new"
    assert repository.updates[-1][3] == "agentbay"
    assert events.index("lookup:provider-gone") < events.index("ledger-replace")
    assert events.index("ledger-replace") < events.index("allocate")


async def test_inconclusive_lookup_never_replaces_or_allocates():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    ledger.current = reservation(
        "old-operation",
        phase=AgentBayReservationPhase.PROVISIONED,
        provider_id="provider-unknown",
    )
    FakeAgentBay.lookup_results["provider-unknown"] = [
        SandboxUnavailableError("lookup inconclusive")
    ]
    session = make_session(sandbox_id="provider-unknown")

    with pytest.raises(SandboxUnavailableError, match="inconclusive"):
        await provisioner.ensure_locked(session)

    assert ledger.replace_calls == []
    assert FakeAgentBay.allocate_count == 0
    assert repository.updates == []
    assert session.sandbox_id == "provider-unknown"


async def test_lookup_provider_id_mismatch_fails_closed():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    ledger.current = reservation(
        "existing-operation",
        phase=AgentBayReservationPhase.PROVISIONED,
        provider_id="provider-expected",
    )
    wrong = FakeProvider("provider-different", FakeAgentBay)
    FakeAgentBay.lookup_results["provider-expected"] = [wrong]

    with pytest.raises(AgentBayQuotaInconsistentError, match="mismatched"):
        await provisioner.ensure_locked(
            make_session(sandbox_id="provider-expected")
        )

    assert ledger.replace_calls == []
    assert FakeAgentBay.allocate_count == 0
    assert repository.updates == []


async def test_session_save_failure_recovers_from_ledger_without_second_allocation():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    repository.fail_updates = 1

    with pytest.raises(ConnectionError, match="projection"):
        await provisioner.ensure_locked(make_session())

    assert ledger.current.phase is AgentBayReservationPhase.PROVISIONED
    assert ledger.current.provider_id == "provider-new"
    assert FakeAgentBay.allocate_count == 1

    reloaded = make_session()
    sandbox = await provisioner.ensure_locked(reloaded)

    assert sandbox.id == "provider-new"
    assert reloaded.sandbox_id == "provider-new"
    assert FakeAgentBay.allocate_count == 1
    assert repository.updates[-1][1] == "provider-new"
    assert repository.updates[-1][3] == "agentbay"


async def test_repeated_cancellation_waits_for_session_projection_before_propagating():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    repository.block_updates = True
    session = make_session()
    task = asyncio.create_task(provisioner.ensure_locked(session))

    await asyncio.wait_for(repository.update_started.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    repository.allow_update.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert ledger.current.phase is AgentBayReservationPhase.PROVISIONED
    assert repository.updates == [
        ("private-session-id", "provider-new", None, "agentbay")
    ]
    assert "connect:provider-new" not in events


async def test_repository_child_cancellation_propagates_without_spinning():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)

    async def cancelled_update(*_args):
        raise asyncio.CancelledError

    repository.update_runtime_ownership = cancelled_update

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            provisioner.ensure_locked(make_session()), timeout=1
        )

    assert ledger.current.phase is AgentBayReservationPhase.PROVISIONED
    assert FakeAgentBay.allocate_count == 1
    assert "connect:provider-new" not in events


async def test_cancellation_before_ledger_mark_recovers_by_operation_label():
    events: list[str] = []
    provisioner, ledger, _ = subject(events)
    ledger.block_mark = True
    task = asyncio.create_task(provisioner.ensure_locked(make_session()))

    await asyncio.wait_for(ledger.mark_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ledger.current.phase is AgentBayReservationPhase.RESERVED
    assert FakeAgentBay.allocate_count == 1
    ledger.block_mark = False

    sandbox = await provisioner.ensure_locked(make_session())

    assert sandbox.id == "provider-new"
    assert FakeAgentBay.allocate_count == 1


async def test_destroy_success_probes_exact_absence_then_clears_then_releases():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    owned = reservation(
        "delete-operation",
        phase=AgentBayReservationPhase.PROVISIONED,
        provider_id="provider-delete",
    )
    ledger.cleanup_reservation = owned
    provider = FakeAgentBay.add_provider("provider-delete")
    session = make_session(sandbox_id="provider-delete", task_id="task-1")

    await provisioner.destroy_locked(session)

    assert provider.delete_calls == 1
    assert session.sandbox_id is None
    assert session.task_id is None
    assert session.sandbox_provider is None
    assert repository.updates == [
        ("private-session-id", None, None, "agentbay"),
        ("private-session-id", None, None, None),
    ]
    assert ledger.release_calls == [
        (
            "private-session-id",
            "private-user-id",
            "delete-operation",
            "provider-delete",
        )
    ]
    assert events.index("delete:provider-delete") < events.index(
        "session-save"
    )
    # The second lookup is the independent exact post-delete probe.
    lookups = [event for event in events if event == "lookup:provider-delete"]
    assert len(lookups) == 2
    assert events.index("session-save") < events.index("ledger-release")


async def test_destroy_unconfirmed_delete_retains_all_recovery_state():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    ledger.cleanup_reservation = reservation(
        "delete-operation",
        phase=AgentBayReservationPhase.PROVISIONED,
        provider_id="provider-delete",
    )
    FakeAgentBay.add_provider("provider-delete", delete_success=False)
    session = make_session(sandbox_id="provider-delete", task_id="task-1")

    with pytest.raises(SandboxUnavailableError, match="did not confirm"):
        await provisioner.destroy_locked(session)

    assert session.sandbox_id == "provider-delete"
    assert session.task_id == "task-1"
    assert repository.updates == []
    assert ledger.release_calls == []


async def test_destroy_success_message_is_not_enough_when_exact_probe_is_live():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    ledger.cleanup_reservation = reservation(
        "delete-operation",
        phase=AgentBayReservationPhase.PROVISIONED,
        provider_id="provider-delete",
    )
    provider = FakeAgentBay.add_provider(
        "provider-delete", remove_on_delete=False
    )
    session = make_session(sandbox_id="provider-delete", task_id="task-1")

    with pytest.raises(SandboxUnavailableError, match="still exists"):
        await provisioner.destroy_locked(session)

    assert provider.delete_calls == 1
    assert session.sandbox_id == "provider-delete"
    assert repository.updates == []
    assert ledger.release_calls == []


async def test_destroy_inconclusive_post_delete_probe_retains_recovery_state():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    ledger.cleanup_reservation = reservation(
        "delete-operation",
        phase=AgentBayReservationPhase.PROVISIONED,
        provider_id="provider-delete",
    )
    provider = FakeAgentBay.add_provider(
        "provider-delete", remove_on_delete=False
    )
    FakeAgentBay.lookup_results["provider-delete"] = [
        provider,
        SandboxUnavailableError("post-delete lookup inconclusive"),
    ]
    session = make_session(sandbox_id="provider-delete", task_id="task-1")

    with pytest.raises(SandboxUnavailableError, match="inconclusive"):
        await provisioner.destroy_locked(session)

    assert session.sandbox_id == "provider-delete"
    assert repository.updates == []
    assert ledger.release_calls == []


async def test_destroy_stale_release_is_detected_after_safe_pointer_clear():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    ledger.cleanup_reservation = reservation(
        "stale-operation",
        phase=AgentBayReservationPhase.PROVISIONED,
        provider_id="provider-already-gone",
    )
    ledger.release_result = quota_result(AgentBayQuotaOutcome.STALE_OPERATION)
    session = make_session(
        sandbox_id="provider-already-gone", task_id="task-1"
    )

    with pytest.raises(AgentBayQuotaInconsistentError, match="exact operation"):
        await provisioner.destroy_locked(session)

    assert session.sandbox_provider == "agentbay"
    assert repository.updates == [
        ("private-session-id", None, None, "agentbay")
    ]
    assert ledger.release_calls[-1][2:] == (
        "stale-operation",
        "provider-already-gone",
    )
    assert events.index("session-save") < events.index("ledger-release")


async def test_destroy_release_failure_retains_agentbay_cleanup_marker():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    ledger.cleanup_reservation = reservation(
        "release-failure-operation",
        phase=AgentBayReservationPhase.PROVISIONED,
        provider_id="provider-already-gone",
    )
    ledger.release_error = AgentBayQuotaUnavailableError("ledger unavailable")
    session = make_session(
        sandbox_id="provider-already-gone",
        task_id="task-1",
    )

    with pytest.raises(AgentBayQuotaUnavailableError, match="unavailable"):
        await provisioner.destroy_locked(session)

    assert session.sandbox_id is None
    assert session.task_id is None
    assert session.sandbox_provider == "agentbay"
    assert repository.updates == [
        ("private-session-id", None, None, "agentbay")
    ]
    assert ledger.release_calls == [
        (
            "private-session-id",
            "private-user-id",
            "release-failure-operation",
            "provider-already-gone",
        )
    ]


async def test_destroy_reserved_operation_recovers_labeled_provider_before_delete():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    owned = reservation("reserved-delete")
    ledger.cleanup_reservation = owned
    provider = FakeAgentBay.add_provider(
        "provider-reserved", labels_for(provisioner, "reserved-delete")
    )
    session = make_session(task_id="task-1")

    await provisioner.destroy_locked(session)

    assert provider.delete_calls == 1
    assert ledger.mark_calls[-1][-1] == "provider-reserved"
    assert ledger.release_calls[-1][-1] == "provider-reserved"
    assert repository.updates[-2:] == [
        ("private-session-id", None, None, "agentbay"),
        ("private-session-id", None, None, None),
    ]


async def test_reserved_cleanup_survives_deployment_config_mismatch():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    owned = reservation("reserved-config-drift")
    ledger.cleanup_reservation = owned
    old_deployment_labels = labels_for(
        provisioner, "reserved-config-drift"
    )
    old_deployment_labels["manus_deployment"] = "old-deployment-hash"
    provider = FakeAgentBay.add_provider(
        "provider-config-drift",
        old_deployment_labels,
    )

    async def mismatched_mark(*_args):
        raise AgentBayQuotaConfigurationError("rolling deployment mismatch")

    ledger.mark_provisioned = mismatched_mark
    session = make_session(
        sandbox_id="provider-config-drift", task_id="task-1"
    )

    await provisioner.destroy_locked(session)

    assert provider.delete_calls == 1
    # The ledger remains reserved, so exact release must not invent a provider
    # handle merely because label recovery found one.
    assert ledger.release_calls[-1] == (
        "private-session-id",
        "private-user-id",
        "reserved-config-drift",
        None,
    )
    assert repository.updates[-2:] == [
        ("private-session-id", None, None, "agentbay"),
        ("private-session-id", None, None, None),
    ]


async def test_destroy_empty_reserved_operation_retains_ambiguous_ownership():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    ledger.cleanup_reservation = reservation("empty-reserved")
    session = make_session(task_id="task-1")

    with pytest.raises(AgentBayQuotaInconsistentError, match="reconciliation"):
        await provisioner.destroy_locked(session)

    assert ledger.mark_calls == []
    assert ledger.release_calls == []
    assert repository.updates == []
    assert session.task_id == "task-1"


async def test_destroy_missing_ledger_with_session_pointer_fails_closed():
    events: list[str] = []
    provisioner, ledger, repository = subject(events)
    ledger.cleanup_reservation = None
    session = make_session(sandbox_id="untracked-provider", task_id="task-1")

    with pytest.raises(AgentBayQuotaInconsistentError, match="missing"):
        await provisioner.destroy_locked(session)

    assert session.sandbox_id == "untracked-provider"
    assert repository.updates == []
    assert ledger.release_calls == []
