"""Unit tests for Claw lifecycle cleanup and provisioning coordination.

Runtime ownership must remain durable until destruction is confirmed, and the
distributed lease must permit at most one provisioning replica.
"""
import asyncio
from datetime import datetime, timedelta, UTC
from typing import Optional, List
from unittest.mock import AsyncMock

import pytest

from app.domain.models.claw import Claw, ClawStatus, ClawMessage, ClawAttachment
from app.domain.external.claw import ClawInstanceInfo
from app.domain.services.claw_domain_service import ClawDomainService
from app.application.services.claw_service import ClawService
from app.application.errors.exceptions import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.core.config import get_settings


@pytest.fixture(autouse=True)
def secure_unit_test_settings(monkeypatch):
    monkeypatch.setenv(
        "JWT_SECRET_KEY", "claw-lifecycle-tests-only-secret-32-bytes"
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeClawRepository:
    def __init__(self, claw: Optional[Claw] = None):
        self.claw = claw
        self.deleted_user_ids: list[str] = []
        self.messages: list[tuple] = []

    async def get_by_user_id(self, user_id: str) -> Optional[Claw]:
        return self.claw

    async def get_by_id(self, claw_id: str) -> Optional[Claw]:
        return self.claw

    async def get_by_api_key(self, api_key: str) -> Optional[Claw]:
        return self.claw

    async def create(self, claw: Claw) -> Claw:
        self.claw = claw
        return claw

    async def update(self, claw: Claw) -> Claw:
        self.claw = claw
        return claw

    async def delete_by_user_id(self, user_id: str) -> bool:
        self.deleted_user_ids.append(user_id)
        self.claw = None
        return True

    async def get_messages(self, user_id: str) -> List[ClawMessage]:
        return []

    async def append_message(
        self, user_id: str, role: str, content: str = "",
        attachments: Optional[List[ClawAttachment]] = None,
    ) -> None:
        self.messages.append((user_id, role, content))

    async def clear_messages(self, user_id: str) -> None:
        pass

    async def count_by_statuses(self, statuses) -> int:
        return int(bool(self.claw and self.claw.status in statuses))

    async def list_by_statuses(self, statuses):
        if self.claw and self.claw.status in statuses:
            return [self.claw]
        return []


class FakeClawRuntime:
    creates_immediately = False

    def __init__(
        self,
        fail_create: bool = False,
        ready: bool = True,
        fail_destroy: bool = False,
        raise_destroy: bool = False,
    ):
        self.fail_create = fail_create
        self.ready = ready
        self.fail_destroy = fail_destroy
        self.raise_destroy = raise_destroy
        self.destroyed: list[Optional[str]] = []
        self.create_calls = 0

    async def create(self, claw_id: str, api_key: str) -> ClawInstanceInfo:
        self.create_calls += 1
        if self.fail_create:
            raise RuntimeError("boom")
        return ClawInstanceInfo(address="10.0.0.5", instance_name=f"manus-claw-{claw_id[:8]}")

    async def destroy(self, instance_name: Optional[str]) -> bool:
        self.destroyed.append(instance_name)
        if self.raise_destroy:
            raise RuntimeError("docker unavailable")
        return not self.fail_destroy

    async def wait_for_ready(self, base_url: str) -> bool:
        return self.ready


class ResolvingClawRuntime(FakeClawRuntime):
    def __init__(self, address: str | None = "10.9.0.7", *, fail=False):
        super().__init__()
        self.address = address
        self.fail = fail
        self.resolve_calls: list[tuple[str, str]] = []

    async def resolve_owned(self, instance_name: str, claw_id: str):
        self.resolve_calls.append((instance_name, claw_id))
        if self.fail:
            raise RuntimeError("owned runtime is missing")
        return self.address


class FakeClawClient:
    async def get_history(self, base_url, session_id, limit=200):
        return []

    async def get_file(self, base_url, filename):
        return b"", "application/octet-stream"

    def chat_stream(self, base_url, message, session_id):
        raise NotImplementedError


def _make_claw(**overrides) -> Claw:
    defaults = dict(
        id="claw-1234-abcd",
        user_id="user-1",
        api_key="manus-testkey",
        status=ClawStatus.RUNNING,
        container_name="manus-claw-claw1234",
        container_ip="10.0.0.5",
    )
    defaults.update(overrides)
    return Claw(**defaults)


async def test_expired_claw_destroys_container():
    claw = _make_claw(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    repo = FakeClawRepository(claw)
    runtime = FakeClawRuntime()
    service = ClawDomainService(repo, runtime, FakeClawClient())
    old_ttl = service.settings.claw_ttl_seconds
    service.settings.claw_ttl_seconds = 3600
    try:
        result = await service.get_claw("user-1")
    finally:
        service.settings.claw_ttl_seconds = old_ttl

    assert result is None
    assert runtime.destroyed == ["manus-claw-claw1234"]
    assert repo.deleted_user_ids == ["user-1"]


async def test_delete_claw_destroys_container():
    claw = _make_claw()
    repo = FakeClawRepository(claw)
    runtime = FakeClawRuntime()
    service = ClawDomainService(repo, runtime, FakeClawClient())

    deleted = await service.delete_claw("user-1")

    assert deleted is True
    assert runtime.destroyed == ["manus-claw-claw1234"]
    assert repo.deleted_user_ids == ["user-1"]


async def test_stale_destroy_claim_never_touches_newer_runtime_generation():
    stale = _make_claw(revision=4)

    class ClaimLostRepository(FakeClawRepository):
        async def claim_runtime_destroy(self, claw):
            return None

    repo = ClaimLostRepository(stale)
    runtime = FakeClawRuntime()
    service = ClawDomainService(repo, runtime, FakeClawClient())

    assert await service.delete_claw("user-1") is False
    assert runtime.destroyed == []
    assert repo.claw is stale


async def test_get_claw_refreshes_owned_address_before_health_check():
    claw = _make_claw(container_ip="10.0.0.5")
    repo = FakeClawRepository(claw)
    runtime = ResolvingClawRuntime("10.9.0.7")
    service = ClawDomainService(repo, runtime, FakeClawClient())
    service._health_check = AsyncMock(return_value=True)

    result = await service.get_claw("user-1")

    assert result is claw
    assert result.container_ip == "10.9.0.7"
    assert runtime.resolve_calls == [
        ("manus-claw-claw1234", "claw-1234-abcd")
    ]
    service._health_check.assert_awaited_once_with("http://10.9.0.7:18788")


async def test_get_claw_never_health_checks_stale_ip_when_owned_runtime_missing():
    claw = _make_claw(container_ip="10.0.0.5")
    repo = FakeClawRepository(claw)
    runtime = ResolvingClawRuntime(fail=True)
    service = ClawDomainService(repo, runtime, FakeClawClient())
    service._health_check = AsyncMock(return_value=True)

    result = await service.get_claw("user-1")

    assert result is claw
    assert result.status == ClawStatus.STOPPED
    assert result.container_ip == "10.0.0.5"
    service._health_check.assert_not_awaited()


async def test_delete_claw_without_record_is_noop():
    repo = FakeClawRepository(None)
    runtime = FakeClawRuntime()
    service = ClawDomainService(repo, runtime, FakeClawClient())

    deleted = await service.delete_claw("user-1")

    assert deleted is False
    assert runtime.destroyed == []


async def test_dynamic_runtime_is_destroyed_even_when_delete_opt_out_is_false():
    claw = _make_claw()
    repo = FakeClawRepository(claw)
    runtime = FakeClawRuntime()
    service = ClawDomainService(repo, runtime, FakeClawClient())
    old_value = service.settings.claw_destroy_on_delete
    service.settings.claw_destroy_on_delete = False
    try:
        assert await service.delete_claw("user-1") is True
    finally:
        service.settings.claw_destroy_on_delete = old_value

    assert runtime.destroyed == ["manus-claw-claw1234"]
    assert repo.claw is None


@pytest.mark.parametrize("raise_destroy", [False, True])
async def test_delete_destroy_failure_retains_error_record_and_ownership(
    raise_destroy: bool,
):
    claw = _make_claw()
    repo = FakeClawRepository(claw)
    runtime = FakeClawRuntime(
        fail_destroy=not raise_destroy,
        raise_destroy=raise_destroy,
    )
    service = ClawDomainService(repo, runtime, FakeClawClient())

    deleted = await service.delete_claw("user-1")

    assert deleted is False
    assert repo.deleted_user_ids == []
    assert repo.claw is claw
    assert repo.claw.status == ClawStatus.ERROR
    assert repo.claw.container_name == "manus-claw-claw1234"
    assert repo.claw.container_ip == "10.0.0.5"
    assert "retained" in repo.claw.error_message


async def test_expired_destroy_failure_does_not_delete_record():
    claw = _make_claw(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    repo = FakeClawRepository(claw)
    runtime = FakeClawRuntime(fail_destroy=True)
    service = ClawDomainService(repo, runtime, FakeClawClient())
    old_ttl = service.settings.claw_ttl_seconds
    service.settings.claw_ttl_seconds = 3600
    try:
        result = await service.get_claw("user-1")
    finally:
        service.settings.claw_ttl_seconds = old_ttl

    assert result is claw
    assert result.status == ClawStatus.ERROR
    assert result.container_name == "manus-claw-claw1234"
    assert repo.deleted_user_ids == []


async def test_destroy_failure_can_be_retried_without_losing_ownership():
    claw = _make_claw()
    repo = FakeClawRepository(claw)
    runtime = FakeClawRuntime(fail_destroy=True)
    service = ClawDomainService(repo, runtime, FakeClawClient())

    assert await service.delete_claw("user-1") is False
    assert repo.claw.container_name == "manus-claw-claw1234"

    runtime.fail_destroy = False
    assert await service.delete_claw("user-1") is True
    assert runtime.destroyed == [
        "manus-claw-claw1234",
        "manus-claw-claw1234",
    ]
    assert repo.claw is None


async def test_reprovision_refuses_to_overwrite_failed_cleanup_ownership():
    claw = _make_claw(status=ClawStatus.ERROR)
    repo = FakeClawRepository(claw)
    runtime = FakeClawRuntime(fail_destroy=True)
    service = ClawDomainService(repo, runtime, FakeClawClient())

    with pytest.raises(RuntimeError, match="ownership was retained"):
        await service.prepare_claw_for_creation("user-1")

    assert repo.claw.status == ClawStatus.ERROR
    assert repo.claw.container_name == "manus-claw-claw1234"
    assert repo.claw.container_ip == "10.0.0.5"


async def test_provision_failure_destroys_container():
    claw = _make_claw(status=ClawStatus.CREATING, container_name=None, container_ip=None)
    repo = FakeClawRepository(claw)
    runtime = FakeClawRuntime(ready=False)  # created but never becomes healthy
    service = ClawDomainService(repo, runtime, FakeClawClient())

    await service.provision_claw_instance(claw, ttl_seconds=3600)

    assert claw.status == ClawStatus.ERROR
    assert runtime.destroyed == ["manus-claw-claw-123"]
    assert claw.container_name is None
    assert claw.container_ip is None


async def test_provision_rollback_failure_retains_runtime_ownership():
    claw = _make_claw(
        status=ClawStatus.CREATING,
        container_name=None,
        container_ip=None,
    )
    repo = FakeClawRepository(claw)
    runtime = FakeClawRuntime(ready=False, fail_destroy=True)
    service = ClawDomainService(repo, runtime, FakeClawClient())

    await service.provision_claw_instance(claw, ttl_seconds=3600)

    assert claw.status == ClawStatus.ERROR
    assert claw.container_name == "manus-claw-claw-123"
    assert claw.container_ip == "10.0.0.5"
    assert "rollback failed" in claw.error_message


async def test_provision_success_sets_expiry_from_start_time():
    claw = _make_claw(status=ClawStatus.CREATING, container_name=None, container_ip=None)
    repo = FakeClawRepository(claw)
    runtime = FakeClawRuntime(ready=True)
    service = ClawDomainService(repo, runtime, FakeClawClient())

    before = datetime.now(UTC)
    await service.provision_claw_instance(claw, ttl_seconds=3600)
    after = datetime.now(UTC)

    assert claw.status == ClawStatus.RUNNING
    assert runtime.destroyed == []
    # expires_at must be anchored at provisioning start, not at readiness,
    # so the DB record never outlives the container's own TTL clock.
    assert before + timedelta(seconds=3600) <= claw.expires_at <= after + timedelta(seconds=3600)


async def test_cancelled_provisioning_destroys_partially_started_container():
    claw = _make_claw(
        status=ClawStatus.CREATING,
        container_name=None,
        container_ip=None,
    )
    repo = FakeClawRepository(claw)

    class BlockingRuntime(FakeClawRuntime):
        def __init__(self):
            super().__init__()
            self.wait_started = asyncio.Event()

        async def wait_for_ready(self, base_url: str) -> bool:
            self.wait_started.set()
            await asyncio.Event().wait()
            return False

    runtime = BlockingRuntime()
    service = ClawDomainService(repo, runtime, FakeClawClient())
    task = asyncio.create_task(service.provision_claw_instance(claw, 3600))
    await runtime.wait_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime.destroyed == ["manus-claw-claw-123"]
    assert claw.status == ClawStatus.ERROR
    assert claw.container_name is None
    assert claw.container_ip is None


class FakeRedis:
    """Small in-memory model of the Lua operations used by ClawService."""

    def __init__(self, *, fail_set: bool = False):
        self.values: dict[str, str] = {}
        self.fail_set = fail_set
        self.set_attempts = 0

    async def set(self, key, value, *, nx=False, ex=None):
        self.set_attempts += 1
        if self.fail_set:
            raise ConnectionError("redis offline")
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def exists(self, key):
        return int(key in self.values)

    async def eval(self, script, numkeys, key, owner_token, *args):
        if self.values.get(key) != owner_token:
            return 0
        if "redis.call('del'" in script:
            del self.values[key]
            return 1
        if "redis.call('expire'" in script:
            return 1
        raise AssertionError("unexpected Lua script")


class FakeRedisHolder:
    def __init__(self, client):
        self.client = client


class FakeProvisionDomain:
    def __init__(self, repo: FakeClawRepository):
        self.claw_repository = repo
        self.provision_calls = 0
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def prepare_claw_for_creation(self, user_id: str) -> Claw:
        return self.claw_repository.claw

    async def provision_claw_instance(self, claw: Claw, ttl_seconds: int):
        self.provision_calls += 1
        self.started.set()
        await self.finish.wait()


async def test_distributed_lock_allows_only_one_concurrent_provisioner(
    monkeypatch,
):
    redis = FakeRedis()
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: FakeRedisHolder(redis),
    )
    repo = FakeClawRepository(
        _make_claw(
            status=ClawStatus.CREATING,
            container_name=None,
            container_ip=None,
        )
    )
    domain = FakeProvisionDomain(repo)
    first = ClawService(domain)
    second = ClawService(domain)

    await asyncio.gather(
        first.create_claw("user-1"),
        second.create_claw("user-1"),
    )
    await asyncio.wait_for(domain.started.wait(), timeout=1)

    assert domain.provision_calls == 1
    assert len(redis.values) == 1

    domain.finish.set()
    await asyncio.gather(*first._bg_tasks, *second._bg_tasks)
    assert redis.values == {}


async def test_expired_owner_cannot_release_new_owners_lock(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: FakeRedisHolder(redis),
    )
    repo = FakeClawRepository()
    domain = FakeProvisionDomain(repo)
    old_service = ClawService(domain)
    new_service = ClawService(domain)
    lock_key = "claw:provision:claw-1"

    old_owner = await old_service._acquire_provision_lock(lock_key)
    assert old_owner
    # Model Redis expiring the old lease before another replica acquires it.
    redis.values.pop(lock_key)
    new_owner = await new_service._acquire_provision_lock(lock_key)
    assert new_owner and new_owner != old_owner

    assert await old_service._release_provision_lock(lock_key, old_owner) is False
    assert redis.values[lock_key] == new_owner
    assert await new_service._release_provision_lock(lock_key, new_owner) is True


async def test_lost_lease_cancels_provisioning_and_preserves_new_owner_lock(
    monkeypatch,
):
    redis = FakeRedis()
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: FakeRedisHolder(redis),
    )
    repo = FakeClawRepository(
        _make_claw(
            status=ClawStatus.CREATING,
            container_name=None,
            container_ip=None,
        )
    )
    domain = FakeProvisionDomain(repo)
    service = ClawService(domain)
    # Keep the test fast while still exercising the real renewal loop.
    monkeypatch.setattr(service, "_provision_lock_ttl_seconds", lambda: 3)

    await service.create_claw("user-1")
    await asyncio.wait_for(domain.started.wait(), timeout=1)
    lock_key = f"claw:provision:{repo.claw.id}"
    redis.values[lock_key] = "replacement-owner"
    await asyncio.wait_for(
        asyncio.gather(*service._bg_tasks, return_exceptions=True),
        timeout=2,
    )

    assert domain.provision_calls == 1
    # The fenced old owner leaves the durable generation untouched. Marking it
    # ERROR here could overwrite a new replica that adopted the same runtime.
    assert repo.claw.status == ClawStatus.CREATING
    assert repo.claw.error_message is None
    assert redis.values[lock_key] == "replacement-owner"


async def test_redis_outage_fails_closed_for_concurrent_creation(monkeypatch):
    redis = FakeRedis(fail_set=True)
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: FakeRedisHolder(redis),
    )
    repo = FakeClawRepository(
        _make_claw(
            status=ClawStatus.CREATING,
            container_name=None,
            container_ip=None,
        )
    )
    domain = FakeProvisionDomain(repo)
    first = ClawService(domain)
    second = ClawService(domain)

    results = await asyncio.gather(
        first.create_claw("user-1"),
        second.create_claw("user-1"),
    )

    assert redis.set_attempts == 2
    assert domain.provision_calls == 0
    # Do not clobber a durable CREATING record: another replica may already be
    # provisioning it.  Failing closed means no additional provisioner starts.
    assert all(result.status == ClawStatus.CREATING for result in results)
    assert all(not service._local_provision_locks for service in (first, second))
    assert all(not service._local_creation_locks for service in (first, second))


class MultiClawRepository:
    """Small multi-user repository that raises on duplicate user inserts."""

    def __init__(self):
        self.claws: dict[str, Claw] = {}
        self.create_calls = 0
        self.messages = []

    async def get_by_user_id(self, user_id):
        return self.claws.get(user_id)

    async def get_by_id(self, claw_id):
        return next((c for c in self.claws.values() if c.id == claw_id), None)

    async def get_by_api_key(self, api_key):
        return next((c for c in self.claws.values() if c.api_key == api_key), None)

    async def count_by_statuses(self, statuses):
        return sum(claw.status in statuses for claw in self.claws.values())

    async def list_by_statuses(self, statuses):
        return [claw for claw in self.claws.values() if claw.status in statuses]

    async def create(self, claw):
        # Widen the race window: without the Redis user/capacity leases both
        # replicas reach this insert and one raises DuplicateKey in production.
        await asyncio.sleep(0.02)
        if claw.user_id in self.claws:
            raise RuntimeError("DuplicateKey")
        self.create_calls += 1
        self.claws[claw.user_id] = claw
        return claw

    async def update(self, claw):
        self.claws[claw.user_id] = claw
        return claw

    async def delete_by_user_id(self, user_id):
        return self.claws.pop(user_id, None) is not None

    async def append_message(self, user_id, role, content="", attachments=None):
        self.messages.append((user_id, role, content))


async def test_user_creation_lease_prevents_cross_replica_duplicate_insert(
    monkeypatch,
):
    redis = FakeRedis()
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: FakeRedisHolder(redis),
    )
    repository = MultiClawRepository()
    runtime = FakeClawRuntime()
    first = ClawService(
        ClawDomainService(repository, runtime, FakeClawClient())
    )
    second = ClawService(
        ClawDomainService(repository, runtime, FakeClawClient())
    )
    monkeypatch.setattr(first.settings, "claw_max_instances_total", 10)

    results = await asyncio.gather(
        first.create_claw("new-user"),
        second.create_claw("new-user"),
    )
    pending = [*first._bg_tasks, *second._bg_tasks]
    if pending:
        await asyncio.gather(*pending)

    assert repository.create_calls == 1
    assert runtime.create_calls == 1
    assert results[0].id == results[1].id
    assert repository.claws["new-user"].status == ClawStatus.RUNNING


async def test_capacity_count_and_insert_are_serialized_across_replicas(
    monkeypatch,
):
    redis = FakeRedis()
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: FakeRedisHolder(redis),
    )
    repository = MultiClawRepository()
    runtime = FakeClawRuntime()
    first = ClawService(
        ClawDomainService(repository, runtime, FakeClawClient())
    )
    second = ClawService(
        ClawDomainService(repository, runtime, FakeClawClient())
    )
    monkeypatch.setattr(first.settings, "claw_max_instances_total", 1)

    results = await asyncio.gather(
        first.create_claw("user-a"),
        second.create_claw("user-b"),
        return_exceptions=True,
    )
    pending = [*first._bg_tasks, *second._bg_tasks]
    if pending:
        await asyncio.gather(*pending)

    assert repository.create_calls == 1
    assert runtime.create_calls == 1
    assert sum(isinstance(result, Claw) for result in results) == 1
    assert sum(isinstance(result, RuntimeError) for result in results) == 1


async def test_maintenance_retries_error_runtime_ownership_cleanup():
    claw = _make_claw(status=ClawStatus.ERROR)
    repository = FakeClawRepository(claw)
    runtime = FakeClawRuntime(fail_destroy=True)
    service = ClawDomainService(repository, runtime, FakeClawClient())

    first = await service.cleanup_instances()
    assert first == {"removed": 0, "errored": 1}
    assert claw.container_name == "manus-claw-claw1234"

    runtime.fail_destroy = False
    second = await service.cleanup_instances()
    assert second == {"removed": 0, "errored": 0}
    assert claw.container_name is None
    assert claw.container_ip is None
    assert runtime.destroyed == [
        "manus-claw-claw1234",
        "manus-claw-claw1234",
    ]


async def test_application_delete_reports_missing_busy_and_cleanup_failure(
    monkeypatch,
):
    empty_repo = FakeClawRepository(None)
    empty_service = ClawService(
        ClawDomainService(empty_repo, FakeClawRuntime(), FakeClawClient())
    )
    with pytest.raises(NotFoundError):
        await empty_service.delete_claw("user-1")

    creating = _make_claw(
        status=ClawStatus.CREATING,
        container_name=None,
        container_ip=None,
    )
    busy_repo = FakeClawRepository(creating)
    busy_service = ClawService(
        ClawDomainService(busy_repo, FakeClawRuntime(), FakeClawClient())
    )
    busy_redis = FakeRedis()
    busy_redis.values[f"claw:provision:{creating.id}"] = "other-owner"
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: FakeRedisHolder(busy_redis),
    )
    with pytest.raises(ConflictError):
        await busy_service.delete_claw("user-1")

    failed_repo = FakeClawRepository(_make_claw())
    failed_service = ClawService(
        ClawDomainService(
            failed_repo,
            FakeClawRuntime(fail_destroy=True),
            FakeClawClient(),
        )
    )
    with pytest.raises(ServiceUnavailableError):
        await failed_service.delete_claw("user-1")


class FakeChatDomain:
    def __init__(self, repository: FakeClawRepository):
        self.claw_repository = repository
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.stream_calls = 0

    async def validate_claw_for_chat(self, user_id: str) -> Claw:
        return self.claw_repository.claw

    async def process_chat_stream(
        self, user_id: str, base_url: str, message: str, session_id: str
    ):
        self.stream_calls += 1
        self.started.set()
        await self.finish.wait()
        yield {"type": "done", "stop_reason": "end_turn"}


class FakeChatEventBus:
    async def publish(self, user_id: str, event: dict) -> None:
        pass


async def test_chat_turn_lease_rejects_cross_replica_concurrent_socket(
    monkeypatch,
):
    redis = FakeRedis()
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: FakeRedisHolder(redis),
    )
    repository = FakeClawRepository(_make_claw())
    domain = FakeChatDomain(repository)
    first = ClawService(domain)
    second = ClawService(domain)
    first.event_bus = FakeChatEventBus()
    second.event_bus = FakeChatEventBus()

    await first.send_message("user-1", "first", "default")
    await asyncio.wait_for(domain.started.wait(), timeout=1)

    with pytest.raises(ConflictError, match="already in progress"):
        await second.send_message("user-1", "second", "default")
    with pytest.raises(ValueError, match="default Claw conversation"):
        await second.send_message("user-1", "session bypass", "other-session")

    assert domain.stream_calls == 1
    assert repository.messages == [("user-1", "user", "first")]
    assert len(redis.values) == 1

    domain.finish.set()
    await asyncio.wait_for(
        asyncio.gather(*first._bg_tasks, return_exceptions=True),
        timeout=1,
    )
    assert redis.values == {}


async def test_hung_chat_lease_renewal_cancels_stream_before_ttl(
    monkeypatch,
):
    class HangingRenewRedis(FakeRedis):
        async def eval(self, script, numkeys, key, owner_token, *args):
            if "redis.call('expire'" in script:
                await asyncio.Event().wait()
            return await super().eval(
                script, numkeys, key, owner_token, *args
            )

    redis = HangingRenewRedis()
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: FakeRedisHolder(redis),
    )
    repository = FakeClawRepository(_make_claw())
    domain = FakeChatDomain(repository)
    service = ClawService(domain)
    service.event_bus = FakeChatEventBus()
    monkeypatch.setattr(service, "_chat_turn_lock_ttl_seconds", lambda: 3)
    monkeypatch.setattr(
        service, "_chat_coordination_timeout_seconds", lambda: 0.01
    )

    await service.send_message("user-1", "first", "default")
    await asyncio.wait_for(domain.started.wait(), timeout=1)
    await asyncio.wait_for(
        asyncio.gather(*service._bg_tasks, return_exceptions=True),
        timeout=2,
    )

    # Renewal starts at one third of the 3-second TTL and fails after the
    # 10ms command deadline.  The blocked stream is cancelled and the
    # owner-safe release removes the still-owned key before its TTL expires.
    assert domain.stream_calls == 1
    assert redis.values == {}
