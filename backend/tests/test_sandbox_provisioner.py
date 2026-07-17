from unittest.mock import AsyncMock

import pytest

from app.domain.external.sandbox import SandboxProvisioningError
from app.domain.models.session import Session
from app.infrastructure.external.sandbox.passthrough_provisioner import (
    PassthroughSandboxProvisioner,
)


class SessionRepository:
    def __init__(self):
        self.updates = []

    async def update_runtime_ownership(
        self, session_id, sandbox_id, task_id, sandbox_provider=None
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


async def test_passthrough_create_failure_persists_cleanup_id_and_provider():
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

    assert session.sandbox_id == "orphan-candidate"
    assert session.sandbox_provider == "docker"
    assert repository.updates == [
        ("session-1", "orphan-candidate", None, "docker")
    ]


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
