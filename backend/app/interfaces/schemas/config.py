from pydantic import BaseModel


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
