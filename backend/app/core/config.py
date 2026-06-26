import os
import json
import logging
from pydantic_settings import BaseSettings
from functools import lru_cache

logger = logging.getLogger(__name__)


def _parse_extra_headers() -> dict | None:
    raw = os.environ.get("EXTRA_HEADERS")
    if not raw:
        return None
    try:
        headers = json.loads(raw)
        if isinstance(headers, dict):
            return headers
        logger.warning("EXTRA_HEADERS is not a JSON object, ignoring")
    except json.JSONDecodeError:
        logger.warning("EXTRA_HEADERS is not valid JSON, ignoring")
    return None


class Settings(BaseSettings):
    
    # Model provider configuration
    api_key: str | None = None
    api_base: str | None = None
    
    # Model configuration
    model_name: str = "gpt-4o"
    model_provider: str = "openai"
    temperature: float = 0.7
    max_tokens: int = 2000
    
    # MongoDB configuration
    mongodb_uri: str = "mongodb://mongodb:27017"
    mongodb_database: str = "manus"
    mongodb_username: str | None = None
    mongodb_password: str | None = None
    
    # Redis configuration
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None
    
    # Sandbox configuration
    sandbox_address: str | None = None
    sandbox_api_port: int = 8080
    sandbox_cdp_port: int = 9222
    sandbox_vnc_port: int = 5901
    sandbox_image: str | None = None
    sandbox_name_prefix: str | None = None
    sandbox_ttl_minutes: int | None = 30
    sandbox_network: str | None = None  # Docker network bridge name
    sandbox_chrome_args: str | None = ""
    sandbox_https_proxy: str | None = None
    sandbox_http_proxy: str | None = None
    sandbox_no_proxy: str | None = None

    # Browser engine configuration
    browser_engine: str = "browser_use"  # "playwright" or "browser_use"
    tool_call_timeout_seconds: int = 180

    # Runtime topology advertised to agents.
    # Public URLs are for the user's machine or internet-facing access.
    # Internal/container URLs are for services on the Docker/cloud private network.
    # Sandbox URLs are what commands and the browser inside the sandbox should use.
    deployment_environment: str = "development"
    frontend_public_url: str | None = None
    backend_public_url: str | None = None
    frontend_internal_url: str | None = None
    backend_internal_url: str | None = None
    frontend_sandbox_url: str | None = None
    backend_sandbox_url: str | None = None
    claw_public_url: str | None = None
    claw_internal_url: str | None = None
    host_gateway_url: str | None = None
    
    # Search engine configuration
    search_provider: str | None = "bing_web"  # "baidu", "baidu_web", "google", "bing", "bing_web", "tavily"
    baidu_search_api_key: str | None = None
    bing_search_api_key: str | None = None
    google_search_api_key: str | None = None
    google_search_engine_id: str | None = None
    tavily_api_key: str | None = None
    
    # Google Analytics configuration
    google_analytics_id: str | None = None

    # Auth configuration
    auth_provider: str = "none"  # "password", "none", "local", "sub2api"
    show_github_button: bool = True
    github_repository_url: str = "https://github.com/simpleyyt/ai-manus"
    password_salt: str | None = None
    password_hash_rounds: int = 10
    password_hash_algorithm: str = "pbkdf2_sha256"
    local_auth_email: str = "admin@example.com"
    local_auth_password: str = "admin"
    sub2api_base_url: str | None = None
    sub2api_login_url: str | None = None
    sub2api_console_url: str | None = None
    sub2api_marketplace_url: str | None = None
    sub2api_use_token_url: str | None = None
    sub2api_auth_me_path: str = "/api/v1/auth/me"
    sub2api_auth_refresh_path: str = "/api/v1/auth/refresh"
    sub2api_timeout_seconds: float = 10.0
    
    # Email configuration
    email_host: str | None = None  # "smtp.gmail.com"
    email_port: int | None = None  # 587
    email_username: str | None = None
    email_password: str | None = None
    email_from: str | None = None
    
    # JWT configuration
    jwt_secret_key: str = "your-secret-key-here"  # Should be set in production
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_refresh_token_expire_days: int = 7
    
    # Extra headers for LLM requests (parsed from EXTRA_HEADERS env var, JSON)
    extra_headers: dict | None = None
    
    # Claw (OpenClaw) configuration
    claw_enabled: bool = True
    claw_image: str = "simpleyyt/manus-claw"
    claw_name_prefix: str = "manus-claw"
    claw_ttl_seconds: int = 0
    claw_network: str | None = None  # Docker network bridge name for claw containers
    claw_ready_timeout: int = 300  # Max seconds to wait for claw container to become ready
    claw_address: str | None = None  # If set, use this fixed host instead of creating Docker containers
    claw_api_key: str | None = None  # Static API key accepted by the LLM proxy (for dev/fixed container)
    manus_api_base_url: str = "http://backend:8000"  # URL of this backend accessible from claw containers
    claw_publish_host_ports: bool = True
    claw_host_bind_address: str = "127.0.0.1"
    claw_http_container_port: int = 18788
    claw_gateway_container_port: int = 18789
    claw_max_instances_total: int = 20
    claw_idle_timeout_seconds: int = 0
    claw_cleanup_interval_seconds: int = 60
    claw_destroy_on_delete: bool = True
    claw_memory_limit: str | None = "1g"
    claw_nano_cpus: int | None = 1_000_000_000
    claw_pids_limit: int | None = 256

    # MCP configuration
    mcp_config_path: str = "/etc/mcp.json"
    
    # Logging configuration
    log_level: str = "INFO"
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        
    def validate(self):
        """Validate configuration settings"""
        if not self.api_key:
            raise ValueError("API key is required")

@lru_cache()
def get_settings() -> Settings:
    """Get application settings"""
    if not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = os.getenv("API_KEY")
    settings = Settings()
    settings.extra_headers = _parse_extra_headers()
    settings.validate()
    return settings 
