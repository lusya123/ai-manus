from types import SimpleNamespace

import pytest

from app.application.services.agent_service import AgentService
from app.core.config import get_settings


class _AgentRepo:
    def __init__(self):
        self.saved = []

    async def save(self, agent):
        self.saved.append(agent)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("API_KEY", "env-key")
    monkeypatch.setenv("API_BASE", "https://env.example")
    monkeypatch.setenv("MODEL_NAME", "env-model")
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_create_agent_uses_per_session_sub2api_model_config():
    repo = _AgentRepo()
    service = AgentService(
        agent_repository=repo,
        session_repository=None,
        sandbox_cls=None,
        task_cls=None,
        file_storage=None,
        mcp_repository=None,
    )

    agent = await service._create_agent(
        SimpleNamespace(
            api_key="sk-user",
            api_base="https://xuedingtoken.com",
            model_name="claude-opus-4-6",
            model_provider="anthropic",
        )
    )

    assert agent.api_key == "sk-user"
    assert agent.api_base == "https://xuedingtoken.com"
    assert agent.model_name == "claude-opus-4-6"
    assert agent.model_provider == "anthropic"
    assert repo.saved == [agent]


@pytest.mark.asyncio
async def test_create_agent_falls_back_to_environment_model_config():
    repo = _AgentRepo()
    service = AgentService(
        agent_repository=repo,
        session_repository=None,
        sandbox_cls=None,
        task_cls=None,
        file_storage=None,
        mcp_repository=None,
    )

    agent = await service._create_agent(None)

    assert agent.api_key == "env-key"
    assert agent.api_base == "https://env.example"
    assert agent.model_name == "env-model"
    assert agent.model_provider == "openai"

