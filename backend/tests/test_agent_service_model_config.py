from types import SimpleNamespace
import json

import pytest

from app.application.errors.exceptions import BadRequestError
from app.application.services.agent_service import AgentService
from app.core.config import get_settings


class _AgentRepo:
    def __init__(self):
        self.saved = []

    async def save(self, agent):
        self.saved.append(agent)

    async def delete(self, agent_id):
        self.saved = [agent for agent in self.saved if agent.id != agent_id]


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("API_KEY", "env-key")
    monkeypatch.setenv("API_BASE", "https://env.example")
    monkeypatch.setenv("MODEL_NAME", "env-model")
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-only-model-credential-secret-32-bytes")
    monkeypatch.setattr(
        "app.infrastructure.external.llm.security.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", args[1]))
        ],
    )
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
    assert agent.is_byok is True
    assert repo.saved == [agent]


@pytest.mark.asyncio
async def test_create_agent_resolves_configured_model_id(monkeypatch):
    monkeypatch.setenv(
        "AVAILABLE_MODELS",
        json.dumps([
            {
                "id": "sonnet-4-6",
                "label": "Claude Sonnet 4.6",
                "model_name": "claude-sonnet-4-6",
                "model_provider": "anthropic",
                "api_base": "https://anthropic.example/v1",
                "api_key": "sk-sonnet",
            }
        ]),
    )
    get_settings.cache_clear()
    repo = _AgentRepo()
    service = AgentService(
        agent_repository=repo,
        session_repository=None,
        sandbox_cls=None,
        task_cls=None,
        file_storage=None,
        mcp_repository=None,
    )

    agent = await service._create_agent(SimpleNamespace(model_id="sonnet-4-6"))

    assert agent.api_key is None
    assert agent.model_id == "sonnet-4-6"
    assert agent.api_base == "https://anthropic.example/v1"
    assert agent.model_name == "claude-sonnet-4-6"
    assert agent.model_provider == "anthropic"


@pytest.mark.asyncio
async def test_create_agent_rejects_unknown_model_id():
    service = AgentService(
        agent_repository=_AgentRepo(),
        session_repository=None,
        sandbox_cls=None,
        task_cls=None,
        file_storage=None,
        mcp_repository=None,
    )

    with pytest.raises(BadRequestError):
        await service._create_agent(SimpleNamespace(model_id="missing-model"))


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

    assert agent.api_key is None
    assert agent.is_byok is False
    assert agent.api_base == "https://env.example"
    assert agent.model_name == "env-model"
    assert agent.model_provider == "openai"
