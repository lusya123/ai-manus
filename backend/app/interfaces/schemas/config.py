from pydantic import BaseModel


class ModelOptionResponse(BaseModel):
    """Model option exposed to the frontend model picker."""
    id: str
    label: str
    model_name: str
    model_provider: str
    api_base: str | None = None


class ClientConfigResponse(BaseModel):
    """Client runtime configuration response schema"""
    auth_provider: str
    sub2api_login_url: str | None = None
    sub2api_console_url: str | None = None
    sub2api_marketplace_url: str | None = None
    sub2api_use_token_url: str | None = None
    show_github_button: bool
    github_repository_url: str
    google_analytics_id: str | None = None
    claw_enabled: bool
    default_model: ModelOptionResponse
    available_models: list[ModelOptionResponse]
