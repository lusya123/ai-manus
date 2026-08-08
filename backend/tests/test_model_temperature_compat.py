from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.domain.models.message import LLMMessage
from app.infrastructure.external.browser.playwright_browser import (
    PlaywrightBrowser,
)
from app.infrastructure.external.llm.anthropic_llm import AnthropicLLM
from app.infrastructure.external.llm.langchain_llm import LangchainLLM
from app.infrastructure.external.llm.model_capabilities import (
    effective_temperature,
)


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("anthropic", "claude-opus-4-7", None),
        ("anthropic", "claude-opus-4-7-20260701", None),
        ("anthropic", "claude-opus-4-8", None),
        ("anthropic", "claude-opus-4-8-20260805", None),
        ("anthropic", "claude-opus-4-10", None),
        ("anthropic", "claude-opus-5", None),
        ("anthropic", "claude-sonnet-5", None),
        ("anthropic", "claude-sonnet-5-1-20260805", None),
        ("anthropic", "claude-opus-4-6", 0.7),
        ("anthropic", "claude-opus-4-20250514", 0.7),
        ("anthropic", "claude-sonnet-4-6", 0.7),
        ("anthropic", "claude-haiku-4-5", 0.7),
        ("anthropic", "custom-model-alias", 0.7),
        ("anthropic", "x-claude-opus-4-8", 0.7),
        ("anthropic", "claude-opus-4-8-preview", 0.7),
        ("anthropic", "claude-opus-4-8evil", 0.7),
        ("anthropic", "claude-ſonnet-5", 0.7),
        ("anthropic", "claude-opus-4-20250514-20260808", 0.7),
        ("openai", "claude-opus-4-8", 0.7),
    ],
)
def test_effective_temperature_respects_provider_model_capabilities(
    provider,
    model,
    expected,
):
    assert effective_temperature(provider, model, 0.7) == expected


@pytest.mark.parametrize(
    ("model", "expected_temperature"),
    [
        ("claude-opus-4-8", None),
        ("claude-opus-4-6", 0.7),
    ],
)
def test_langchain_anthropic_omits_only_unsupported_temperature(
    monkeypatch,
    model,
    expected_temperature,
):
    captured = {}
    fake_model = SimpleNamespace()
    monkeypatch.setattr(
        "app.infrastructure.external.llm.langchain_llm.init_chat_model",
        lambda **kwargs: captured.update(kwargs) or fake_model,
    )
    monkeypatch.setattr(
        "app.infrastructure.external.llm.langchain_llm.RetryWithErrorOutputParser.from_llm",
        lambda **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "app.infrastructure.external.llm.langchain_llm.RobustJsonParser.from_llm",
        lambda _model: SimpleNamespace(),
    )

    LangchainLLM(
        Settings(
            _env_file=None,
            anthropic_api_key="anthropic-key",
            model_name=model,
            model_provider="anthropic",
            temperature=0.7,
        )
    )

    if expected_temperature is None:
        assert "temperature" not in captured
    else:
        assert captured["temperature"] == expected_temperature


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expected_temperature"),
    [
        ("claude-opus-4-8", None),
        ("claude-opus-4-6", 0.7),
    ],
)
async def test_native_anthropic_omits_only_unsupported_temperature(
    monkeypatch,
    model,
    expected_temperature,
):
    captured = {}

    class FakeHTTPClient:
        is_closed = False

        async def aclose(self):
            self.is_closed = True

    class FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=[])

    class FakeAsyncAnthropic:
        def __init__(self, **_kwargs):
            self.messages = FakeMessages()

        async def close(self):
            return None

    http_client = FakeHTTPClient()
    monkeypatch.setattr(
        "app.infrastructure.external.llm.anthropic_llm.create_pinned_model_http_client",
        lambda *_args, **_kwargs: http_client,
    )
    monkeypatch.setattr(
        "app.infrastructure.external.llm.anthropic_llm.AsyncAnthropic",
        FakeAsyncAnthropic,
    )
    gateway = AnthropicLLM(
        Settings(
            _env_file=None,
            api_key="byok-key",
            api_base="https://gateway.example",
            byok_pinned_ip="93.184.216.34",
            model_name=model,
            model_provider="anthropic",
            temperature=0.7,
        )
    )

    await gateway.ask([LLMMessage.user("hello")])
    await gateway.aclose()

    if expected_temperature is None:
        assert "temperature" not in captured
    else:
        assert captured["temperature"] == expected_temperature
    assert http_client.is_closed


@pytest.mark.parametrize(
    ("model", "expected_temperature"),
    [
        ("claude-opus-4-8", None),
        ("claude-opus-4-6", 0.7),
    ],
)
def test_playwright_model_omits_only_unsupported_temperature(
    monkeypatch,
    model,
    expected_temperature,
):
    captured = {}
    settings = SimpleNamespace(
        model_name=model,
        model_provider="anthropic",
        temperature=0.7,
        max_tokens=1024,
        api_base="https://gateway.example",
        extra_headers=None,
    )
    monkeypatch.setattr(
        "app.infrastructure.external.browser.playwright_browser.get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "app.infrastructure.external.browser.playwright_browser.init_chat_model",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(),
    )

    PlaywrightBrowser("http://sandbox:9222")

    if expected_temperature is None:
        assert "temperature" not in captured
    else:
        assert captured["temperature"] == expected_temperature
