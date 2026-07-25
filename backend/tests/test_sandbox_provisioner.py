import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.config import get_settings
from app.domain.external.sandbox import (
    SandboxProvisioningError,
    SandboxShellCleanupUnsupportedError,
    SandboxShellProcessScope,
    SandboxUnavailableError,
)
from app.domain.external.coordination import mark_lifecycle_task_lease_lost
from app.domain.models.session import Session
from app.domain.models.tool_result import ToolResult
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.infrastructure.external.sandbox.passthrough_provisioner import (
    PassthroughSandboxProvisioner,
)


class SessionRepository:
    def __init__(self):
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


class Sandbox:
    def __init__(self, sandbox_id="sandbox-new"):
        self.id = sandbox_id
        self.aclose = AsyncMock()
        self.destroy = AsyncMock(return_value=True)


class SandboxFactory:
    existing = None
    created = None
    get = AsyncMock()
    create = AsyncMock()


@pytest.fixture(autouse=True)
def reset_factory():
    SandboxFactory.get = AsyncMock(return_value=None)
    SandboxFactory.create = AsyncMock(return_value=Sandbox())


async def test_passthrough_reuses_persisted_sandbox():
    repository = SessionRepository()
    existing = Sandbox("sandbox-existing")
    SandboxFactory.get.return_value = existing
    provisioner = PassthroughSandboxProvisioner(SandboxFactory, repository)
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="sandbox-existing",
        sandbox_provider="docker",
    )

    result = await provisioner.ensure_locked(session)

    assert result is existing
    SandboxFactory.create.assert_not_awaited()
    assert repository.updates == []


async def test_passthrough_persists_new_id_before_returning():
    repository = SessionRepository()
    provisioner = PassthroughSandboxProvisioner(SandboxFactory, repository)
    session = Session(
        id="session-1", user_id="user-1", agent_id="agent-1"
    )

    sandbox = await provisioner.ensure_locked(session)

    assert session.sandbox_id == sandbox.id
    assert session.sandbox_provider == "docker"
    assert repository.updates == [
        ("session-1", sandbox.id, None, "docker")
    ]


async def test_fixed_docker_sandbox_provisioning_uses_only_dev_sandbox(
    monkeypatch,
):
    monkeypatch.setenv("SANDBOX_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("API_KEY", "test")
    get_settings.cache_clear()
    DockerSandbox._resolve_hostname_to_ip.cache_clear()
    create = AsyncMock(wraps=DockerSandbox.create)
    dynamic_create = AsyncMock(
        side_effect=AssertionError(
            "fixed sandbox provisioning must not dynamically create Docker"
        )
    )
    monkeypatch.setattr(DockerSandbox, "create", create)
    monkeypatch.setattr(DockerSandbox, "_create_named", dynamic_create)
    monkeypatch.setattr(
        "app.infrastructure.external.sandbox.docker_sandbox.docker.from_env",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("fixed sandbox provisioning must not touch Docker")
        ),
    )

    sandbox = None
    try:
        repository = SessionRepository()
        provisioner = PassthroughSandboxProvisioner(
            DockerSandbox,
            repository,
        )
        session = Session(
            id="fixed-session",
            user_id="user-1",
            agent_id="agent-1",
        )

        assert DockerSandbox.plan_owned_id(session.id) == "dev-sandbox"
        sandbox = await provisioner.ensure_locked(session)

        assert sandbox.id == "dev-sandbox"
        assert sandbox.ip == "127.0.0.1"
        assert session.sandbox_id == "dev-sandbox"
        assert session.sandbox_provider == "docker"
        assert repository.updates == [
            ("fixed-session", "dev-sandbox", None, "docker")
        ]
        create.assert_awaited_once_with()
        dynamic_create.assert_not_awaited()

        with pytest.raises(
            SandboxProvisioningError,
            match="only accepts the dev-sandbox identity",
        ):
            await DockerSandbox.create_owned_with_id(
                session.id,
                "dev-sandbox-unexpected-generation",
            )
        create.assert_awaited_once_with()
        dynamic_create.assert_not_awaited()
    finally:
        if sandbox is not None:
            await sandbox.aclose()
        get_settings.cache_clear()
        DockerSandbox._resolve_hostname_to_ip.cache_clear()


async def test_passthrough_create_failure_clears_pointer_after_exact_not_found():
    repository = SessionRepository()
    provisioner = PassthroughSandboxProvisioner(SandboxFactory, repository)
    session = Session(
        id="session-1", user_id="user-1", agent_id="agent-1"
    )
    SandboxFactory.create.side_effect = SandboxProvisioningError(
        "orphan-candidate",
        "rollback not confirmed",
    )

    with pytest.raises(SandboxProvisioningError, match="rollback not confirmed"):
        await provisioner.ensure_locked(session)

    assert session.sandbox_id is None
    assert session.sandbox_provider is None
    assert repository.updates == []


@pytest.mark.parametrize(
    ("scope", "expected_cleaned", "expected_calls"),
    [
        (SandboxShellProcessScope.EXCLUSIVE, True, 1),
        (SandboxShellProcessScope.SHARED, False, 0),
    ],
)
async def test_shell_cleanup_only_runs_for_owner_verified_exclusive_sandbox(
    scope,
    expected_cleaned,
    expected_calls,
):
    cleanup = AsyncMock(return_value=ToolResult(success=True))
    handle = SimpleNamespace(
        shell_process_scope=scope,
        kill_all_shell_processes=cleanup,
        aclose=AsyncMock(),
    )

    class Factory:
        @staticmethod
        def is_owned_id(sandbox_id, owner_id):
            return (sandbox_id, owner_id) == ("owned-runtime", "session-1")

        @staticmethod
        async def get_owned(sandbox_id, owner_id):
            assert (sandbox_id, owner_id) == (
                "owned-runtime",
                "session-1",
            )
            return handle

    provisioner = PassthroughSandboxProvisioner(
        Factory,
        SessionRepository(),
    )
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="owned-runtime",
        sandbox_provider="docker",
    )

    assert (
        await provisioner.terminate_shell_processes_locked(session)
        is expected_cleaned
    )
    assert cleanup.await_count == expected_calls
    handle.aclose.assert_awaited_once()


async def test_shell_cleanup_fails_closed_for_unverified_process_scope():
    handle = SimpleNamespace(
        shell_process_scope=SandboxShellProcessScope.UNKNOWN,
        kill_all_shell_processes=AsyncMock(
            side_effect=AssertionError("cleanup must not run")
        ),
        aclose=AsyncMock(),
    )

    class Factory:
        @staticmethod
        def is_owned_id(_sandbox_id, _owner_id):
            return True

        @staticmethod
        async def get_owned(_sandbox_id, _owner_id):
            return handle

    provisioner = PassthroughSandboxProvisioner(
        Factory,
        SessionRepository(),
    )
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="runtime",
        sandbox_provider="docker",
    )

    with pytest.raises(
        SandboxProvisioningError,
        match="ownership could not be verified",
    ):
        await provisioner.terminate_shell_processes_locked(session)

    handle.kill_all_shell_processes.assert_not_awaited()
    handle.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    "sandbox",
    [
        DockerSandbox(
            ip="127.0.0.1",
            container_name="dev-sandbox",
            managed_container=False,
        ),
        DockerSandbox(
            ip="127.0.0.1",
            container_name="unverified-runtime",
            managed_container=True,
        ),
    ],
)
async def test_sandbox_handle_itself_refuses_broad_cleanup_without_owner(
    sandbox,
):
    sandbox.client.post = AsyncMock(
        side_effect=AssertionError("sandbox API must not be called")
    )
    try:
        with pytest.raises(PermissionError, match="exclusive"):
            await sandbox.kill_all_shell_processes()
        sandbox.client.post.assert_not_awaited()
    finally:
        await sandbox.aclose()


async def test_owner_verified_handle_maps_kill_all_http_failure_to_retryable():
    sandbox = DockerSandbox(
        ip="127.0.0.1",
        container_name="owned-runtime",
        managed_container=True,
        owner_id="session-1",
    )
    request = httpx.Request(
        "POST",
        "http://127.0.0.1:8080/api/v1/shell/kill-all",
    )
    sandbox.client.post = AsyncMock(
        return_value=httpx.Response(500, request=request)
    )
    try:
        with pytest.raises(
            SandboxUnavailableError,
            match=r"HTTP 500",
        ):
            await sandbox.kill_all_shell_processes()
        sandbox.client.post.assert_awaited_once_with(
            "http://127.0.0.1:8080/api/v1/shell/kill-all",
            timeout=15.0,
        )
    finally:
        await sandbox.aclose()


@pytest.mark.parametrize("status_code", [404, 405, 501])
async def test_old_runtime_reports_scoped_cleanup_as_unsupported(
    status_code,
):
    sandbox = DockerSandbox(
        ip="127.0.0.1",
        container_name="owned-runtime",
        managed_container=True,
        owner_id="session-1",
    )
    request = httpx.Request(
        "POST",
        "http://127.0.0.1:8080/api/v1/shell/kill-all",
    )
    sandbox.client.post = AsyncMock(
        return_value=httpx.Response(status_code, request=request)
    )
    try:
        with pytest.raises(
            SandboxShellCleanupUnsupportedError
        ) as exc_info:
            await sandbox.kill_all_shell_processes()
        assert exc_info.value.status_code == status_code
        sandbox.client.post.assert_awaited_once_with(
            "http://127.0.0.1:8080/api/v1/shell/kill-all",
            timeout=15.0,
        )
    finally:
        await sandbox.aclose()


async def test_old_private_runtime_is_deleted_exactly_without_clearing_task():
    repository = SessionRepository()
    cleanup = AsyncMock(
        side_effect=SandboxShellCleanupUnsupportedError(404)
    )
    handle = SimpleNamespace(
        shell_process_scope=SandboxShellProcessScope.EXCLUSIVE,
        kill_all_shell_processes=cleanup,
        aclose=AsyncMock(),
    )

    class Factory:
        destroy_owned_by_id = AsyncMock(return_value=True)

        @staticmethod
        def is_owned_id(sandbox_id, owner_id):
            return (sandbox_id, owner_id) == (
                "owned-runtime",
                "session-1",
            )

        @staticmethod
        async def get_owned(sandbox_id, owner_id):
            assert (sandbox_id, owner_id) == (
                "owned-runtime",
                "session-1",
            )
            return handle

    provisioner = PassthroughSandboxProvisioner(Factory, repository)
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="owned-runtime",
        sandbox_provider="docker",
        task_id="task-1",
        task_sandbox_id="owned-runtime",
    )

    assert await provisioner.terminate_shell_processes_locked(session) is True

    cleanup.assert_awaited_once()
    Factory.destroy_owned_by_id.assert_awaited_once_with(
        "owned-runtime",
        "session-1",
    )
    handle.aclose.assert_awaited_once()
    assert session.sandbox_id is None
    assert session.sandbox_provider is None
    assert session.task_id == "task-1"
    assert session.task_sandbox_id == "owned-runtime"
    assert repository.updates[-1] == (
        "session-1",
        None,
        "task-1",
        None,
    )


async def test_old_private_runtime_failed_delete_resumes_from_durable_claim():
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="owned-runtime",
        sandbox_provider="docker",
        task_id="task-1",
        task_sandbox_id="owned-runtime",
    )

    class Repository:
        def __init__(self):
            self.current = session.model_copy(deep=True)

        async def claim_runtime_destroy(
            self,
            session_id,
            sandbox_id,
            task_id,
            task_sandbox_id,
            sandbox_provider,
        ):
            current = self.current
            if (
                current.id != session_id
                or current.sandbox_id != sandbox_id
                or current.task_id != task_id
                or current.task_sandbox_id != task_sandbox_id
                or current.sandbox_provider != sandbox_provider
                or current.sandbox_destroying
            ):
                return False
            current.sandbox_destroying = True
            return True

        async def finish_runtime_destroy(
            self,
            session_id,
            expected_sandbox_id,
            expected_task_id,
            expected_task_sandbox_id,
            expected_sandbox_provider,
            sandbox_id,
            task_id,
            sandbox_provider,
            task_sandbox_id,
        ):
            current = self.current
            if (
                current.id != session_id
                or current.sandbox_id != expected_sandbox_id
                or current.task_id != expected_task_id
                or current.task_sandbox_id != expected_task_sandbox_id
                or current.sandbox_provider != expected_sandbox_provider
                or not current.sandbox_destroying
            ):
                return False
            current.sandbox_id = sandbox_id
            current.task_id = task_id
            current.task_sandbox_id = task_sandbox_id
            current.sandbox_provider = sandbox_provider
            current.sandbox_destroying = False
            return True

        async def find_by_id(self, _session_id):
            return self.current.model_copy(deep=True)

    cleanup = AsyncMock(
        side_effect=SandboxShellCleanupUnsupportedError(404)
    )
    handle = SimpleNamespace(
        shell_process_scope=SandboxShellProcessScope.EXCLUSIVE,
        kill_all_shell_processes=cleanup,
        aclose=AsyncMock(),
    )

    class Factory:
        destroy_owned_by_id = AsyncMock(side_effect=[False, True])

        @staticmethod
        def is_owned_id(sandbox_id, owner_id):
            return (sandbox_id, owner_id) == (
                "owned-runtime",
                "session-1",
            )

        @staticmethod
        async def get_owned(_sandbox_id, _owner_id):
            return handle

    repository = Repository()
    provisioner = PassthroughSandboxProvisioner(Factory, repository)

    with pytest.raises(
        SandboxUnavailableError,
        match="did not confirm deletion",
    ):
        await provisioner.terminate_shell_processes_locked(session)

    assert session.sandbox_destroying is True
    assert session.sandbox_id == "owned-runtime"
    assert repository.current.sandbox_destroying is True
    assert repository.current.sandbox_id == "owned-runtime"

    assert await provisioner.terminate_shell_processes_locked(session) is True

    assert Factory.destroy_owned_by_id.await_count == 2
    cleanup.assert_awaited_once()
    handle.aclose.assert_awaited_once()
    assert session.sandbox_destroying is False
    assert session.sandbox_id is None
    assert session.task_id == "task-1"
    assert repository.current.sandbox_destroying is False
    assert repository.current.sandbox_id is None
    assert repository.current.task_id == "task-1"


async def test_private_shell_cleanup_has_a_total_retryable_deadline(
    monkeypatch,
):
    entered = asyncio.Event()

    class Factory:
        @staticmethod
        def is_owned_id(_sandbox_id, _owner_id):
            return True

        @staticmethod
        async def get_owned(_sandbox_id, _owner_id):
            entered.set()
            await asyncio.Event().wait()

    provisioner = PassthroughSandboxProvisioner(
        Factory,
        SessionRepository(),
    )
    monkeypatch.setattr(
        provisioner,
        "_SHELL_CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="owned-runtime",
        sandbox_provider="docker",
    )

    with pytest.raises(
        SandboxUnavailableError,
        match="safety deadline",
    ):
        await provisioner.terminate_shell_processes_locked(session)

    assert entered.is_set()


@pytest.mark.parametrize(
    "publish_outcome", ["success", "false_after_commit", "raise_after_commit"]
)
async def test_unpublished_rollback_is_atomically_tombstoned_before_delete(
    publish_outcome,
):
    trace = []
    current = Session(
        id="session-late-delete",
        user_id="user-1",
        agent_id="agent-1",
    )

    class CasRepository:
        async def compare_and_set_runtime_ownership(self, *_args):
            trace.append("ordinary-persist")
            raise ConnectionError("initial ownership reply unavailable")

        async def find_by_id(self, session_id):
            return current

        async def publish_runtime_destroy_claim(
            self,
            session_id,
            expected_task_id,
            expected_task_sandbox_id,
            expected_sandbox_provider,
            sandbox_id,
            sandbox_provider,
        ):
            assert current.sandbox_id is None
            assert current.sandbox_provider is expected_sandbox_provider
            assert current.task_id == expected_task_id
            assert current.task_sandbox_id == expected_task_sandbox_id
            current.sandbox_id = sandbox_id
            current.sandbox_provider = sandbox_provider
            current.sandbox_destroying = True
            trace.append("publish-tombstone")
            if publish_outcome == "false_after_commit":
                return False
            if publish_outcome == "raise_after_commit":
                raise ConnectionError("publish reply lost after commit")
            return True

        async def claim_runtime_destroy(self, *_args):
            raise AssertionError("the absent pointer must use atomic publish+claim")

    sandbox = Sandbox("generation-late-delete")

    async def indeterminate_destroy():
        trace.append("destroy")
        return False

    sandbox.destroy = indeterminate_destroy

    class Factory:
        create = AsyncMock(return_value=sandbox)
        get = AsyncMock(return_value=sandbox)

    session = current.model_copy(deep=True)
    provisioner = PassthroughSandboxProvisioner(Factory, CasRepository())
    provisioner._OWNERSHIP_RECONCILE_MAX_ATTEMPTS = 1

    with pytest.raises(
        SandboxProvisioningError,
        match="rollback and ownership persistence",
    ):
        await provisioner.ensure_locked(session)

    assert trace == ["ordinary-persist", "publish-tombstone", "destroy"]
    assert current.sandbox_id == "generation-late-delete"
    assert current.sandbox_provider == "docker"
    assert current.sandbox_destroying is True
    # A new lifecycle owner will enter tombstone recovery before any lookup or
    # adoption, so the late provider delete can never target an active pointer.


async def test_passthrough_rejects_agentbay_owned_session_before_lookup():
    repository = SessionRepository()
    provisioner = PassthroughSandboxProvisioner(SandboxFactory, repository)
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="agentbay-provider",
        sandbox_provider="agentbay",
    )

    with pytest.raises(SandboxProvisioningError, match="different provider"):
        await provisioner.ensure_locked(session)

    SandboxFactory.get.assert_not_awaited()
    SandboxFactory.create.assert_not_awaited()
    assert repository.updates == []


async def test_passthrough_adopts_legacy_id_only_after_exact_lookup():
    repository = SessionRepository()
    existing = Sandbox("legacy-provider")
    SandboxFactory.get.return_value = existing
    provisioner = PassthroughSandboxProvisioner(SandboxFactory, repository)
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="legacy-provider",
    )

    result = await provisioner.ensure_locked(session)

    assert result is existing
    SandboxFactory.get.assert_awaited_once_with("legacy-provider")
    SandboxFactory.create.assert_not_awaited()
    assert session.sandbox_provider == "docker"
    assert repository.updates == [
        ("session-1", "legacy-provider", None, "docker")
    ]


@pytest.mark.parametrize(
    "lookup_result",
    [
        None,
        Sandbox("wrong-provider"),
    ],
)
async def test_passthrough_legacy_id_fails_closed_without_exact_lookup(
    lookup_result,
):
    repository = SessionRepository()
    SandboxFactory.get.return_value = lookup_result
    provisioner = PassthroughSandboxProvisioner(SandboxFactory, repository)
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="legacy-unknown-provider",
    )

    with pytest.raises(SandboxProvisioningError, match="did not confirm"):
        await provisioner.ensure_locked(session)

    SandboxFactory.create.assert_not_awaited()
    assert session.sandbox_id == "legacy-unknown-provider"
    assert session.sandbox_provider is None
    assert repository.updates == []


async def test_passthrough_delete_clears_runtime_only_after_confirmation():
    repository = SessionRepository()
    sandbox = Sandbox("sandbox-existing")
    SandboxFactory.get.return_value = sandbox
    provisioner = PassthroughSandboxProvisioner(SandboxFactory, repository)
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="sandbox-existing",
        sandbox_provider="docker",
        task_id="task-1",
    )

    await provisioner.destroy_locked(session)

    sandbox.destroy.assert_awaited_once()
    assert session.sandbox_provider is None
    assert repository.updates == [("session-1", None, None, None)]


async def test_passthrough_delete_retains_pointer_when_unconfirmed():
    repository = SessionRepository()
    sandbox = Sandbox("sandbox-existing")
    sandbox.destroy.return_value = False
    SandboxFactory.get.return_value = sandbox
    provisioner = PassthroughSandboxProvisioner(SandboxFactory, repository)
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="sandbox-existing",
        sandbox_provider="docker",
    )

    with pytest.raises(RuntimeError, match="did not confirm"):
        await provisioner.destroy_locked(session)

    assert session.sandbox_id == "sandbox-existing"
    assert repository.updates == []


async def test_passthrough_never_deletes_agentbay_owned_session():
    repository = SessionRepository()
    sandbox = Sandbox("agentbay-provider")
    SandboxFactory.get.return_value = sandbox
    provisioner = PassthroughSandboxProvisioner(SandboxFactory, repository)
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="agentbay-provider",
        sandbox_provider="agentbay",
    )

    with pytest.raises(SandboxProvisioningError, match="different provider"):
        await provisioner.destroy_locked(session)

    SandboxFactory.get.assert_not_awaited()
    sandbox.destroy.assert_not_awaited()
    assert repository.updates == []


async def test_passthrough_adopts_exact_legacy_id_before_deleting():
    repository = SessionRepository()
    sandbox = Sandbox("legacy-provider")
    SandboxFactory.get.return_value = sandbox
    provisioner = PassthroughSandboxProvisioner(SandboxFactory, repository)
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="legacy-provider",
        task_id="task-1",
    )

    await provisioner.destroy_locked(session)

    SandboxFactory.get.assert_awaited_once_with("legacy-provider")
    sandbox.destroy.assert_awaited_once()
    assert session.sandbox_id is None
    assert session.sandbox_provider is None
    assert repository.updates == [
        ("session-1", "legacy-provider", "task-1", "docker"),
        ("session-1", None, None, None),
    ]


async def test_passthrough_legacy_missing_id_is_never_deleted():
    repository = SessionRepository()
    provisioner = PassthroughSandboxProvisioner(SandboxFactory, repository)
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="legacy-missing-provider",
    )

    with pytest.raises(SandboxProvisioningError, match="did not confirm"):
        await provisioner.destroy_locked(session)

    SandboxFactory.get.assert_awaited_once_with("legacy-missing-provider")
    assert session.sandbox_id == "legacy-missing-provider"
    assert session.sandbox_provider is None
    assert repository.updates == []


async def test_deterministic_delete_cleans_provider_after_autoremove():
    class DeterministicFactory:
        get_owned = AsyncMock(return_value=None)
        destroy_owned_by_id = AsyncMock(return_value=True)

        @staticmethod
        def planned_id(owner_id):
            return f"planned-{owner_id}"

        @staticmethod
        async def create_owned(owner_id):
            raise AssertionError("delete must not create a provider resource")

    repository = SessionRepository()
    provisioner = PassthroughSandboxProvisioner(
        DeterministicFactory, repository
    )
    session = Session(
        id="session-autoremove",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="planned-session-autoremove",
        sandbox_provider="docker",
        task_id="task-1",
    )

    await provisioner.destroy_locked(session)

    DeterministicFactory.get_owned.assert_not_awaited()
    DeterministicFactory.destroy_owned_by_id.assert_awaited_once_with(
        "planned-session-autoremove", "session-autoremove"
    )
    assert session.sandbox_id is None
    assert session.task_id is None
    assert repository.updates == [
        ("session-autoremove", None, None, None)
    ]


async def test_failed_deterministic_create_removes_orphan_network_and_pointer():
    class DeterministicFactory:
        destroy_owned_by_id = AsyncMock(return_value=True)

        @staticmethod
        def planned_id(owner_id):
            return f"planned-{owner_id}"

        @staticmethod
        async def create_owned(owner_id):
            raise SandboxProvisioningError(
                f"planned-{owner_id}",
                "containers.run failed after network creation",
            )

    repository = SessionRepository()
    provisioner = PassthroughSandboxProvisioner(
        DeterministicFactory, repository
    )
    session = Session(
        id="session-create-failed",
        user_id="user-1",
        agent_id="agent-1",
    )

    with pytest.raises(SandboxProvisioningError, match="containers.run failed"):
        await provisioner.ensure_locked(session)

    DeterministicFactory.destroy_owned_by_id.assert_awaited_once_with(
        "planned-session-create-failed", "session-create-failed"
    )
    assert session.sandbox_id is None
    assert session.sandbox_provider is None
    assert repository.updates == [
        (
            "session-create-failed",
            "planned-session-create-failed",
            None,
            "docker",
        ),
    ]


async def test_foreign_deterministic_network_retains_pointer_fail_closed():
    class DeterministicFactory:
        get_owned = AsyncMock(return_value=None)
        destroy_owned_by_id = AsyncMock(return_value=False)

        @staticmethod
        def planned_id(owner_id):
            return f"planned-{owner_id}"

        @staticmethod
        async def create_owned(owner_id):
            raise AssertionError("delete must not create a provider resource")

    repository = SessionRepository()
    provisioner = PassthroughSandboxProvisioner(
        DeterministicFactory, repository
    )
    session = Session(
        id="session-foreign",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="planned-session-foreign",
        sandbox_provider="docker",
    )

    with pytest.raises(RuntimeError, match="did not confirm deletion"):
        await provisioner.destroy_locked(session)

    DeterministicFactory.get_owned.assert_not_awaited()
    assert session.sandbox_id == "planned-session-foreign"
    assert repository.updates == []


async def test_lost_lease_cancellation_keeps_prepublished_generation():
    class BlockingFactory:
        started = asyncio.Event()
        destroy_owned_by_id = AsyncMock(return_value=True)

        @staticmethod
        def planned_id(owner_id):
            return f"planned-{owner_id}"

        @staticmethod
        def plan_owned_id(owner_id):
            return f"planned-{owner_id}-generation"

        @staticmethod
        def is_owned_id(container_name, owner_id):
            return container_name == f"planned-{owner_id}-generation"

        @classmethod
        async def create_owned_with_id(cls, owner_id, container_name):
            cls.started.set()
            await asyncio.Event().wait()

        @staticmethod
        async def create_owned(owner_id):
            raise AssertionError("exact generation create is required")

    repository = SessionRepository()
    provisioner = PassthroughSandboxProvisioner(
        BlockingFactory, repository
    )
    session = Session(
        id="session-lease-lost",
        user_id="user-1",
        agent_id="agent-1",
    )

    task = asyncio.create_task(provisioner.ensure_locked(session))
    await BlockingFactory.started.wait()
    mark_lifecycle_task_lease_lost(task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.sandbox_id == "planned-session-lease-lost-generation"
    assert session.sandbox_provider == "docker"
    BlockingFactory.destroy_owned_by_id.assert_not_awaited()


async def test_stale_destroy_cannot_clear_new_generation_or_task():
    class CasRepository:
        def __init__(self):
            self.current = Session(
                id="session-cas",
                user_id="user-1",
                agent_id="agent-1",
                sandbox_id="planned-session-cas-generation-b",
                sandbox_provider="docker",
                task_id="task-b",
            )

        async def compare_and_set_runtime_ownership(
            self,
            session_id,
            expected_sandbox_id,
            expected_task_id,
            expected_task_sandbox_id,
            expected_sandbox_provider,
            sandbox_id,
            task_id,
            sandbox_provider=None,
            task_sandbox_id=None,
        ):
            if (
                self.current.sandbox_id != expected_sandbox_id
                or self.current.task_id != expected_task_id
                or self.current.task_sandbox_id
                != expected_task_sandbox_id
                or self.current.sandbox_provider
                != expected_sandbox_provider
            ):
                return False
            self.current.sandbox_id = sandbox_id
            self.current.task_id = task_id
            self.current.sandbox_provider = sandbox_provider
            self.current.task_sandbox_id = task_sandbox_id
            return True

        async def find_by_id(self, session_id):
            return self.current

    class GenerationalFactory:
        destroy_owned_by_id = AsyncMock(return_value=True)

        @staticmethod
        def planned_id(owner_id):
            return f"planned-{owner_id}"

        @staticmethod
        def is_owned_id(container_name, owner_id):
            return container_name.startswith(f"planned-{owner_id}-generation-")

        @staticmethod
        async def create_owned(owner_id):
            raise AssertionError("destroy must not create")

    repository = CasRepository()
    stale = Session(
        id="session-cas",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="planned-session-cas-generation-a",
        sandbox_provider="docker",
        task_id="task-a",
    )
    provisioner = PassthroughSandboxProvisioner(
        GenerationalFactory, repository
    )

    with pytest.raises(RuntimeError, match="ownership changed"):
        await provisioner.destroy_locked(stale)

    assert repository.current.sandbox_id == "planned-session-cas-generation-b"
    assert repository.current.task_id == "task-b"


async def test_destroying_tombstone_is_resumed_before_new_generation():
    trace = []

    class CasRepository:
        def __init__(self):
            self.current = Session(
                id="session-resume",
                user_id="user-1",
                agent_id="agent-1",
                sandbox_id="planned-session-resume-generation-old",
                sandbox_provider="docker",
                sandbox_destroying=True,
                task_id="task-old",
            )

        async def compare_and_set_runtime_ownership(
            self,
            session_id,
            expected_sandbox_id,
            expected_task_id,
            expected_task_sandbox_id,
            expected_sandbox_provider,
            sandbox_id,
            task_id,
            sandbox_provider=None,
            task_sandbox_id=None,
        ):
            current = self.current
            if (
                current.sandbox_destroying
                or current.sandbox_id != expected_sandbox_id
                or current.task_id != expected_task_id
                or current.task_sandbox_id != expected_task_sandbox_id
                or current.sandbox_provider != expected_sandbox_provider
            ):
                return False
            current.sandbox_id = sandbox_id
            current.task_id = task_id
            current.sandbox_provider = sandbox_provider
            current.task_sandbox_id = task_sandbox_id
            return True

        async def finish_runtime_destroy(
            self,
            session_id,
            expected_sandbox_id,
            expected_task_id,
            expected_task_sandbox_id,
            expected_sandbox_provider,
            sandbox_id,
            task_id,
            sandbox_provider,
            task_sandbox_id,
        ):
            current = self.current
            trace.append("finish")
            if (
                not current.sandbox_destroying
                or current.sandbox_id != expected_sandbox_id
                or current.task_id != expected_task_id
                or current.task_sandbox_id != expected_task_sandbox_id
                or current.sandbox_provider != expected_sandbox_provider
            ):
                return False
            current.sandbox_id = sandbox_id
            current.task_id = task_id
            current.sandbox_provider = sandbox_provider
            current.task_sandbox_id = task_sandbox_id
            current.sandbox_destroying = False
            return True

        async def find_by_id(self, session_id):
            return self.current

    class GenerationalFactory:
        @staticmethod
        def plan_owned_id(owner_id):
            return f"planned-{owner_id}-generation-new"

        @staticmethod
        def is_owned_id(container_name, owner_id):
            return container_name.startswith(f"planned-{owner_id}-generation-")

        @staticmethod
        async def destroy_owned_by_id(container_name, owner_id):
            trace.append(("destroy", container_name))
            return True

        @staticmethod
        async def create_owned_with_id(owner_id, container_name):
            trace.append(("create", container_name))
            return Sandbox(container_name)

        @staticmethod
        async def create_owned(owner_id):
            raise AssertionError("exact generation create is required")

    repository = CasRepository()
    session = repository.current.model_copy(deep=True)
    provisioner = PassthroughSandboxProvisioner(
        GenerationalFactory, repository
    )

    sandbox = await provisioner.ensure_locked(session)

    assert sandbox.id == "planned-session-resume-generation-new"
    assert trace == [
        ("destroy", "planned-session-resume-generation-old"),
        "finish",
        ("create", "planned-session-resume-generation-new"),
    ]
    assert repository.current.sandbox_id == sandbox.id
    assert repository.current.task_id == "task-old"
    assert repository.current.sandbox_destroying is False


async def test_idempotent_runtime_write_rejects_claimed_destroy_projection():
    target = Session(
        id="session-claimed",
        user_id="user-1",
        agent_id="agent-1",
        sandbox_id="generation-a",
        sandbox_provider="docker",
        sandbox_destroying=False,
        task_id="task-a",
    )

    class CasRepository:
        async def compare_and_set_runtime_ownership(self, *_args):
            return False

        async def find_by_id(self, session_id):
            current = target.model_copy(deep=True)
            current.sandbox_destroying = True
            return current

    provisioner = PassthroughSandboxProvisioner(
        SandboxFactory, CasRepository()
    )

    with pytest.raises(RuntimeError, match="ownership changed"):
        await provisioner._persist(
            target,
            expected_sandbox_id="generation-a",
            expected_task_id="task-a",
            expected_sandbox_provider="docker",
        )


async def test_failed_prepublished_create_claims_before_provider_rollback():
    trace = []

    class CasRepository:
        def __init__(self):
            self.current = Session(
                id="session-rollback-claim",
                user_id="user-1",
                agent_id="agent-1",
            )

        async def compare_and_set_runtime_ownership(
            self,
            session_id,
            expected_sandbox_id,
            expected_task_id,
            expected_task_sandbox_id,
            expected_sandbox_provider,
            sandbox_id,
            task_id,
            sandbox_provider,
            task_sandbox_id,
        ):
            current = self.current
            if (
                current.sandbox_destroying
                or current.sandbox_id != expected_sandbox_id
                or current.task_id != expected_task_id
                or current.task_sandbox_id != expected_task_sandbox_id
                or current.sandbox_provider != expected_sandbox_provider
            ):
                return False
            current.sandbox_id = sandbox_id
            current.task_id = task_id
            current.task_sandbox_id = task_sandbox_id
            current.sandbox_provider = sandbox_provider
            trace.append("publish")
            return True

        async def claim_runtime_destroy(
            self,
            session_id,
            expected_sandbox_id,
            expected_task_id,
            expected_task_sandbox_id,
            expected_sandbox_provider,
        ):
            current = self.current
            if (
                current.sandbox_destroying
                or current.sandbox_id != expected_sandbox_id
                or current.task_id != expected_task_id
                or current.task_sandbox_id != expected_task_sandbox_id
                or current.sandbox_provider != expected_sandbox_provider
            ):
                return False
            current.sandbox_destroying = True
            trace.append("claim")
            return True

        async def finish_runtime_destroy(
            self,
            session_id,
            expected_sandbox_id,
            expected_task_id,
            expected_task_sandbox_id,
            expected_sandbox_provider,
            sandbox_id,
            task_id,
            sandbox_provider,
            task_sandbox_id,
        ):
            assert self.current.sandbox_destroying is True
            self.current.sandbox_id = sandbox_id
            self.current.task_id = task_id
            self.current.task_sandbox_id = task_sandbox_id
            self.current.sandbox_provider = sandbox_provider
            self.current.sandbox_destroying = False
            trace.append("finish")
            return True

        async def find_by_id(self, session_id):
            return self.current

    repository = CasRepository()

    class Factory:
        @staticmethod
        def plan_owned_id(owner_id):
            return f"planned-{owner_id}-failed"

        @staticmethod
        def is_owned_id(container_name, owner_id):
            return container_name == f"planned-{owner_id}-failed"

        @staticmethod
        async def create_owned_with_id(owner_id, container_name):
            trace.append("create")
            raise RuntimeError("provider create failed")

        @staticmethod
        async def create_owned(owner_id):
            raise AssertionError("exact generation create is required")

        @staticmethod
        async def destroy_owned_by_id(container_name, owner_id):
            assert repository.current.sandbox_destroying is True
            trace.append("destroy")
            return True

    session = repository.current.model_copy(deep=True)
    provisioner = PassthroughSandboxProvisioner(Factory, repository)

    with pytest.raises(RuntimeError, match="provider create failed"):
        await provisioner.ensure_locked(session)

    assert trace == ["publish", "create", "claim", "destroy", "finish"]
    assert repository.current.sandbox_id is None
    assert repository.current.sandbox_destroying is False
