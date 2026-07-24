"""Audit and reconcile legacy AgentBay ownership into the Mongo cost ledger.

The default is a read-only audit.  ``--apply`` closes the provisioning gate,
rescans both Mongo and the complete AgentBay account, then atomically replaces
the bounded ledger inventory.  It never deletes a provider session or clears a
Mongo pointer.

Run from ``backend/``::

    uv run python scripts/reconcile_agentbay_quota.py
    uv run python scripts/reconcile_agentbay_quota.py --apply

If an already-ready ledger differs from provider/Mongo truth, the tool refuses
to replace it unless ``--replace-ready-ledger`` is also supplied.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference

from app.core.config import Settings, get_settings
from app.domain.external.agentbay_quota import (
    AgentBayBootstrapState,
    AgentBayQuotaInconsistentError,
    AgentBayQuotaInventoryEntry,
    AgentBayQuotaLedger,
    AgentBayQuotaSnapshot,
)
from app.infrastructure.external.sandbox.agentbay_sandbox import AgentBaySandbox
from app.infrastructure.repositories.external.sandbox.mongo_agentbay_quota import (
    MongoAgentBayQuotaLedger,
)
from app.infrastructure.storage.mongodb import get_mongodb


class AgentBayReconciliationError(RuntimeError):
    """Inventory is incomplete, divergent, or unsafe to apply."""


@dataclass(frozen=True, slots=True)
class InventoryAudit:
    entries: tuple[AgentBayQuotaInventoryEntry, ...]
    mongo_owned: int
    provider_live: int


def _identifier(value: Any, name: str, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
    ):
        raise AgentBayReconciliationError(f"Invalid bounded {name}")
    return value


def _fingerprint(value: str) -> str:
    """Short one-way identifier suitable for an operator-facing report."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _operation_id(
    deployment_id: str,
    session_id: str,
    user_id: str,
    provider_id: str,
) -> str:
    digest = hashlib.sha256(
        (
            "ai-manus-agentbay-legacy-operation-v1\0"
            f"{deployment_id}\0{session_id}\0{user_id}\0{provider_id}"
        ).encode("utf-8")
    ).hexdigest()
    return f"legacy-{digest}"


async def scan_inventory(
    records: Sequence[Mapping[str, Any]],
    *,
    deployment_id: str,
    sandbox_cls: type[AgentBaySandbox] = AgentBaySandbox,
    max_total: int = 20,
    max_per_user: int = 20,
) -> InventoryAudit:
    """Cross-check every Mongo owner against the entire provider account."""
    deployment_id = _identifier(deployment_id, "deployment_id", 512)
    mongo_by_provider: dict[str, tuple[str, str]] = {}
    mongo_session_ids: set[str] = set()
    user_counts: Counter[str] = Counter()
    for record in records:
        provider_value = record.get("sandbox_id")
        if provider_value in (None, ""):
            continue
        session_id = _identifier(record.get("session_id"), "session_id")
        user_id = _identifier(record.get("user_id"), "user_id")
        provider_id = _identifier(provider_value, "provider_id")
        if session_id in mongo_session_ids:
            raise AgentBayReconciliationError(
                "Mongo contains duplicate logical session ownership"
            )
        if provider_id in mongo_by_provider:
            raise AgentBayReconciliationError(
                "One provider session is owned by multiple Mongo sessions "
                f"(fingerprint={_fingerprint(provider_id)})"
            )
        mongo_by_provider[provider_id] = (session_id, user_id)
        mongo_session_ids.add(session_id)
        user_counts[user_id] += 1

    if len(mongo_by_provider) > max_total:
        raise AgentBayReconciliationError(
            "Mongo AgentBay ownership exceeds the configured global cap"
        )
    if any(count > max_per_user for count in user_counts.values()):
        raise AgentBayReconciliationError(
            "Mongo AgentBay ownership exceeds the configured per-user cap"
        )

    listed = await sandbox_cls.list_provider_session_ids({})
    live_provider_ids: set[str] = set()
    for raw_provider_id in listed:
        provider_id = _identifier(raw_provider_id, "provider_id")
        provider = await sandbox_cls.lookup_provider_session(provider_id)
        if provider is None:
            # Provider list pagination can race an exact external deletion.
            continue
        actual = _identifier(
            getattr(provider, "session_id", None), "provider session_id"
        )
        if actual != provider_id:
            raise AgentBayReconciliationError(
                "Provider lookup returned a mismatched session identifier"
            )
        live_provider_ids.add(provider_id)

    # A recently created provider may be absent from an eventually consistent
    # list. Probe every Mongo pointer directly before classifying it missing.
    missing: list[str] = []
    for provider_id in mongo_by_provider:
        provider = await sandbox_cls.lookup_provider_session(provider_id)
        if provider is None:
            missing.append(_fingerprint(provider_id))
            continue
        actual = _identifier(
            getattr(provider, "session_id", None), "provider session_id"
        )
        if actual != provider_id:
            raise AgentBayReconciliationError(
                "Provider lookup returned a mismatched session identifier"
            )
        live_provider_ids.add(provider_id)

    if missing:
        raise AgentBayReconciliationError(
            "Mongo contains exact-missing AgentBay pointers; no writes made "
            f"(fingerprints={','.join(sorted(missing))})"
        )
    orphaned = sorted(live_provider_ids - set(mongo_by_provider))
    if orphaned:
        raise AgentBayReconciliationError(
            "AgentBay contains sessions with no Mongo owner; no writes made "
            "(fingerprints="
            + ",".join(_fingerprint(value) for value in orphaned)
            + ")"
        )

    entries = tuple(
        AgentBayQuotaInventoryEntry(
            session_id=session_id,
            user_id=user_id,
            operation_id=_operation_id(
                deployment_id, session_id, user_id, provider_id
            ),
            provider_id=provider_id,
        )
        for provider_id, (session_id, user_id) in sorted(
            mongo_by_provider.items(), key=lambda item: item[1][0]
        )
    )
    return InventoryAudit(
        entries=entries,
        mongo_owned=len(mongo_by_provider),
        provider_live=len(live_provider_ids),
    )


def _snapshot_matches(
    snapshot: AgentBayQuotaSnapshot,
    inventory: Sequence[AgentBayQuotaInventoryEntry],
) -> bool:
    expected = {
        (
            MongoAgentBayQuotaLedger.session_key(item.session_id),
            MongoAgentBayQuotaLedger.user_key(item.user_id),
            MongoAgentBayQuotaLedger.operation_key(item.operation_id),
            item.operation_id,
            MongoAgentBayQuotaLedger.provider_key(item.provider_id),
            item.provider_id,
        )
        for item in inventory
    }
    actual = {
        (
            item.session_key,
            item.user_key,
            item.operation_key,
            item.operation_id,
            item.provider_key,
            item.provider_id,
        )
        for item in snapshot.reservations
    }
    return snapshot.total == len(expected) and actual == expected


async def reconcile(
    *,
    apply: bool,
    replace_ready_ledger: bool,
    ledger: AgentBayQuotaLedger,
    load_sessions: Callable[[], Awaitable[Sequence[Mapping[str, Any]]]],
    deployment_id: str,
    sandbox_cls: type[AgentBaySandbox] = AgentBaySandbox,
    max_total: int = 20,
    max_per_user: int = 20,
) -> InventoryAudit:
    """Audit, optionally gate provisioning, rescan, and reconcile atomically."""
    audit = await scan_inventory(
        await load_sessions(),
        deployment_id=deployment_id,
        sandbox_cls=sandbox_cls,
        max_total=max_total,
        max_per_user=max_per_user,
    )
    if not apply:
        return audit

    state = await ledger.ensure_ledger()
    if state.bootstrap_state is AgentBayBootstrapState.READY:
        snapshot = await ledger.snapshot()
        if _snapshot_matches(snapshot, audit.entries):
            return audit
        if not replace_ready_ledger:
            raise AgentBayReconciliationError(
                "Ready AgentBay ledger differs from live inventory; rerun only "
                "after review with --apply --replace-ready-ledger"
            )

    transition = await ledger.begin_reconciliation()
    if (
        transition.bootstrap_state is not AgentBayBootstrapState.RECONCILING
        or not isinstance(transition.revision, int)
        or isinstance(transition.revision, bool)
        or transition.revision < 0
    ):
        raise AgentBayQuotaInconsistentError(
            "AgentBay reconciliation transition has no valid revision"
        )

    # App provisioning is now gated. Rescan so inventory changes between the
    # dry phase and the CAS cannot be silently written into the cost ledger.
    gated_audit = await scan_inventory(
        await load_sessions(),
        deployment_id=deployment_id,
        sandbox_cls=sandbox_cls,
        max_total=max_total,
        max_per_user=max_per_user,
    )
    if gated_audit.entries != audit.entries:
        raise AgentBayReconciliationError(
            "AgentBay inventory changed while closing the provisioning gate; "
            "ledger remains reconciling and a fresh audit is required"
        )
    result = await ledger.reconcile_inventory(
        gated_audit.entries,
        expected_revision=transition.revision,
    )
    if result.bootstrap_state is not AgentBayBootstrapState.READY:
        raise AgentBayQuotaInconsistentError(
            "AgentBay ledger did not confirm a ready reconciled inventory"
        )
    return gated_audit


async def _load_session_records(settings: Settings) -> Sequence[Mapping[str, Any]]:
    collection = get_mongodb().client[settings.mongodb_database][
        "sessions"
    ].with_options(
        read_preference=ReadPreference.PRIMARY,
        read_concern=ReadConcern("majority"),
    )
    timeout = settings.agentbay_quota_command_timeout_seconds
    cursor = collection.find(
        {},
        projection={
            "_id": 0,
            "session_id": 1,
            "user_id": 1,
            "sandbox_id": 1,
        },
        max_time_ms=max(1, int(timeout * 1000)),
        comment="agentbay-quota:reconcile-session-scan",
    )
    return await asyncio.wait_for(cursor.to_list(length=None), timeout=timeout)


async def run(*, apply: bool, replace_ready_ledger: bool) -> int:
    settings = get_settings()
    if (settings.sandbox_provider or "").strip().lower() != "agentbay":
        raise AgentBayReconciliationError(
            "SANDBOX_PROVIDER must be agentbay for this audit"
        )
    deployment_id = _identifier(
        settings.agentbay_deployment_id, "deployment_id", 512
    )
    mongodb = get_mongodb()
    await mongodb.initialize()
    try:
        ledger = MongoAgentBayQuotaLedger(
            deployment_id=deployment_id,
            max_total=settings.agentbay_max_sessions_total,
            max_per_user=settings.agentbay_max_sessions_per_user,
            config_version=settings.agentbay_quota_config_version,
            command_timeout_seconds=(
                settings.agentbay_quota_command_timeout_seconds
            ),
        )
        audit = await reconcile(
            apply=apply,
            replace_ready_ledger=replace_ready_ledger,
            ledger=ledger,
            load_sessions=lambda: _load_session_records(settings),
            deployment_id=deployment_id,
            max_total=settings.agentbay_max_sessions_total,
            max_per_user=settings.agentbay_max_sessions_per_user,
        )
        print(
            "AgentBay inventory verified: "
            f"{audit.mongo_owned} Mongo owners, "
            f"{audit.provider_live} live provider sessions."
        )
        if apply:
            print("Mongo AgentBay cost ledger is reconciled and ready.")
        else:
            print("Dry run only; rerun with --apply after reviewing the audit.")
        return 0
    finally:
        await mongodb.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="gate provisioning and atomically write the verified inventory",
    )
    parser.add_argument(
        "--replace-ready-ledger",
        action="store_true",
        help="allow --apply to replace a differing already-ready ledger",
    )
    args = parser.parse_args()
    if args.replace_ready_ledger and not args.apply:
        parser.error("--replace-ready-ledger requires --apply")
    return asyncio.run(
        run(
            apply=args.apply,
            replace_ready_ledger=args.replace_ready_ledger,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
