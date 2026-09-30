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
from app.domain.models.session import Session
from app.infrastructure.external import docker_async as docker_async_module
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
