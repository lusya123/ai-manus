import asyncio
from types import SimpleNamespace

import pytest

from app.application.services.agent_service import AgentService
from app.core.config import get_settings
from app.domain.services import agent_task_runner as runner_module
from app.domain.services.agent_task_runner import (
    AgentTaskRunner,
    AgentTaskRunnerFactory,
    RunnerCleanupCapacityError,
)
from app.infrastructure.external.sandbox.agentbay_sandbox import AgentBaySandbox
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.domain.external.sandbox_provisioner import (
    SandboxProvisioningRequiredError,
)


async def test_fixed_sandbox_handles_are_not_shared_or_cross_closed(monkeypatch):
    """Deleting one session must not close another fixed-host client."""
    monkeypatch.setenv("SANDBOX_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("API_KEY", "test")
    get_settings.cache_clear()
    DockerSandbox._resolve_hostname_to_ip.cache_clear()
    try:
        first = await DockerSandbox.get("dev-sandbox")
        second = await DockerSandbox.get("dev-sandbox")

        assert first is not second
        assert first.client is not second.client
        assert first._managed_container is False
        assert second._managed_container is False

        assert await first.destroy() is True
        assert first.client.is_closed is True
        assert second.client.is_closed is False
        assert await second.destroy() is True
    finally:
        get_settings.cache_clear()
        DockerSandbox._resolve_hostname_to_ip.cache_clear()


async def test_fixed_sandbox_rejects_noncanonical_persisted_id(monkeypatch):
    monkeypatch.setenv("SANDBOX_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("API_KEY", "test")
    get_settings.cache_clear()
    DockerSandbox._resolve_hostname_to_ip.cache_clear()
    try:
        assert await DockerSandbox.get("agentbay-or-stale-container-id") is None
    finally:
        get_settings.cache_clear()
        DockerSandbox._resolve_hostname_to_ip.cache_clear()


async def test_docker_aclose_never_removes_managed_container(monkeypatch):
    sandbox = DockerSandbox(
        ip="127.0.0.1",
        container_name="persisted-sandbox",
        managed_container=True,
    )
    docker_touched = False

    def fail_if_docker_is_touched():
        nonlocal docker_touched
        docker_touched = True
        raise AssertionError("aclose must not touch the Docker provider")

    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.docker_sandbox.docker.from_env",
        fail_if_docker_is_touched,
    )

    await sandbox.aclose()
    await sandbox.aclose()

    assert sandbox.client.is_closed is True
    assert docker_touched is False


async def test_dynamic_docker_sandbox_applies_configured_resource_limits(
    monkeypatch,
):
    captured = {}

    class Container:
        attrs = {"NetworkSettings": {"IPAddress": "172.20.0.9"}}

        def reload(self):
            return None

    class Containers:
        def run(self, **kwargs):
            captured.update(kwargs)
            return Container()

    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.docker_sandbox.docker.from_env",
        lambda **kwargs: SimpleNamespace(containers=Containers()),
    )
    monkeypatch.setenv("SANDBOX_ADDRESS", "")
    monkeypatch.setenv("SANDBOX_IMAGE", "example/sandbox:tested")
    monkeypatch.setenv("SANDBOX_NAME_PREFIX", "bounded")
    monkeypatch.setenv("SANDBOX_NETWORK", "manus-network")
    monkeypatch.setenv("RUNTIME_NETWORK_ISOLATION", "true")
    monkeypatch.setenv("SANDBOX_MEMORY_LIMIT", "768m")
    monkeypatch.setenv("SANDBOX_CPU_LIMIT", "1.5")
    monkeypatch.setenv("SANDBOX_PIDS_LIMIT", "123")
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
    get_settings.cache_clear()
    sandbox = None
    try:
        sandbox = await DockerSandbox._create_named("bounded-focused-test")

        assert captured["image"] == "example/sandbox:tested"
        assert captured["network"] == "test-egress-network"
        assert captured["mem_limit"] == "768m"
        assert captured["nano_cpus"] == 1_500_000_000
        assert captured["pids_limit"] == 123
        assert sandbox._managed_container is True
        assert sandbox.ip == "172.20.0.9"
    finally:
        if sandbox is not None:
            await sandbox.aclose()
        get_settings.cache_clear()


async def test_agentbay_aclose_never_deletes_cloud_session():
    class Session:
        session_id = "persisted-agentbay-session"

        def __init__(self):
            self.delete_count = 0

        async def delete(self):
            self.delete_count += 1
            return SimpleNamespace(success=True)

    session = Session()
    sandbox = AgentBaySandbox(
        session,
        "https://gateway.example",
        "wss://gateway.example/cdp",
        "wss://gateway.example/vnc",
    )

    await sandbox.aclose()
    await sandbox.aclose()

    assert sandbox.client.is_closed is True
    assert session.delete_count == 0


class _CloseTracker:
    def __init__(self):
        self.close_count = 0

    async def cleanup(self):
        self.close_count += 1


class _LLMCloseTracker:
    def __init__(self):
        self.close_count = 0

    async def aclose(self):
        self.close_count += 1


class _SandboxCloseTracker:
    id = "sandbox-1"

    def __init__(self):
        self.close_count = 0
        self.destroy_count = 0

    async def aclose(self):
        self.close_count += 1

    async def destroy(self):
        self.destroy_count += 1
        return True


class _FactorySessionRepository:
    async def find_by_id_and_user_id(self, session_id, user_id):
        return SimpleNamespace(
            id=session_id,
            user_id=user_id,
            agent_id="agent-1",
            sandbox_id="sandbox-1",
            sandbox_provider="docker",
            deleting=False,
            sandbox_destroying=False,
            task_id="task-1",
            task_sandbox_id="sandbox-1",
        )


def _bare_runner():
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._browser = _CloseTracker()
    runner._mcp_tool = _CloseTracker()
    runner._llm = _LLMCloseTracker()
    runner._sandbox = _SandboxCloseTracker()
    runner._close_lock = asyncio.Lock()
    runner._closed = False
    return runner


async def test_runner_aclose_is_idempotent_and_non_destructive():
    runner = _bare_runner()

    await asyncio.gather(runner.aclose(), runner.aclose())
    await runner.aclose()

    assert runner._browser.close_count == 1
    assert runner._mcp_tool.close_count == 1
    assert runner._llm.close_count == 1
    assert runner._sandbox.close_count == 1
    assert runner._sandbox.destroy_count == 0


async def test_runner_destroy_is_compatibility_close_and_never_deletes_provider():
    runner = _bare_runner()

    await runner.destroy()

    assert runner._sandbox.destroy_count == 0
    assert runner._sandbox.close_count == 1
    assert runner._browser.close_count == 1
    assert runner._mcp_tool.close_count == 1
    assert runner._llm.close_count == 1


async def test_runner_factory_failure_closes_partial_handles_without_destroy(
    monkeypatch,
):
    sandbox = _SandboxCloseTracker()
    browser = _CloseTracker()
    llm = _LLMCloseTracker()

    async def get_browser():
        return browser

    sandbox.get_browser = get_browser

    class SandboxClass:
        @classmethod
        async def get(cls, sandbox_id):
            return sandbox

    class AgentRepository:
        async def find_by_id(self, agent_id):
            return SimpleNamespace(id=agent_id)

    class LLMFactory:
        def create(self, agent):
            return llm

    factory = AgentTaskRunnerFactory(
        agent_repository=AgentRepository(),
        session_repository=_FactorySessionRepository(),
        sandbox_cls=SandboxClass,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        llm_factory=LLMFactory(),
    )
    monkeypatch.setattr(
        "app.domain.services.agent_task_runner.AgentTaskRunner",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("constructor failed")),
    )

    with pytest.raises(RuntimeError, match="constructor failed"):
        await factory.create_runner(
            {
                "session_id": "session-1",
                "agent_id": "agent-1",
                "user_id": "owner",
                "sandbox_id": "sandbox-1",
                "task_id": "task-1",
                "task_sandbox_id": "sandbox-1",
            }
        )

    assert browser.close_count == 1
    assert llm.close_count == 1
    assert sandbox.close_count == 1
    assert sandbox.destroy_count == 0
    assert runner_module._runner_cleanup_leases_for_loop() == set()


async def test_runner_factory_backpressures_before_constructing_live_handles(
    monkeypatch,
):
    monkeypatch.setattr(
        runner_module,
        "_MAX_RUNNER_CLEANUP_BUNDLES",
        2,
    )
    sandboxes = []
    browsers = []
    llms = []

    class Sandbox(_SandboxCloseTracker):
        async def get_browser(self):
            browser = _CloseTracker()
            browsers.append(browser)
            return browser

    class SandboxClass:
        get_count = 0

        @classmethod
        async def get(cls, sandbox_id):
            cls.get_count += 1
            sandbox = Sandbox()
            sandboxes.append(sandbox)
            return sandbox

    class AgentRepository:
        find_count = 0

        async def find_by_id(self, agent_id):
            self.find_count += 1
            return SimpleNamespace(id=agent_id)

    class LLMFactory:
        create_count = 0

        def create(self, agent):
            self.create_count += 1
            llm = _LLMCloseTracker()
            llms.append(llm)
            return llm

    agent_repository = AgentRepository()
    llm_factory = LLMFactory()
    factory = AgentTaskRunnerFactory(
        agent_repository=agent_repository,
        session_repository=_FactorySessionRepository(),
        sandbox_cls=SandboxClass,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        llm_factory=llm_factory,
    )
    params = {
        "session_id": "session-1",
        "agent_id": "agent-1",
        "user_id": "owner",
        "sandbox_id": "sandbox-1",
        "task_id": "task-1",
        "task_sandbox_id": "sandbox-1",
    }

    first = await factory.create_runner(params)
    second = await factory.create_runner(params)

    with pytest.raises(RunnerCleanupCapacityError) as raised:
        await factory.create_runner(params)

    assert raised.value.transient is True
    assert raised.value.retryable is True
    assert SandboxClass.get_count == 2
    assert agent_repository.find_count == 2
    assert llm_factory.create_count == 2
    assert len(runner_module._runner_cleanup_leases_for_loop()) == 2

    await first.aclose()
    assert len(runner_module._runner_cleanup_leases_for_loop()) == 1

    third = await factory.create_runner(params)
    assert SandboxClass.get_count == 3
    assert agent_repository.find_count == 3
    assert llm_factory.create_count == 3

    await asyncio.gather(second.aclose(), third.aclose())

    assert runner_module._runner_cleanup_leases_for_loop() == set()
    assert all(browser.close_count == 1 for browser in browsers)
    assert all(llm.close_count == 1 for llm in llms)
    assert all(sandbox.close_count == 1 for sandbox in sandboxes)
    assert all(sandbox.destroy_count == 0 for sandbox in sandboxes)


async def test_runner_factory_never_allocates_an_exact_missing_sandbox():
    class SandboxClass:
        create_count = 0

        @classmethod
        async def get(cls, sandbox_id):
            return None

        @classmethod
        async def create(cls):
            cls.create_count += 1
            raise AssertionError("workers must never allocate providers")

    factory = AgentTaskRunnerFactory(
        agent_repository=SimpleNamespace(),
        session_repository=_FactorySessionRepository(),
        sandbox_cls=SandboxClass,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        llm=SimpleNamespace(),
    )

    with pytest.raises(SandboxProvisioningRequiredError):
        await factory.create_runner(
            {
                "session_id": "session-1",
                "agent_id": "agent-1",
                "user_id": "owner",
                "sandbox_id": "sandbox-1",
                "task_id": "task-1",
                "task_sandbox_id": "sandbox-1",
            }
        )

    assert SandboxClass.create_count == 0


async def test_runner_factory_prefers_owner_aware_sandbox_lookup():
    class SandboxClass:
        owned_calls = []
        get_count = 0

        @classmethod
        async def get_owned(cls, sandbox_id, owner_id):
            cls.owned_calls.append((sandbox_id, owner_id))
            return None

        @classmethod
        async def get(cls, sandbox_id):
            cls.get_count += 1
            raise AssertionError("owner-aware providers must not use get()")

    factory = AgentTaskRunnerFactory(
        agent_repository=SimpleNamespace(),
        session_repository=_FactorySessionRepository(),
        sandbox_cls=SandboxClass,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        llm=SimpleNamespace(),
    )

    with pytest.raises(SandboxProvisioningRequiredError):
        await factory.create_runner(
            {
                "session_id": "session-1",
                "agent_id": "agent-1",
                "user_id": "owner",
                "sandbox_id": "sandbox-1",
                "task_id": "task-1",
                "task_sandbox_id": "sandbox-1",
            }
        )

    assert SandboxClass.owned_calls == [("sandbox-1", "session-1")]
    assert SandboxClass.get_count == 0


async def test_runner_factory_rejects_mismatched_runtime_handle_id():
    sandbox = _SandboxCloseTracker()
    sandbox.id = "different-sandbox"

    class SandboxClass:
        @classmethod
        async def get(cls, sandbox_id):
            return sandbox

    factory = AgentTaskRunnerFactory(
        agent_repository=SimpleNamespace(),
        session_repository=_FactorySessionRepository(),
        sandbox_cls=SandboxClass,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        llm=SimpleNamespace(),
    )

    with pytest.raises(
        SandboxProvisioningRequiredError,
        match="different persisted runtime",
    ):
        await factory.create_runner(
            {
                "session_id": "session-1",
                "agent_id": "agent-1",
                "user_id": "owner",
                "sandbox_id": "sandbox-1",
                "task_id": "task-1",
                "task_sandbox_id": "sandbox-1",
            }
        )

    assert sandbox.close_count == 1
    assert sandbox.destroy_count == 0
    assert runner_module._runner_cleanup_leases_for_loop() == set()


async def test_runner_factory_falls_back_for_provider_without_get_owned():
    class LegacySandboxClass:
        get_calls = []

        @classmethod
        async def get(cls, sandbox_id):
            cls.get_calls.append(sandbox_id)
            return None

    factory = AgentTaskRunnerFactory(
        agent_repository=SimpleNamespace(),
        session_repository=_FactorySessionRepository(),
        sandbox_cls=LegacySandboxClass,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        llm=SimpleNamespace(),
    )

    with pytest.raises(SandboxProvisioningRequiredError):
        await factory.create_runner(
            {
                "session_id": "session-1",
                "agent_id": "agent-1",
                "user_id": "owner",
                "sandbox_id": "sandbox-1",
                "task_id": "task-1",
                "task_sandbox_id": "sandbox-1",
            }
        )

    assert LegacySandboxClass.get_calls == ["sandbox-1"]


async def test_runner_factory_does_not_fallback_after_owned_lookup_error():
    class SandboxClass:
        get_count = 0

        @classmethod
        async def get_owned(cls, sandbox_id, owner_id):
            raise RuntimeError("owned lookup is inconclusive")

        @classmethod
        async def get(cls, sandbox_id):
            cls.get_count += 1
            return None

    factory = AgentTaskRunnerFactory(
        agent_repository=SimpleNamespace(),
        session_repository=_FactorySessionRepository(),
        sandbox_cls=SandboxClass,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        llm=SimpleNamespace(),
    )

    with pytest.raises(RuntimeError, match="owned lookup is inconclusive"):
        await factory.create_runner(
            {
                "session_id": "session-1",
                "agent_id": "agent-1",
                "user_id": "owner",
                "sandbox_id": "sandbox-1",
                "task_id": "task-1",
                "task_sandbox_id": "sandbox-1",
            }
        )

    assert SandboxClass.get_count == 0


@pytest.mark.parametrize(
    "session_overrides",
    [
        {"deleting": True},
        {"sandbox_destroying": True},
        {"task_id": "replacement-task"},
        {"task_sandbox_id": "previous-sandbox"},
        {"sandbox_id": "replacement-sandbox"},
    ],
)
async def test_runner_factory_fences_delete_task_and_sandbox_generation(
    monkeypatch,
    session_overrides,
):
    class SessionRepository:
        async def find_by_id_and_user_id(self, session_id, user_id):
            values = {
                "id": session_id,
                "user_id": user_id,
                "agent_id": "agent-1",
                "sandbox_id": "sandbox-1",
                "sandbox_provider": "docker",
                "deleting": False,
                "sandbox_destroying": False,
                "task_id": "task-1",
                "task_sandbox_id": "sandbox-1",
            }
            values.update(session_overrides)
            return SimpleNamespace(**values)

    class SandboxClass:
        get_count = 0

        @classmethod
        async def get(cls, sandbox_id):
            cls.get_count += 1
            raise AssertionError("runtime lookup must be fenced first")

    monkeypatch.setenv("SANDBOX_PROVIDER", "docker")
    get_settings.cache_clear()
    try:
        factory = AgentTaskRunnerFactory(
            agent_repository=SimpleNamespace(),
            session_repository=SessionRepository(),
            sandbox_cls=SandboxClass,
            file_storage=SimpleNamespace(),
            mcp_repository=SimpleNamespace(),
            llm=SimpleNamespace(),
        )
        with pytest.raises(
            SandboxProvisioningRequiredError,
            match="ownership does not match",
        ):
            await factory.create_runner(
                {
                    "session_id": "session-1",
                    "agent_id": "agent-1",
                    "user_id": "owner",
                    "sandbox_id": "sandbox-1",
                    "sandbox_provider": "docker",
                    "task_id": "task-1",
                    "task_sandbox_id": "sandbox-1",
                }
            )
        assert SandboxClass.get_count == 0
    finally:
        get_settings.cache_clear()


async def test_runner_factory_rejects_cross_provider_session_before_lookup(
    monkeypatch,
):
    class SessionRepository:
        async def find_by_id_and_user_id(self, session_id, user_id):
            return SimpleNamespace(
                agent_id="agent-1",
                sandbox_id="agentbay-session",
                sandbox_provider="agentbay",
                deleting=False,
                sandbox_destroying=False,
                task_id="task-1",
                task_sandbox_id="agentbay-session",
            )

    class SandboxClass:
        get_count = 0

        @classmethod
        async def get(cls, sandbox_id):
            cls.get_count += 1
            raise AssertionError("provider lookup must be fenced first")

    monkeypatch.setenv("SANDBOX_PROVIDER", "docker")
    get_settings.cache_clear()
    try:
        factory = AgentTaskRunnerFactory(
            agent_repository=SimpleNamespace(),
            session_repository=SessionRepository(),
            sandbox_cls=SandboxClass,
            file_storage=SimpleNamespace(),
            mcp_repository=SimpleNamespace(),
            llm=SimpleNamespace(),
        )
        with pytest.raises(
            SandboxProvisioningRequiredError,
            match="ownership does not match",
        ):
            await factory.create_runner(
                {
                    "session_id": "session-1",
                    "agent_id": "agent-1",
                    "user_id": "owner",
                    "sandbox_id": "agentbay-session",
                    "sandbox_provider": "agentbay",
                    "task_id": "task-1",
                    "task_sandbox_id": "agentbay-session",
                }
            )
        assert SandboxClass.get_count == 0
    finally:
        get_settings.cache_clear()


async def test_short_lived_agent_service_handles_close_on_success_without_destroy():
    handles = []

    class Handle:
        vnc_url = "ws://sandbox.example/vnc"
        base_url = "http://sandbox.example:8080"

        def __init__(self):
            self.close_count = 0
            self.destroy_count = 0

        async def aclose(self):
            self.close_count += 1

        async def destroy(self):
            self.destroy_count += 1

        async def view_shell(self, session_id, console=False):
            return SimpleNamespace(
                success=True,
                data={"output": "ok", "session_id": session_id, "console": []},
            )

        async def file_read(self, file_path):
            return SimpleNamespace(
                success=True,
                data={"content": "contents", "file": file_path},
            )

    class SandboxClass:
        @classmethod
        async def get(cls, sandbox_id):
            handle = Handle()
            handles.append(handle)
            return handle

    class Repository:
        async def find_by_id(self, session_id):
            return SimpleNamespace(sandbox_id="sandbox-1")

        async def find_by_id_and_user_id(self, session_id, user_id):
            return SimpleNamespace(sandbox_id="sandbox-1")

    service = object.__new__(AgentService)
    service._session_repository = Repository()
    service._sandbox_cls = SandboxClass

    shell = await service.shell_view("session-1", "shell-1", "owner")
    vnc_url = await service.get_vnc_url("session-1")
    preview_url = await service.get_preview_proxy_base_url("session-1")
    file_view = await service.file_view("session-1", "/tmp/file", "owner")

    assert shell.output == "ok"
    assert vnc_url == "ws://sandbox.example/vnc"
    assert preview_url == "http://sandbox.example:8080"
    assert file_view.content == "contents"
    assert len(handles) == 4
    assert all(handle.close_count == 1 for handle in handles)
    assert all(handle.destroy_count == 0 for handle in handles)


async def test_short_lived_agent_service_handle_closes_on_route_error():
    class Handle:
        def __init__(self):
            self.close_count = 0
            self.destroy_count = 0

        async def aclose(self):
            self.close_count += 1

        async def file_read(self, file_path):
            raise RuntimeError("sandbox request failed")

    handle = Handle()

    class SandboxClass:
        @classmethod
        async def get(cls, sandbox_id):
            return handle

    class Repository:
        async def find_by_id_and_user_id(self, session_id, user_id):
            return SimpleNamespace(sandbox_id="sandbox-1")

    service = object.__new__(AgentService)
    service._session_repository = Repository()
    service._sandbox_cls = SandboxClass

    with pytest.raises(RuntimeError, match="sandbox request failed"):
        await service.file_view("session-1", "/tmp/file", "owner")

    assert handle.close_count == 1
    assert handle.destroy_count == 0


async def test_agent_service_rejects_cross_provider_handle_before_lookup(
    monkeypatch,
):
    class SandboxClass:
        get_count = 0

        @classmethod
        async def get(cls, sandbox_id):
            cls.get_count += 1
            return None

    class Repository:
        async def find_by_id_and_user_id(self, session_id, user_id):
            return SimpleNamespace(
                sandbox_id="agentbay-session",
                sandbox_provider="agentbay",
            )

    monkeypatch.setenv("SANDBOX_PROVIDER", "docker")
    get_settings.cache_clear()
    try:
        service = object.__new__(AgentService)
        service._session_repository = Repository()
        service._sandbox_cls = SandboxClass
        with pytest.raises(RuntimeError, match="different provider"):
            await service.file_view("session-1", "/tmp/file", "owner")
        assert SandboxClass.get_count == 0
    finally:
        get_settings.cache_clear()
