import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import docker
import pytest

from app.core.config import get_settings
from app.domain.external.sandbox import (
    SandboxProvisioningError,
    SandboxUnavailableError,
)
from app.domain.models.claw import Claw, ClawStatus
from app.domain.models.session import Session
from app.domain.services.claw_domain_service import ClawDomainService
from app.infrastructure.external.claw import readiness as readiness_module
from app.infrastructure.external import docker_async as docker_async_module
from app.infrastructure.external.claw.docker_claw_runtime import (
    DockerClawRuntime,
)
from app.infrastructure.external.sandbox.agentbay_sandbox import AgentBaySandbox
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.infrastructure.external.sandbox.passthrough_provisioner import (
    PassthroughSandboxProvisioner,
)


async def _wait_thread_event(event: threading.Event) -> None:
    for _ in range(1000):
        if event.is_set():
            return
        await asyncio.sleep(0)
    raise AssertionError("worker thread did not reach the expected point")


async def _wait_registry_clear(registry: dict, key: str) -> None:
    for _ in range(1000):
        if key not in registry:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"retained operation {key} did not finish")


async def _count_event_loop_ticks(stop: asyncio.Event, counter: list[int]) -> None:
    while not stop.is_set():
        counter[0] += 1
        await asyncio.sleep(0)


class _FakeContainer:
    def __init__(self, provider: "_FakeDockerProvider") -> None:
        self.provider = provider
        self.attrs = {
            "NetworkSettings": {
                "IPAddress": "172.20.0.9",
                "Networks": {},
                "Ports": {},
            }
        }

    def reload(self) -> None:
        self.provider.reload_count += 1

    def remove(self, force: bool = False) -> None:
        self.provider.remove_started.set()
        if self.provider.block_remove:
            self.provider.remove_release.wait(timeout=2)
        self.provider.removed = True


class _FakeContainers:
    def __init__(self, provider: "_FakeDockerProvider") -> None:
        self.provider = provider

    def run(self, **kwargs):
        self.provider.run_count += 1
        self.provider.run_config = kwargs
        self.provider.run_started.set()
        self.provider.run_release.wait(timeout=2)
        self.provider.created = True
        self.provider.removed = False
        self.provider.container.attrs["Config"] = {
            "Labels": dict(kwargs.get("labels") or {})
        }
        self.provider.container.name = kwargs.get("name")
        return self.provider.container

    def get(self, container_name: str):
        self.provider.get_count += 1
        if self.provider.block_get:
            self.provider.get_started.set()
            self.provider.get_release.wait(timeout=2)
        if not self.provider.created or self.provider.removed:
            raise docker.errors.NotFound("container missing")
        return self.provider.container


class _FakeDockerClient:
    def __init__(self, provider: "_FakeDockerProvider") -> None:
        self.provider = provider
        self.containers = _FakeContainers(provider)

    def close(self) -> None:
        self.provider.close_count += 1


class _FakeDockerProvider:
    def __init__(self) -> None:
        self.run_count = 0
        self.get_count = 0
        self.reload_count = 0
        self.close_count = 0
        self.run_config = None
        self.created = False
        self.removed = False
        self.block_get = False
        self.block_remove = False
        self.run_started = threading.Event()
        self.run_release = threading.Event()
        self.get_started = threading.Event()
        self.get_release = threading.Event()
        self.remove_started = threading.Event()
        self.remove_release = threading.Event()
        self.container = _FakeContainer(self)

    def client(self, *args, **kwargs) -> _FakeDockerClient:
        return _FakeDockerClient(self)


class _RuntimeOwnershipRepository:
    def __init__(self) -> None:
        self.updates = []

    async def update_runtime_ownership(
        self,
        session_id,
        sandbox_id,
        task_id,
        sandbox_provider=None,
        task_sandbox_id=None,
    ):
        self.updates.append(
            (session_id, sandbox_id, task_id, sandbox_provider)
        )
        return True


def _configure_dynamic_sandbox(monkeypatch, provider: _FakeDockerProvider) -> None:
    monkeypatch.setenv("SANDBOX_ADDRESS", "")
    monkeypatch.setenv("SANDBOX_NAME_PREFIX", "bounded-sandbox")
    monkeypatch.setenv("RUNTIME_NETWORK_ISOLATION", "true")
    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.docker_sandbox.docker.from_env",
        provider.client,
    )
    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.docker_sandbox."
        "require_containerized_runtime_gateway",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.docker_sandbox."
        "ensure_runtime_egress_network",
        lambda *_args, **_kwargs: "test-egress-network",
    )
    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.docker_sandbox."
        "ensure_private_runtime_network",
        lambda *_args, **_kwargs: "test-private-network",
    )
    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.docker_sandbox."
        "connect_runtime_to_private_network",
        lambda *_args, **_kwargs: "172.20.0.9",
    )
    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.docker_sandbox."
        "resolve_owned_runtime_control_ip",
        lambda *_args, **_kwargs: "172.20.0.9",
    )
    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.docker_sandbox."
        "owned_runtime_network_intent_exists",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.docker_sandbox."
        "remove_private_runtime_network",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(DockerSandbox, "_DOCKER_SDK_TIMEOUT_SECONDS", 0.02)
    DockerSandbox._CREATE_OPERATIONS.clear()
    get_settings.cache_clear()


async def test_docker_timeout_retry_waits_late_create_without_second_run(
    monkeypatch,
):
    provider = _FakeDockerProvider()
    _configure_dynamic_sandbox(monkeypatch, provider)
    repository = _RuntimeOwnershipRepository()
    provisioner = PassthroughSandboxProvisioner(
        DockerSandbox, repository
    )
    session = Session(
        id="session-timeout",
        user_id="user",
        agent_id="agent",
    )
    stop = asyncio.Event()
    ticks = [0]
    ticker = asyncio.create_task(_count_event_loop_ticks(stop, ticks))

    try:
        with pytest.raises(SandboxProvisioningError) as raised:
            await provisioner.ensure_locked(session)
        stop.set()
        await ticker

        retained_id = raised.value.sandbox_id
        assert ticks[0] > 1
        assert session.sandbox_id == retained_id
        assert repository.updates[-1][1:] == (
            retained_id,
            None,
            "docker",
        )
        with pytest.raises(
            SandboxUnavailableError,
            match="indeterminate lifecycle operation",
        ):
            await provisioner.ensure_locked(session)
        assert provider.run_count == 1

        provider.run_release.set()
        await _wait_registry_clear(
            DockerSandbox._CREATE_OPERATIONS, retained_id
        )
        sandbox = await provisioner.ensure_locked(session)
        assert sandbox.id == retained_id
        assert provider.run_count == 1
        assert await sandbox.destroy() is True
    finally:
        provider.run_release.set()
        stop.set()
        if not ticker.done():
            await ticker
        DockerSandbox._CREATE_OPERATIONS.clear()


async def test_docker_create_cancellation_persists_cleanup_pointer(
    monkeypatch,
):
    provider = _FakeDockerProvider()
    _configure_dynamic_sandbox(monkeypatch, provider)
    monkeypatch.setattr(DockerSandbox, "_DOCKER_SDK_TIMEOUT_SECONDS", 1.0)
    repository = _RuntimeOwnershipRepository()
    provisioner = PassthroughSandboxProvisioner(
        DockerSandbox, repository
    )
    session = Session(
        id="session-cancel",
        user_id="user",
        agent_id="agent",
    )

    task = asyncio.create_task(provisioner.ensure_locked(session))
    await _wait_thread_event(provider.run_started)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.sandbox_id
    assert session.sandbox_provider == "docker"
    assert repository.updates[-1][1] == session.sandbox_id

    provider.run_release.set()
    await _wait_registry_clear(
        DockerSandbox._CREATE_OPERATIONS, session.sandbox_id
    )
    sandbox = await provisioner.ensure_locked(session)
    assert provider.run_count == 1
    assert await sandbox.destroy() is True
    DockerSandbox._CREATE_OPERATIONS.clear()


async def test_docker_get_and_destroy_do_not_block_event_loop(monkeypatch):
    provider = _FakeDockerProvider()
    _configure_dynamic_sandbox(monkeypatch, provider)
    provider.block_get = True
    stop = asyncio.Event()
    ticks = [0]
    ticker = asyncio.create_task(_count_event_loop_ticks(stop, ticks))

    get_task = asyncio.create_task(DockerSandbox.get("sandbox-existing"))
    await _wait_thread_event(provider.get_started)
    with pytest.raises(SandboxUnavailableError):
        await get_task
    assert ticks[0] > 1
    provider.get_release.set()

    provider.block_get = False
    provider.created = True
    provider.block_remove = True
    sandbox = DockerSandbox(
        ip="172.20.0.9",
        container_name="sandbox-existing",
        managed_container=True,
    )
    assert await sandbox.destroy() is False
    assert sandbox._container_name == "sandbox-existing"
    assert ticks[0] > 1
    provider.remove_release.set()
    stop.set()
    await ticker


async def test_docker_capacity_is_bounded_per_event_loop(monkeypatch):
    monkeypatch.setattr(
        docker_async_module, "_MAX_INFLIGHT_DOCKER_CALLS", 2
    )
    release = threading.Event()
    state_lock = threading.Lock()
    active = 0
    max_active = 0
    started = 0

    def blocking_call() -> str:
        nonlocal active, max_active, started
        with state_lock:
            active += 1
            started += 1
            max_active = max(max_active, active)
        release.wait(timeout=1)
        with state_lock:
            active -= 1
        return "ok"

    tasks = [
        asyncio.create_task(
            docker_async_module.run_bounded_docker_call(
                blocking_call,
                timeout_seconds=0.5,
                operation=f"capacity-{index}",
            )
        )
        for index in range(5)
    ]
    try:
        for _ in range(1000):
            with state_lock:
                observed_started = started
            if observed_started >= 2:
                break
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)
        with state_lock:
            assert started == 2
            assert max_active == 2
        release.set()
        assert await asyncio.gather(*tasks) == ["ok"] * 5
        assert max_active == 2
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_docker_capacity_primitives_are_not_reused_across_event_loops():
    async def one_call(value: str) -> str:
        return await docker_async_module.run_bounded_docker_call(
            lambda: value,
            timeout_seconds=0.5,
            operation=f"loop-{value}",
            serialize_key="same-resource-name",
        )

    assert asyncio.run(one_call("first")) == "first"
    assert asyncio.run(one_call("second")) == "second"


async def test_docker_prepublish_failure_starts_no_provider_mutation(
    monkeypatch,
):
    provider = _FakeDockerProvider()
    _configure_dynamic_sandbox(monkeypatch, provider)

    class Repository:
        async def update_runtime_ownership(self, *args):
            raise ConnectionError("Mongo unavailable before create")

    provisioner = PassthroughSandboxProvisioner(
        DockerSandbox, Repository()
    )
    session = Session(
        id="session-prepublish-failure",
        user_id="user",
        agent_id="agent",
    )

    with pytest.raises(ConnectionError, match="before create"):
        await provisioner.ensure_locked(session)

    assert provider.run_count == 0
    assert session.sandbox_id is None
    assert session.sandbox_provider is None


class _SupervisorResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "success": True,
            "message": "starting",
            "data": [{"name": "api", "statename": "STARTING"}],
        }


async def test_sandbox_readiness_exhaustion_raises_provisioning_error():
    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "sandbox-not-ready"
    sandbox.base_url = "http://sandbox"
    sandbox.client = SimpleNamespace(
        get=AsyncMock(return_value=_SupervisorResponse())
    )
    sandbox._READINESS_MAX_ATTEMPTS = 3
    sandbox._READINESS_RETRY_INTERVAL_SECONDS = 0
    sandbox._READINESS_REQUEST_TIMEOUT_SECONDS = 0.02
    sandbox._READINESS_TOTAL_TIMEOUT_SECONDS = 1

    with pytest.raises(SandboxProvisioningError) as raised:
        await sandbox.ensure_sandbox()

    assert raised.value.sandbox_id == "sandbox-not-ready"
    assert sandbox.client.get.await_count == 3


async def test_sandbox_readiness_has_one_total_deadline():
    calls = 0

    async def hanging_get(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "sandbox-hung"
    sandbox.base_url = "http://sandbox"
    sandbox.client = SimpleNamespace(get=hanging_get)
    sandbox._READINESS_MAX_ATTEMPTS = 30
    sandbox._READINESS_RETRY_INTERVAL_SECONDS = 0.002
    sandbox._READINESS_REQUEST_TIMEOUT_SECONDS = 0.01
    sandbox._READINESS_TOTAL_TIMEOUT_SECONDS = 0.025

    started = asyncio.get_running_loop().time()
    with pytest.raises(SandboxProvisioningError):
        await sandbox.ensure_sandbox()
    elapsed = asyncio.get_running_loop().time() - started

    assert calls >= 1
    assert elapsed < 0.15


def _claw_settings() -> SimpleNamespace:
    return SimpleNamespace(
        runtime_network_isolation=False,
        claw_network=None,
        manus_api_base_url="http://gateway.internal:8000",
        backend_sandbox_url=None,
        backend_internal_url=None,
        backend_public_url=None,
        claw_name_prefix="bounded-claw",
        claw_image="example/claw:test",
        claw_ttl_seconds=0,
        claw_memory_limit="1g",
        claw_nano_cpus=1_000_000_000,
        claw_pids_limit=128,
        claw_publish_host_ports=False,
        claw_http_container_port=18788,
        claw_gateway_container_port=18789,
        claw_host_bind_address="127.0.0.1",
        claw_ready_timeout=1,
    )


async def test_claw_timeout_retains_name_and_destroy_never_claims_success(
    monkeypatch,
):
    provider = _FakeDockerProvider()
    monkeypatch.setattr("docker.from_env", provider.client)
    monkeypatch.setattr(DockerClawRuntime, "_DOCKER_SDK_TIMEOUT_SECONDS", 0.02)
    DockerClawRuntime._CREATE_OPERATIONS.clear()
    runtime = object.__new__(DockerClawRuntime)
    runtime.settings = _claw_settings()
    monkeypatch.setattr(runtime, "_is_running_in_container", lambda: False)
    stop = asyncio.Event()
    ticks = [0]
    ticker = asyncio.create_task(_count_event_loop_ticks(stop, ticks))

    try:
        with pytest.raises(Exception) as raised:
            await runtime.create("claw-12345678", "runtime-secret")
        instance_name = raised.value.claw_instance_name
        assert instance_name == "bounded-claw-claw-123"
        assert ticks[0] > 1
        assert await runtime.destroy(instance_name) is False

        provider.run_release.set()
        await _wait_registry_clear(
            DockerClawRuntime._CREATE_OPERATIONS, instance_name
        )
        assert await runtime.destroy(instance_name) is True
        assert provider.removed is True
        assert provider.run_count == 1
    finally:
        provider.run_release.set()
        stop.set()
        await ticker
        DockerClawRuntime._CREATE_OPERATIONS.clear()


async def test_claw_create_cancellation_carries_deterministic_name(monkeypatch):
    provider = _FakeDockerProvider()
    monkeypatch.setattr("docker.from_env", provider.client)
    monkeypatch.setattr(DockerClawRuntime, "_DOCKER_SDK_TIMEOUT_SECONDS", 1.0)
    DockerClawRuntime._CREATE_OPERATIONS.clear()
    runtime = object.__new__(DockerClawRuntime)
    runtime.settings = _claw_settings()

    task = asyncio.create_task(runtime.create("claw-abcdefgh", "secret"))
    await _wait_thread_event(provider.run_started)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert raised.value.claw_instance_name == "bounded-claw-claw-abc"

    provider.run_release.set()
    await _wait_registry_clear(
        DockerClawRuntime._CREATE_OPERATIONS,
        raised.value.claw_instance_name,
    )
    assert await runtime.destroy(raised.value.claw_instance_name) is True
    DockerClawRuntime._CREATE_OPERATIONS.clear()


async def test_claw_same_generation_is_adopted_without_stale_replacement(
    monkeypatch,
):
    provider = _FakeDockerProvider()
    provider.created = True
    provider.container.attrs["Config"] = {
        "Labels": {
            "ai-manus.kind": "claw",
            "ai-manus.claw_id": "claw-stale-late",
        }
    }
    provider.run_release.set()
    monkeypatch.setattr("docker.from_env", provider.client)
    monkeypatch.setattr(DockerClawRuntime, "_DOCKER_SDK_TIMEOUT_SECONDS", 0.02)
    DockerClawRuntime._CREATE_OPERATIONS.clear()
    runtime = object.__new__(DockerClawRuntime)
    runtime.settings = _claw_settings()
    instance_name = runtime.planned_id("claw-stale-late")

    info = await runtime.create("claw-stale-late", "secret")

    assert info.instance_name == instance_name
    assert provider.get_count == 1
    assert provider.run_count == 0
    assert provider.created is True
    assert provider.removed is False
    assert await runtime.destroy(instance_name) is True
    DockerClawRuntime._CREATE_OPERATIONS.clear()


async def test_claw_never_replaces_or_deletes_a_different_owner(monkeypatch):
    provider = _FakeDockerProvider()
    provider.created = True
    provider.run_release.set()
    provider.container.attrs["Config"] = {
        "Labels": {
            "ai-manus.kind": "claw",
            "ai-manus.claw_id": "different-claw-id",
        }
    }
    monkeypatch.setattr("docker.from_env", provider.client)
    DockerClawRuntime._CREATE_OPERATIONS.clear()
    runtime = object.__new__(DockerClawRuntime)
    runtime.settings = _claw_settings()
    claw_id = "claw-owned-full-id"
    instance_name = runtime.planned_id(claw_id)

    with pytest.raises(PermissionError, match="another record"):
        await runtime.create(claw_id, "secret")

    assert provider.run_count == 0
    assert provider.removed is False
    assert await runtime.destroy_owned(instance_name, claw_id) is False
    assert provider.removed is False
    DockerClawRuntime._CREATE_OPERATIONS.clear()


async def test_isolated_claw_resolver_never_reuses_stale_ip_when_missing(
    monkeypatch,
):
    provider = _FakeDockerProvider()
    monkeypatch.setattr("docker.from_env", provider.client)
    monkeypatch.setattr(
        "app.infrastructure.external.claw.docker_claw_runtime."
        "require_containerized_runtime_gateway",
        lambda: None,
    )
    runtime = object.__new__(DockerClawRuntime)
    runtime.settings = SimpleNamespace(runtime_network_isolation=True)
    DockerClawRuntime._CREATE_OPERATIONS.clear()

    with pytest.raises(RuntimeError, match="no longer exists"):
        await runtime.resolve_owned("bounded-claw-missing", "claw-owner")

    DockerClawRuntime._CREATE_OPERATIONS.clear()


async def test_shared_claw_readiness_uses_one_monotonic_deadline(monkeypatch):
    calls = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            nonlocal calls
            calls += 1
            await asyncio.Event().wait()

    monkeypatch.setattr(
        readiness_module.httpx,
        "AsyncClient",
        lambda **kwargs: Client(),
    )
    started = asyncio.get_running_loop().time()
    ready = await readiness_module.wait_for_http_health(
        "http://claw",
        total_timeout_seconds=0.025,
        request_timeout_seconds=0.01,
        retry_interval_seconds=0.002,
    )

    assert ready is False
    assert calls >= 1
    assert asyncio.get_running_loop().time() - started < 0.15


async def test_agentbay_delete_timeout_retains_provider_id():
    delete_started = asyncio.Event()

    async def hanging_delete():
        delete_started.set()
        await asyncio.Event().wait()

    session = SimpleNamespace(
        session_id="agentbay-retained",
        delete=hanging_delete,
    )
    sandbox = AgentBaySandbox(
        session,
        "https://gateway.example",
        "wss://gateway.example/cdp",
        "wss://gateway.example/vnc",
    )
    sandbox._PROVIDER_DELETE_TIMEOUT_SECONDS = 0.01

    assert await sandbox.destroy() is False
    assert delete_started.is_set()
    assert sandbox.id == "agentbay-retained"
    assert sandbox.client.is_closed is True


class _ClawFailureRepository:
    def __init__(
        self,
        *,
        block_first_update: bool = False,
        fail_update_count: int = 1,
    ) -> None:
        self.update_calls = 0
        self.block_first_update = block_first_update
        self.fail_update_count = fail_update_count
        self.first_update_started = asyncio.Event()
        self.release_first_update = asyncio.Event()
        self.first_update_cancelled = False

    async def update(self, claw):
        self.update_calls += 1
        if self.block_first_update and self.update_calls == 1:
            self.first_update_started.set()
            try:
                await self.release_first_update.wait()
            except asyncio.CancelledError:
                self.first_update_cancelled = True
                raise
        elif self.update_calls <= self.fail_update_count:
            raise ConnectionError("transient Mongo failure")
        return claw


class _FailedClawRuntime:
    ready_timeout = 1

    def __init__(
        self,
        *,
        block_create: bool = False,
        destroy_results: list[bool] | None = None,
    ) -> None:
        self.block_create = block_create
        self.destroy_results = list(destroy_results or [False])
        self.create_started = asyncio.Event()
        self.destroyed = []

    async def create(self, claw_id, api_key):
        error = RuntimeError("Docker create indeterminate")
        error.claw_instance_name = "bounded-claw-owned"
        if not self.block_create:
            raise error
        self.create_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as cancelled:
            cancelled.claw_instance_name = error.claw_instance_name
            raise

    async def destroy(self, instance_name):
        self.destroyed.append(instance_name)
        if len(self.destroy_results) > 1:
            return self.destroy_results.pop(0)
        return self.destroy_results[0]


def _creating_claw() -> Claw:
    return Claw(
        id="claw-owned",
        user_id="user",
        api_key="runtime-key",
        status=ClawStatus.CREATING,
    )


async def test_claw_failed_create_retries_ownership_persist_when_destroy_false():
    repository = _ClawFailureRepository()
    runtime = _FailedClawRuntime()
    claw = _creating_claw()
    service = ClawDomainService(repository, runtime, SimpleNamespace())

    await service.provision_claw_instance(claw)

    assert repository.update_calls == 2
    assert runtime.destroyed == ["bounded-claw-owned"]
    assert claw.container_name == "bounded-claw-owned"
    assert "ownership was retained" in claw.error_message


async def test_claw_repeated_cancel_does_not_cancel_ownership_persist():
    repository = _ClawFailureRepository(
        block_first_update=True,
        fail_update_count=0,
    )
    runtime = _FailedClawRuntime(block_create=True)
    claw = _creating_claw()
    service = ClawDomainService(repository, runtime, SimpleNamespace())
    task = asyncio.create_task(service.provision_claw_instance(claw))
    await runtime.create_started.wait()

    task.cancel()
    await repository.first_update_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert repository.first_update_cancelled is False
    repository.release_first_update.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert repository.update_calls == 2
    assert repository.first_update_cancelled is False
    assert runtime.destroyed == ["bounded-claw-owned"]
    assert claw.container_name == "bounded-claw-owned"


async def test_passthrough_continuous_persist_failure_retries_exact_destroy(
    monkeypatch,
):
    class Repository:
        def __init__(self):
            self.calls = 0

        async def update_runtime_ownership(self, *args):
            self.calls += 1
            raise ConnectionError("Mongo remains unavailable")

    class Candidate:
        id = "deterministic-sandbox"

        def __init__(self):
            self.destroy_results = [False, True]
            self.destroy_calls = 0

        async def destroy(self):
            result = self.destroy_results[min(self.destroy_calls, 1)]
            self.destroy_calls += 1
            return result

        async def aclose(self):
            return None

    candidate = Candidate()

    class Factory:
        @classmethod
        async def get(cls, sandbox_id):
            assert sandbox_id == candidate.id
            return candidate

        @classmethod
        async def create(cls):
            raise SandboxProvisioningError(
                candidate.id, "indeterminate create"
            )

    repository = Repository()
    provisioner = PassthroughSandboxProvisioner(Factory, repository)
    monkeypatch.setattr(
        provisioner, "_OWNERSHIP_RECONCILE_INTERVAL_SECONDS", 0
    )
    session = Session(
        id="session-continuous-failure",
        user_id="user",
        agent_id="agent",
    )

    with pytest.raises(SandboxProvisioningError, match="indeterminate"):
        await provisioner.ensure_locked(session)

    # Failed/indeterminate generations are never republished through the
    # ordinary, adoptable ownership update path.
    assert repository.calls == 0
    assert candidate.destroy_calls == 2
    assert session.sandbox_id is None
    assert session.sandbox_provider is None


async def test_passthrough_publish_failure_converges_on_exact_destroy(
    monkeypatch,
):
    class Repository:
        def __init__(self):
            self.calls = 0

        async def update_runtime_ownership(self, *args):
            self.calls += 1
            raise ConnectionError("Mongo remains unavailable")

    class Candidate:
        id = "created-sandbox"

        def __init__(self):
            self.destroy_results = [False, True]
            self.destroy_calls = 0

        async def destroy(self):
            result = self.destroy_results[min(self.destroy_calls, 1)]
            self.destroy_calls += 1
            return result

    candidate = Candidate()

    class Factory:
        @classmethod
        async def get(cls, sandbox_id):
            return None

        @classmethod
        async def create(cls):
            return candidate

    repository = Repository()
    provisioner = PassthroughSandboxProvisioner(Factory, repository)
    monkeypatch.setattr(
        provisioner, "_OWNERSHIP_RECONCILE_INTERVAL_SECONDS", 0
    )
    session = Session(
        id="session-publish-failure",
        user_id="user",
        agent_id="agent",
    )

    with pytest.raises(ConnectionError, match="Mongo remains unavailable"):
        await provisioner.ensure_locked(session)

    # The sole ordinary write is the initial publish attempt. Rollback uses
    # cleanup/tombstone state and must not make the generation adoptable.
    assert repository.calls == 1
    assert candidate.destroy_calls == 2
    assert session.sandbox_id is None
    assert session.sandbox_provider is None


async def test_claw_continuous_persist_failure_retries_exact_destroy(
    monkeypatch,
):
    repository = _ClawFailureRepository(fail_update_count=100)
    runtime = _FailedClawRuntime(destroy_results=[False, True])
    claw = _creating_claw()
    service = ClawDomainService(repository, runtime, SimpleNamespace())
    monkeypatch.setattr(
        service, "_OWNERSHIP_RECONCILE_INTERVAL_SECONDS", 0
    )

    await service.provision_claw_instance(claw)

    assert repository.update_calls >= 3
    assert runtime.destroyed == [
        "bounded-claw-owned",
        "bounded-claw-owned",
    ]
    assert claw.container_name is None
    assert claw.container_ip is None


async def test_passthrough_permanent_failure_has_bounded_foreground_reconcile(
    monkeypatch,
):
    class Repository:
        def __init__(self):
            self.calls = 0

        async def update_runtime_ownership(self, *args):
            self.calls += 1
            raise ConnectionError("Mongo remains unavailable")

    class Candidate:
        id = "bounded-reconcile-sandbox"

        def __init__(self):
            self.destroy_calls = 0

        async def destroy(self):
            self.destroy_calls += 1
            return False

        async def aclose(self):
            return None

    candidate = Candidate()

    class Factory:
        @classmethod
        async def get(cls, sandbox_id):
            return candidate

        @classmethod
        async def create(cls):
            raise SandboxProvisioningError(
                candidate.id, "indeterminate create"
            )

    repository = Repository()
    provisioner = PassthroughSandboxProvisioner(Factory, repository)
    monkeypatch.setattr(
        provisioner, "_OWNERSHIP_RECONCILE_INTERVAL_SECONDS", 0
    )
    monkeypatch.setattr(
        provisioner, "_OWNERSHIP_RECONCILE_MAX_ATTEMPTS", 2
    )
    session = Session(
        id="session-bounded-reconcile",
        user_id="user",
        agent_id="agent",
    )

    with pytest.raises(SandboxProvisioningError, match="reconciliation budget"):
        await asyncio.wait_for(provisioner.ensure_locked(session), timeout=0.2)

    assert repository.calls == 0
    assert candidate.destroy_calls == 2


async def test_claw_permanent_failure_defers_after_bounded_reconcile(
    monkeypatch,
):
    repository = _ClawFailureRepository(fail_update_count=100)
    runtime = _FailedClawRuntime(destroy_results=[False])
    claw = _creating_claw()
    service = ClawDomainService(repository, runtime, SimpleNamespace())
    monkeypatch.setattr(
        service, "_OWNERSHIP_RECONCILE_INTERVAL_SECONDS", 0
    )
    monkeypatch.setattr(
        service, "_OWNERSHIP_RECONCILE_MAX_ATTEMPTS", 2
    )

    await asyncio.wait_for(service.provision_claw_instance(claw), timeout=0.2)

    assert repository.update_calls == 3
    assert runtime.destroyed == ["bounded-claw-owned"] * 3
    assert claw.container_name == "bounded-claw-owned"
    assert "deferred" in (claw.error_message or "")


async def test_claw_prepare_prepublishes_deterministic_runtime_name():
    class Repository:
        def __init__(self):
            self.claw = None

        async def get_by_user_id(self, user_id):
            return None

        async def count_by_statuses(self, statuses):
            return 0

        async def create(self, claw):
            self.claw = claw
            return claw

    class Runtime:
        ready_timeout = 1

        @staticmethod
        def planned_id(claw_id):
            return f"planned-{claw_id}"

    repository = Repository()
    service = ClawDomainService(repository, Runtime(), SimpleNamespace())

    claw = await service.prepare_claw_for_creation("user-prepublished")

    assert claw.container_name == f"planned-{claw.id}"
    assert repository.claw.container_name == claw.container_name


async def test_claw_bounded_failure_is_reconciled_from_durable_pointer(
    monkeypatch,
):
    class Repository:
        def __init__(self):
            self.stored = None
            self.fail_updates = False

        async def get_by_user_id(self, user_id):
            return None

        async def count_by_statuses(self, statuses):
            return 0

        async def create(self, claw):
            self.stored = claw.model_copy(deep=True)
            return claw

        async def update(self, claw):
            if self.fail_updates:
                raise ConnectionError("Mongo unavailable")
            self.stored = claw.model_copy(deep=True)
            return claw

        async def list_by_statuses(self, statuses):
            return [self.stored.model_copy(deep=True)]

    class Runtime:
        ready_timeout = 1

        def __init__(self):
            self.allow_destroy = False
            self.destroy_calls = 0

        @staticmethod
        def planned_id(claw_id):
            return f"planned-{claw_id}"

        async def create(self, claw_id, api_key):
            raise RuntimeError("indeterminate Docker create")

        async def destroy(self, instance_name):
            self.destroy_calls += 1
            return self.allow_destroy

    repository = Repository()
    runtime = Runtime()
    service = ClawDomainService(repository, runtime, SimpleNamespace())
    monkeypatch.setattr(
        service, "_OWNERSHIP_RECONCILE_INTERVAL_SECONDS", 0
    )
    monkeypatch.setattr(
        service, "_OWNERSHIP_RECONCILE_MAX_ATTEMPTS", 2
    )
    claw = await service.prepare_claw_for_creation("durable-user")
    durable_name = claw.container_name

    repository.fail_updates = True
    await asyncio.wait_for(service.provision_claw_instance(claw), timeout=0.2)

    assert repository.stored.status == ClawStatus.CREATING
    assert repository.stored.container_name == durable_name

    repository.fail_updates = False
    repository.stored.updated_at = datetime.now(UTC) - timedelta(minutes=5)
    runtime.allow_destroy = True
    result = await service.cleanup_instances()

    assert result == {"removed": 0, "errored": 1}
    assert repository.stored.status == ClawStatus.ERROR
    assert repository.stored.container_name is None
    assert runtime.destroy_calls == 4
