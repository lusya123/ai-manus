from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.external.agentbay_quota import (
    AgentBayBootstrapState,
    AgentBayQuotaOutcome,
    AgentBayQuotaReservation,
    AgentBayQuotaResult,
    AgentBayQuotaSnapshot,
    AgentBayReservationPhase,
)
from scripts.reconcile_agentbay_quota import (
    AgentBayReconciliationError,
    _snapshot_matches,
    reconcile,
    scan_inventory,
)
from app.infrastructure.repositories.external.sandbox.mongo_agentbay_quota import (
    MongoAgentBayQuotaLedger,
)


class FakeSandbox:
    live: dict[str, object] = {}
    listed: list[str] = []
    failures: dict[str, Exception] = {}
    list_calls = 0
    lookup_calls: list[str] = []

    @classmethod
    def reset(cls, provider_ids=()):
        cls.live = {
            provider_id: SimpleNamespace(session_id=provider_id)
            for provider_id in provider_ids
        }
        cls.listed = list(provider_ids)
        cls.failures = {}
        cls.list_calls = 0
        cls.lookup_calls = []

    @classmethod
    async def list_provider_session_ids(cls, labels):
        assert labels == {}
        cls.list_calls += 1
        return list(cls.listed)

    @classmethod
    async def lookup_provider_session(cls, provider_id):
        cls.lookup_calls.append(provider_id)
        if provider_id in cls.failures:
            raise cls.failures[provider_id]
        return cls.live.get(provider_id)


class FakeLedger:
    def __init__(self, *, state=AgentBayBootstrapState.REQUIRED, revision=7):
        self.state = state
        self.revision = revision
        self.calls: list[str] = []
        self.inventory = None
        self.snapshot_value = AgentBayQuotaSnapshot(
            bootstrap_state=state,
            revision=revision if isinstance(revision, int) else 0,
            total=0,
            reservations=(),
        )

    async def ensure_ledger(self):
        self.calls.append("ensure")
        return AgentBayQuotaResult(
            outcome=AgentBayQuotaOutcome.LEDGER_EXISTS,
            bootstrap_state=self.state,
            revision=self.revision,
        )

    async def snapshot(self):
        self.calls.append("snapshot")
        return self.snapshot_value

    async def begin_reconciliation(self):
        self.calls.append("begin")
        return AgentBayQuotaResult(
            outcome=AgentBayQuotaOutcome.RECONCILING,
            bootstrap_state=AgentBayBootstrapState.RECONCILING,
            revision=self.revision,
        )

    async def reconcile_inventory(self, inventory, *, expected_revision):
        self.calls.append(f"reconcile:{expected_revision}")
        self.inventory = tuple(inventory)
        return AgentBayQuotaResult(
            outcome=AgentBayQuotaOutcome.RECONCILED,
            bootstrap_state=AgentBayBootstrapState.READY,
            revision=expected_revision + 1,
        )


def record(session="session-1", user="user-1", provider="provider-1"):
    return {
        "session_id": session,
        "user_id": user,
        "sandbox_id": provider,
    }


async def test_dry_run_cross_checks_all_inventory_without_touching_ledger():
    FakeSandbox.reset(["provider-1"])
    ledger = FakeLedger()

    async def load():
        return [record()]

    audit = await reconcile(
        apply=False,
        replace_ready_ledger=False,
        ledger=ledger,
        load_sessions=load,
        deployment_id="deployment-a",
        sandbox_cls=FakeSandbox,
    )

    assert audit.mongo_owned == audit.provider_live == 1
    assert ledger.calls == []
    assert audit.entries[0].operation_id.startswith("legacy-")
    assert "session-1" not in audit.entries[0].operation_id
    assert "provider-1" not in audit.entries[0].operation_id


@pytest.mark.parametrize(
    ("records", "providers", "message"),
    [
        ([], ["orphan"], "no Mongo owner"),
        ([record(provider="missing")], [], "exact-missing"),
        (
            [record(session="one"), record(session="two")],
            ["provider-1"],
            "multiple Mongo sessions",
        ),
    ],
)
async def test_divergent_inventory_fails_before_any_ledger_write(
    records, providers, message
):
    FakeSandbox.reset(providers)
    ledger = FakeLedger()

    with pytest.raises(AgentBayReconciliationError, match=message):
        await reconcile(
            apply=True,
            replace_ready_ledger=False,
            ledger=ledger,
            load_sessions=lambda: _value(records),
            deployment_id="deployment-a",
            sandbox_cls=FakeSandbox,
        )

    assert ledger.calls == []


async def test_quota_overflow_fails_before_provider_or_ledger_calls():
    FakeSandbox.reset(["provider-1", "provider-2"])
    ledger = FakeLedger()
    records = [
        record(session="one", provider="provider-1"),
        record(session="two", provider="provider-2"),
    ]

    with pytest.raises(AgentBayReconciliationError, match="per-user cap"):
        await reconcile(
            apply=True,
            replace_ready_ledger=False,
            ledger=ledger,
            load_sessions=lambda: _value(records),
            deployment_id="deployment-a",
            sandbox_cls=FakeSandbox,
            max_total=2,
            max_per_user=1,
        )

    assert FakeSandbox.list_calls == 0
    assert ledger.calls == []


async def test_apply_gates_rescans_then_atomically_reconciles():
    FakeSandbox.reset(["provider-1"])
    ledger = FakeLedger(revision=11)
    load_count = 0

    async def load():
        nonlocal load_count
        load_count += 1
        return [record()]

    audit = await reconcile(
        apply=True,
        replace_ready_ledger=False,
        ledger=ledger,
        load_sessions=load,
        deployment_id="deployment-a",
        sandbox_cls=FakeSandbox,
    )

    assert load_count == 2
    assert FakeSandbox.list_calls == 2
    assert ledger.calls == ["ensure", "begin", "reconcile:11"]
    assert ledger.inventory == audit.entries


@pytest.mark.parametrize("revision", [None, True, -1])
async def test_invalid_gate_revision_never_writes_inventory(revision):
    FakeSandbox.reset([])
    ledger = FakeLedger(revision=revision)

    with pytest.raises(RuntimeError, match="valid revision"):
        await reconcile(
            apply=True,
            replace_ready_ledger=False,
            ledger=ledger,
            load_sessions=lambda: _value([]),
            deployment_id="deployment-a",
            sandbox_cls=FakeSandbox,
        )

    assert ledger.inventory is None


async def test_inventory_change_after_gate_stays_reconciling_without_replace():
    FakeSandbox.reset(["provider-1"])
    ledger = FakeLedger()
    load_count = 0

    async def load():
        nonlocal load_count
        load_count += 1
        if load_count == 2:
            FakeSandbox.reset([])
            return []
        return [record()]

    with pytest.raises(AgentBayReconciliationError, match="changed"):
        await reconcile(
            apply=True,
            replace_ready_ledger=False,
            ledger=ledger,
            load_sessions=load,
            deployment_id="deployment-a",
            sandbox_cls=FakeSandbox,
        )

    assert ledger.calls == ["ensure", "begin"]
    assert ledger.inventory is None


async def test_ready_matching_ledger_is_an_idempotent_noop():
    FakeSandbox.reset(["provider-1"])
    audit = await scan_inventory(
        [record()], deployment_id="deployment-a", sandbox_cls=FakeSandbox
    )
    entry = audit.entries[0]
    reservation = AgentBayQuotaReservation(
        session_key=MongoAgentBayQuotaLedger.session_key(entry.session_id),
        user_key=MongoAgentBayQuotaLedger.user_key(entry.user_id),
        operation_key=MongoAgentBayQuotaLedger.operation_key(entry.operation_id),
        operation_id=entry.operation_id,
        phase=AgentBayReservationPhase.PROVISIONED,
        provider_id=entry.provider_id,
        provider_key=MongoAgentBayQuotaLedger.provider_key(entry.provider_id),
    )
    ledger = FakeLedger(state=AgentBayBootstrapState.READY)
    ledger.snapshot_value = AgentBayQuotaSnapshot(
        bootstrap_state=AgentBayBootstrapState.READY,
        revision=9,
        total=1,
        reservations=(reservation,),
    )

    result = await reconcile(
        apply=True,
        replace_ready_ledger=False,
        ledger=ledger,
        load_sessions=lambda: _value([record()]),
        deployment_id="deployment-a",
        sandbox_cls=FakeSandbox,
    )

    assert _snapshot_matches(ledger.snapshot_value, result.entries)
    assert ledger.calls == ["ensure", "snapshot"]


async def test_ready_different_ledger_requires_second_explicit_flag():
    FakeSandbox.reset(["provider-1"])
    ledger = FakeLedger(state=AgentBayBootstrapState.READY)

    with pytest.raises(AgentBayReconciliationError, match="replace-ready-ledger"):
        await reconcile(
            apply=True,
            replace_ready_ledger=False,
            ledger=ledger,
            load_sessions=lambda: _value([record()]),
            deployment_id="deployment-a",
            sandbox_cls=FakeSandbox,
        )

    assert ledger.calls == ["ensure", "snapshot"]


async def _value(value):
    return value
