from types import SimpleNamespace

from app.interfaces.api.config_routes import get_frontend_config


def _model(
    model_id: str,
    provider: str,
    *,
    api_key: str | None = None,
):
    return SimpleNamespace(
        id=model_id,
        label=model_id,
        model_name=model_id,
        model_provider=provider,
        api_base=None,
        api_key=api_key,
    )


async def test_frontend_catalog_hides_models_without_a_usable_server_key(
    monkeypatch,
):
    settings = SimpleNamespace(
        auth_provider="none",
        sub2api_base_url=None,
        sub2api_login_url=None,
        sub2api_console_url=None,
        sub2api_marketplace_url=None,
        sub2api_use_token_url=None,
        show_github_button=False,
        github_repository_url="",
        google_analytics_id=None,
        claw_enabled=False,
        registration_enabled=False,
        model_name="default-openai",
        model_provider="openai",
        api_base="https://openai.example/v1",
        api_key="openai-server-key",
        anthropic_api_key=None,
        available_models=[
            _model("openai-ready", "openai"),
            _model("anthropic-missing-key", "anthropic"),
            _model("anthropic-explicit", "anthropic", api_key="catalog-key"),
        ],
    )
    monkeypatch.setattr(
        "app.interfaces.api.config_routes.get_settings", lambda: settings
    )

    response = await get_frontend_config()

    assert [model.id for model in response.data.available_models] == [
        "openai-ready",
        "anthropic-explicit",
    ]
    assert response.data.supported_byok_providers == [
        "openai",
        "anthropic",
        "deepseek",
        "ollama",
    ]
