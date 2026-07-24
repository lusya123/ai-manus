from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.domain.models.agent import Agent
from app.domain.services.agent_task_runner import AgentTaskRunnerFactory
from app.infrastructure.external.llm.factory import ConfigurableLLMFactory
from app.infrastructure.external.llm.security import ResolvedPublicModelEndpoint


PUBLIC_IP = "93.184.216.34"


def test_configurable_llm_factory_applies_persisted_agent_overrides(monkeypatch):
    captured = {}

    class Gateway:
        def __init__(self, settings):
            captured["settings"] = settings

    monkeypatch.setattr(
        "app.infrastructure.external.llm.factory.AnthropicLLM", Gateway
    )
    monkeypatch.setattr(
        "app.infrastructure.external.llm.factory.resolve_public_model_endpoint",
        lambda url: ResolvedPublicModelEndpoint(
            url=url,
            hostname="sub2api.example",
            addresses=(PUBLIC_IP,),
        ),
    )
    settings = Settings(
        _env_file=None,
        api_key="default-key",
        api_base="https://default.example/v1",
        model_name="default-model",
        model_provider="openai",
        llm_provider="openai",
    )
    agent = Agent(
        model_name="claude-opus-4-6",
        model_provider="anthropic",
        api_base="https://sub2api.example/v1",
        api_key="per-session-key",
        is_byok=True,
        temperature=0.2,
        max_tokens=4096,
    )

    gateway = ConfigurableLLMFactory(settings).create(agent)

    assert isinstance(gateway, Gateway)
    resolved = captured["settings"]
    assert resolved.model_name == "claude-opus-4-6"
    assert resolved.model_provider == "anthropic"
    assert resolved.api_base == "https://sub2api.example/v1"
    assert resolved.api_key == "per-session-key"
    assert resolved.byok_pinned_ip == PUBLIC_IP
    assert resolved.extra_headers is None
    assert resolved.temperature == 0.2
    assert resolved.max_tokens == 4096


class _Sandbox:
    id = "sandbox-1"

    async def get_browser(self):
        return SimpleNamespace()


class _SandboxClass:
    @classmethod
    async def get(cls, sandbox_id):
        return _Sandbox()


class _SessionRepository:
    async def find_by_id(self, session_id):
        return None

    async def find_by_id_and_user_id(self, session_id, user_id):
        return SimpleNamespace(
            id=session_id,
            user_id=user_id,
            agent_id=(
                "missing" if session_id == "session-1-missing" else "agent-1"
            ),
            sandbox_id="sandbox-1",
            sandbox_provider="docker",
            deleting=False,
            sandbox_destroying=False,
            task_id="task-1",
            task_sandbox_id="sandbox-1",
        )


async def test_task_runner_factory_reconstructs_llm_from_persisted_agent(monkeypatch):
    agent = Agent(
        id="agent-1",
        model_name="session-model",
        model_provider="anthropic",
        api_key="session-key",
    )

    class AgentRepository:
        async def find_by_id(self, agent_id):
            return agent

    class LLMFactory:
        def __init__(self):
            self.agent = None
            self.llm = SimpleNamespace()

        def create(self, persisted_agent):
            self.agent = persisted_agent
            return self.llm

    llm_factory = LLMFactory()
    captured = {}

    class Runner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "app.domain.services.agent_task_runner.AgentTaskRunner", Runner
    )
    factory = AgentTaskRunnerFactory(
        agent_repository=AgentRepository(),
        session_repository=_SessionRepository(),
        sandbox_cls=_SandboxClass,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        llm_factory=llm_factory,
    )

    await factory.create_runner(
        {
            "session_id": "session-1",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "sandbox_id": "sandbox-1",
            "task_id": "task-1",
            "task_sandbox_id": "sandbox-1",
        }
    )

    assert llm_factory.agent is agent
    assert captured["llm"] is llm_factory.llm
    assert captured["sandbox_id"] == "sandbox-1"


async def test_task_runner_factory_does_not_silently_fallback_when_agent_missing():
    class AgentRepository:
        async def find_by_id(self, agent_id):
            return None

    factory = AgentTaskRunnerFactory(
        agent_repository=AgentRepository(),
        session_repository=_SessionRepository(),
        sandbox_cls=_SandboxClass,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        llm_factory=SimpleNamespace(create=lambda agent: SimpleNamespace()),
    )

    with pytest.raises(RuntimeError, match="Agent configuration not found"):
        await factory.create_runner(
            {
                "session_id": "session-1-missing",
                "agent_id": "missing",
                "user_id": "user-1",
                "sandbox_id": "sandbox-1",
                "task_id": "task-1",
                "task_sandbox_id": "sandbox-1",
            }
        )
