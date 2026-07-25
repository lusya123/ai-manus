import os
import ipaddress
from urllib.parse import urlsplit
import json
import logging
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from functools import lru_cache

logger = logging.getLogger(__name__)


INSECURE_JWT_SECRETS = {
    "",
    "your-secret-key-here",
    "change-me",
    "changeme",
    "secret",
}

# Providers installed and supported by the per-session BYOK gateway. Keep this
# as one backend-owned contract so the UI cannot advertise unsupported values.
SUPPORTED_BYOK_PROVIDERS = ("openai", "anthropic", "deepseek", "ollama")

# MongoDB rejects documents larger than 16 MiB.  Claw chat messages are
# embedded in the same aggregate as lifecycle metadata, so history must leave
# meaningful headroom for the rest of that document and BSON array overhead.
CLAW_HISTORY_DEFAULT_MAX_BYTES = 8 * 1024 * 1024
CLAW_HISTORY_SAFE_MAX_BYTES = 12 * 1024 * 1024
# Session documents also embed canonical file metadata, so their event
# projection needs more headroom than the Claw-only lifecycle aggregate.
# Complete durable turn replay lives in the separate turn-output collection;
# this embedded history is intentionally only a recent, bounded projection.
SESSION_HISTORY_DEFAULT_MAX_BYTES = 6 * 1024 * 1024
SESSION_HISTORY_SAFE_MAX_BYTES = 6 * 1024 * 1024


def is_secure_jwt_secret(secret: str | None) -> bool:
    """Return whether a JWT root secret is suitable for real deployments."""

    normalized = str(secret or "")
    return (
        normalized.lower() not in INSECURE_JWT_SECRETS
        and len(normalized.encode("utf-8")) >= 32
    )


class ConfiguredModelOption(BaseModel):
    """Model option selectable for a newly created chat session."""

    id: str
    label: str
    model_name: str
    model_provider: str = "openai"
    api_base: str | None = None
    api_key: str | None = None


def _default_available_models() -> list[ConfiguredModelOption]:
    return [
        ConfiguredModelOption(
            id="claude-sonnet-4-6",
            label="Claude Sonnet 4.6",
            model_name="claude-sonnet-4-6",
            model_provider="anthropic",
        ),
        ConfiguredModelOption(
            id="claude-opus-4-6",
            label="Claude Opus 4.6",
            model_name="claude-opus-4-6",
            model_provider="anthropic",
        ),
        ConfiguredModelOption(
            id="claude-opus-4-7",
            label="Claude Opus 4.7",
            model_name="claude-opus-4-7",
            model_provider="anthropic",
        ),
        ConfiguredModelOption(
            id="claude-opus-4-8",
            label="Claude Opus 4.8",
            model_name="claude-opus-4-8",
            model_provider="anthropic",
        ),
        ConfiguredModelOption(
            id="gpt-4o",
            label="GPT-4o",
            model_name="gpt-4o",
            model_provider="openai",
        ),
        ConfiguredModelOption(
            id="gpt-4o-mini",
            label="GPT-4o mini",
            model_name="gpt-4o-mini",
            model_provider="openai",
        ),
    ]


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
    # Accept the conventional provider-specific environment variable so a
    # developer .env does not fail validation. ``api_key`` remains the
    # project's canonical key and per-session credentials override it.
    anthropic_api_key: str | None = None
    api_base: str | None = None
    
    # Model configuration
    model_name: str = "gpt-4o"
    model_provider: str = "openai"
    available_models: list[ConfiguredModelOption] = Field(default_factory=_default_available_models)
    temperature: float = 0.7
    max_tokens: int = 2000
    # Runtime-only BYOK endpoint pin populated by ConfigurableLLMFactory after
    # DNS validation. It prevents DNS rebinding between validation and connect.
    byok_pinned_ip: str | None = None

    # LLM gateway provider: "langchain" (default, supports many providers via
    # init_chat_model) or "openai" (direct OpenAI Python SDK, for
    # OpenAI / OpenAI-compatible endpoints).
    llm_provider: str = "langchain"
    
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
    redis_socket_connect_timeout: float = 5.0
    redis_socket_timeout: float = Field(default=5.0, gt=0)
    redis_health_check_interval: int = 30
    redis_max_connections: int = 100
    redis_retry_attempts: int = 3
    
    # Sandbox configuration
    sandbox_provider: str = "docker"
    sandbox_address: str | None = None
    sandbox_api_port: int = 8080
    sandbox_cdp_port: int = 9222
    sandbox_vnc_port: int = 5901
    sandbox_image: str = "simpleyyt/manus-sandbox"
    sandbox_name_prefix: str | None = None
    sandbox_ttl_minutes: int | None = 30
    sandbox_network: str | None = None  # Docker network bridge name
    sandbox_chrome_args: str | None = ""
    sandbox_https_proxy: str | None = None
    sandbox_http_proxy: str | None = None
    sandbox_no_proxy: str | None = None
    # Per-container Docker resource bounds. AgentBay capacity is controlled by
    # its provider/ledger configuration instead.
    sandbox_memory_limit: str | None = "2g"
    sandbox_cpu_limit: float | None = Field(default=2.0, gt=0)
    sandbox_pids_limit: int | None = Field(default=512, ge=32)
    # Managed Docker sandbox/Claw containers are hostile execution domains.
    # Give every runtime an internal control bridge plus a one-container egress
    # bridge; only trusted backend/worker clients join the control bridge.
    # Dynamic Docker runtimes require this durable provider-visible intent.
    # Disabling it is accepted only when every enabled runtime is fixed-host
    # (or the sandbox provider is non-Docker).
    runtime_network_isolation: bool = True
    runtime_gateway_container: str | None = None
    # Stable scope for all dynamic Docker runtime names and labels.  Every
    # replica in one deployment must use the same value; deployments sharing a
    # Docker daemon must use different values.
    runtime_deployment_id: str = "ai-manus"
    # Explicit small subnets avoid exhausting Docker's much coarser default
    # user-defined bridge pools. Each live runtime consumes two subnets.
    runtime_network_address_pool: str = "10.240.0.0/12"
    runtime_network_subnet_prefix: int = Field(default=28, ge=24, le=29)
    runtime_network_gc_interval_seconds: int = Field(default=60, ge=10, le=3600)
    runtime_network_gc_grace_seconds: int = Field(default=300, ge=60, le=86400)

    # Alibaba Cloud Wuying AgentBay (SANDBOX_PROVIDER=agentbay)
    agentbay_api_key: str | None = None
    agentbay_region_id: str | None = None
    agentbay_image_id: str | None = None
    # Stable logical deployment identity used only through one-way digests in
    # provider labels and the Mongo cost ledger. It must remain unchanged
    # across replicas and ordinary releases.
    agentbay_deployment_id: str | None = None
    agentbay_quota_config_version: str = "1"
    agentbay_api_port: int = 30150
    agentbay_cdp_port: int = 30151
    agentbay_vnc_port: int = 30152
    # Conservative, cross-replica caps for billable AgentBay sessions. The
    # hard cost ledger is one majority+journaled Mongo document; Redis is not
    # authoritative for these counters.
    agentbay_max_sessions_total: int = Field(default=20, ge=1, le=20)
    agentbay_max_sessions_per_user: int = Field(default=3, ge=1)
    agentbay_quota_command_timeout_seconds: float = Field(
        default=2.0, gt=0, le=30
    )

    # Browser engine configuration
    browser_engine: str = "browser_use"  # "playwright" or "browser_use"
    tool_call_timeout_seconds: int = 180

    # Runtime topology advertised to agents.
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
    # Comma-separated exact browser origins. Wildcards are intentionally not
    # accepted because localhost deployments can otherwise be driven by any
    # malicious website open in the user's browser.
    cors_allowed_origins: str | None = None
    
    # Search engine configuration
    search_provider: str | None = "bing_web"  # "baidu", "baidu_web", "google", "bing", "bing_web", "tavily", "serper", "custom"
    bing_web_market: str = "en-US"
    bing_web_setlang: str = "en"
    baidu_search_api_key: str | None = None
    bing_search_api_key: str | None = None
    google_search_api_key: str | None = None
    google_search_engine_id: str | None = None
    tavily_api_key: str | None = None
    # Serper.dev search configuration (SEARCH_PROVIDER=serper)
    serper_api_key: str | None = None
    # Custom search API configuration (SEARCH_PROVIDER=custom)
    search_api_url: str | None = None
    search_api_key: str | None = None
    search_api_key_header: str = "Authorization"
    search_api_key_header_prefix: str = "Bearer "
    search_api_key_param: str = ""
    search_api_method: str = "POST"
    search_query_field: str = "q"
    search_result_field: str = "results"
    search_title_field: str = "title"
    search_link_field: str = "link"
    search_snippet_field: str = "snippet"
    
    # Google Analytics configuration
    google_analytics_id: str | None = None

    # Auth configuration
    auth_provider: str = "password"  # "password", "none", "local"
    registration_enabled: bool = False
    show_github_button: bool = True
    github_repository_url: str = "https://github.com/simpleyyt/ai-manus"
    password_salt: str | None = None
    password_hash_rounds: int = 600000
    # Pre-self-describing releases used one deployment-wide salt and 10 rounds.
    # Keep the historical cost separately so successful logins can migrate.
    password_legacy_hash_rounds: int = 10
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
    # Opaque external refresh tokens have no locally readable exp claim. Keep
    # revocations at least as long as the provider's documented maximum age.
    sub2api_refresh_token_max_age_days: int = 90
    auth_login_attempts_per_window: int = 10
    auth_login_ip_attempts_per_window: int = 30
    auth_login_window_seconds: int = 300
    auth_register_attempts_per_hour: int = 5
    auth_password_reset_attempts_per_hour: int = 5
    auth_refresh_attempts_per_minute: int = 60
    
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
    # JSON array. First key encrypts new BYOK credentials; all keys decrypt,
    # which permits staged rotation without coupling data to JWT signing.
    model_credential_encryption_keys: str | None = None
    
    # Extra headers for LLM requests (parsed from EXTRA_HEADERS env var, JSON)
    extra_headers: dict | None = None
    
    # Claw (OpenClaw) configuration
    claw_enabled: bool = False
    claw_image: str = "simpleyyt/manus-claw"
    claw_name_prefix: str = "manus-claw"
    claw_ttl_seconds: int = 0
    claw_network: str | None = None  # Docker network bridge name for claw containers
    claw_ready_timeout: int = 300  # Max seconds to wait for claw container to become ready
    claw_address: str | None = None  # If set, use this fixed host instead of creating Docker containers
    # Bootstrap key injected into a fixed development Claw runtime.  It is
    # accepted only when the matching owned Claw record is RUNNING; it is not a
    # process-wide proxy bypass and is never returned by the user-facing API.
    claw_api_key: str | None = None
    # Comma-separated current,previous... HMAC secrets for durable runtime-key
    # digests.  The first key signs new/rotated records; previous keys permit
    # online verification and lazy rehash during secret rotation.
    claw_api_key_hmac_keys: str | None = None
    manus_api_base_url: str = "http://backend:8000"  # URL of this backend accessible from claw containers
    claw_publish_host_ports: bool = True
    claw_host_bind_address: str = "127.0.0.1"
    claw_http_container_port: int = 18788
    claw_gateway_container_port: int = 18789
    claw_max_instances_total: int = 20
    claw_idle_timeout_seconds: int = 0
    claw_cleanup_interval_seconds: int = 60
    # Opt-out applies only to externally managed CLAW_ADDRESS runtimes.
    # Docker-created runtimes are always destroyed before ownership is deleted.
    claw_destroy_on_delete: bool = True
    claw_memory_limit: str | None = "1g"
    claw_nano_cpus: int | None = 1_000_000_000
    claw_pids_limit: int | None = 256
    # Cost/abuse limits for the Claw-only OpenAI-compatible model proxy.
    claw_proxy_max_input_bytes: int = 128 * 1024
    claw_proxy_requests_per_minute: int = 30
    claw_proxy_max_concurrent_requests: int = 2
    claw_proxy_request_lease_seconds: int = 300
    # Distributed ownership lease for one in-flight Claw chat turn.  The
    # application renews it while streaming, so the value is only the
    # fail-safe window after a crashed replica.
    claw_chat_turn_lease_seconds: int = 300
    claw_chat_max_message_bytes: int = 64 * 1024
    # Bound both the in-memory stream and the assistant message persisted in
    # the embedded Claw history document.
    claw_chat_max_response_bytes: int = Field(default=256 * 1024, ge=1)
    # A read timeout is not a whole-turn deadline: an upstream can otherwise
    # keep a response alive forever with periodic keepalives.
    claw_chat_max_duration_seconds: float = Field(default=300.0, gt=0)
    # Enforce raw SSE limits before decoding/JSON parsing.  The stream limit
    # includes framing and non-text events in addition to visible model text.
    claw_chat_max_upstream_event_bytes: int = Field(
        default=512 * 1024, ge=1
    )
    claw_chat_max_upstream_stream_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1
    )
    # Per-WebSocket subscriber memory budget.  Slow subscribers retain the
    # latest turn only and always receive that turn's terminal event.
    claw_event_queue_max_bytes: int = Field(default=512 * 1024, ge=1024)
    claw_chat_max_attachments: int = 10
    claw_chat_max_attachment_bytes: int = 25 * 1024 * 1024
    claw_chat_max_total_attachment_bytes: int = 50 * 1024 * 1024
    claw_upload_max_bytes: int = 25 * 1024 * 1024
    # One atomic Mongo update retains the newest records satisfying both the
    # count and aggregate BSON-byte budgets.  The byte ceiling deliberately
    # stays below MongoDB's 16 MiB document limit to leave room for lifecycle
    # fields and array/document overhead.
    claw_history_max_messages: int = Field(default=128, ge=1, le=128)
    claw_history_max_bytes: int = Field(
        default=CLAW_HISTORY_DEFAULT_MAX_BYTES,
        ge=1,
        le=CLAW_HISTORY_SAFE_MAX_BYTES,
    )
    # Total HTTP body cap enforced before FastAPI parses multipart uploads.
    # The extra MiB above the default per-file cap covers multipart metadata.
    multipart_upload_max_body_bytes: int = 26 * 1024 * 1024
    file_upload_max_bytes: int = 25 * 1024 * 1024
    file_storage_max_bytes_per_user: int = 1024 * 1024 * 1024
    file_storage_max_files_per_user: int = 1000

    # Main Agent chat admission and persistence bounds. The ASGI body limit is
    # enforced before FastAPI/Pydantic parse either Content-Length or chunked
    # bodies; the lower logical limits are enforced again on decoded values.
    chat_max_body_bytes: int = 256 * 1024
    chat_max_message_bytes: int = 64 * 1024
    chat_max_attachments: int = 10
    chat_attachment_file_id_max_chars: int = 256
    chat_attachment_filename_max_chars: int = 512
    chat_turn_max_payload_bytes: int = 128 * 1024
    chat_turn_max_active_per_session: int = 3
    chat_turn_max_active_per_user: int = 10
    chat_turn_claim_seconds: int = 300
    chat_turn_claim_renew_seconds: int = 60
    # Terminal submissions retain their idempotency key for this window. Active
    # submissions have no TTL and therefore cannot disappear mid-execution.
    chat_turn_terminal_retention_days: int = 30
    # One atomic Mongo update retains the newest events satisfying both count
    # and aggregate BSON-byte budgets. Session.files shares this document, so
    # the safe maximum deliberately reserves at least half of MongoDB's 16 MiB
    # ceiling for canonical file metadata and the remaining session fields.
    session_history_max_events: int = Field(default=512, ge=1, le=512)
    session_event_max_bytes: int = Field(
        default=256 * 1024,
        ge=1024,
        le=1024 * 1024,
    )
    session_history_max_bytes: int = Field(
        default=SESSION_HISTORY_DEFAULT_MAX_BYTES,
        ge=1,
        le=SESSION_HISTORY_SAFE_MAX_BYTES,
    )

    # Task backend configuration: "local" (in-process asyncio, default)
    # or "celery" (distributed Celery workers; requires running `app.worker`)
    task_backend: str = "local"
    # The local task registry only exists inside one Python process.  Declare
    # the API replica/process count so startup can reject an unsafe topology;
    # use TASK_BACKEND=celery for more than one backend process.
    backend_replica_count: int = Field(default=1, ge=1)
    # Optional custom Celery broker URL, only used when TASK_BACKEND=celery.
    # Defaults to the Redis settings above when unset.
    # e.g. "redis://:password@redis:6379/0" or "amqp://user:pass@rabbitmq:5672//"
    celery_broker_url: str | None = None

    # MCP configuration
    mcp_config_path: str = "/etc/mcp.json"
    
    # Logging configuration
    log_level: str = "INFO"
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        
    def validate(self):
        """Validate configuration settings"""
        largest_claw_history_record = max(
            int(self.claw_chat_max_message_bytes),
            int(self.claw_chat_max_response_bytes),
            int(self.claw_chat_max_upstream_event_bytes),
            int(self.claw_chat_max_upstream_stream_bytes),
        )
        if self.claw_history_max_bytes < largest_claw_history_record + 4096:
            raise ValueError(
                "CLAW_HISTORY_MAX_BYTES must exceed every permitted Claw "
                "message/response record by at least 4096 bytes"
            )
        if self.session_history_max_bytes < self.session_event_max_bytes + 4096:
            raise ValueError(
                "SESSION_HISTORY_MAX_BYTES must exceed "
                "SESSION_EVENT_MAX_BYTES by at least 4096 bytes"
            )
        try:
            runtime_pool = ipaddress.ip_network(
                self.runtime_network_address_pool,
                strict=True,
            )
        except ValueError as exc:
            raise ValueError(
                "RUNTIME_NETWORK_ADDRESS_POOL must be a canonical IPv4 CIDR"
            ) from exc
        if runtime_pool.version != 4:
            raise ValueError("RUNTIME_NETWORK_ADDRESS_POOL must be IPv4")
        if self.runtime_network_subnet_prefix <= runtime_pool.prefixlen:
            raise ValueError(
                "RUNTIME_NETWORK_SUBNET_PREFIX must be larger than the "
                "address-pool prefix"
            )
        if self.runtime_network_gc_grace_seconds < (
            self.runtime_network_gc_interval_seconds * 2
        ):
            raise ValueError(
                "RUNTIME_NETWORK_GC_GRACE_SECONDS must be at least twice "
                "RUNTIME_NETWORK_GC_INTERVAL_SECONDS"
            )
        if not self.runtime_deployment_id.strip() or len(
            self.runtime_deployment_id
        ) > 128:
            raise ValueError(
                "RUNTIME_DEPLOYMENT_ID must contain 1 to 128 characters"
            )
        task_backend = (self.task_backend or "").strip().lower()
        if task_backend not in {"local", "celery"}:
            raise ValueError("TASK_BACKEND must be either 'local' or 'celery'")
        if task_backend == "local" and self.backend_replica_count > 1:
            raise ValueError(
                "TASK_BACKEND=local only supports one backend process; set "
                "TASK_BACKEND=celery before using BACKEND_REPLICA_COUNT>1"
            )
        if self.agentbay_max_sessions_per_user > self.agentbay_max_sessions_total:
            raise ValueError(
                "AGENTBAY_MAX_SESSIONS_PER_USER cannot exceed "
                "AGENTBAY_MAX_SESSIONS_TOTAL"
            )
        sandbox_provider = (self.sandbox_provider or "").strip().lower()
        if sandbox_provider not in {"docker", "agentbay"}:
            raise ValueError(
                f"Unknown SANDBOX_PROVIDER '{sandbox_provider}' "
                "(expected 'docker' or 'agentbay')"
            )
        dynamic_docker_sandbox = (
            sandbox_provider == "docker" and not self.sandbox_address
        )
        dynamic_docker_claw = self.claw_enabled and not self.claw_address
        if (
            not self.runtime_network_isolation
            and (dynamic_docker_sandbox or dynamic_docker_claw)
        ):
            raise ValueError(
                "RUNTIME_NETWORK_ISOLATION=false is unsupported for dynamic "
                "Docker Sandbox or Claw runtimes; use fixed runtime addresses "
                "or enable isolated runtime networks"
            )
        if sandbox_provider == "agentbay":
            required_agentbay = {
                "AGENTBAY_API_KEY": self.agentbay_api_key,
                "AGENTBAY_IMAGE_ID": self.agentbay_image_id,
                "AGENTBAY_DEPLOYMENT_ID": self.agentbay_deployment_id,
            }
            missing = [
                name
                for name, value in required_agentbay.items()
                if not isinstance(value, str) or not value.strip()
            ]
            if missing:
                raise ValueError(
                    f"{', '.join(missing)} must be set when "
                    "SANDBOX_PROVIDER=agentbay"
                )
            if not self.agentbay_quota_config_version.strip():
                raise ValueError(
                    "AGENTBAY_QUOTA_CONFIG_VERSION must be non-empty"
                )

        effective_key = (
            self.anthropic_api_key or self.api_key
            if self.model_provider.lower() == "anthropic"
            else self.api_key
        )
        if not effective_key:
            raise ValueError("API key is required")

        # The same root secret protects login tokens, signed file/VNC/preview
        # capabilities, and encrypted BYOK credentials.  A known development
        # default therefore compromises every authorization boundary.  Keep a
        # narrow escape hatch only for disposable local development with auth
        # explicitly disabled.
        auth_provider = (self.auth_provider or "").strip().lower()
        deployment_environment = (
            self.deployment_environment or ""
        ).strip().lower()
        disposable_local_environment = deployment_environment in {
            "development",
            "local",
            "test",
        }
        requires_secure_jwt = (
            auth_provider != "none" or not disposable_local_environment
        )
        if requires_secure_jwt and not is_secure_jwt_secret(self.jwt_secret_key):
            raise ValueError(
                "JWT_SECRET_KEY must be a non-default secret of at least 32 bytes "
                "unless authentication is disabled in an explicit local/test/"
                "development environment"
            )

        if (
            auth_provider == "local"
            and not disposable_local_environment
            and self.local_auth_password in {"", "admin", "password", "change-me"}
        ):
            raise ValueError(
                "LOCAL_AUTH_PASSWORD must be changed from the development "
                "default outside local/test/development environments"
            )

        if (
            self.claw_enabled
            and self.claw_address
            and auth_provider not in {"none", "local"}
        ):
            raise ValueError(
                "CLAW_ADDRESS is a shared fixed runtime and is supported only "
                "with single-user AUTH_PROVIDER=none/local"
            )

        if self.claw_api_key_hmac_keys:
            hmac_keys = [
                part.strip()
                for part in self.claw_api_key_hmac_keys.split(",")
                if part.strip()
            ]
            if not hmac_keys or any(
                len(key.encode("utf-8")) < 32 for key in hmac_keys
            ):
                raise ValueError(
                    "Every CLAW_API_KEY_HMAC_KEYS entry must be at least "
                    "32 bytes"
                )

        # Parse eagerly so malformed/wildcard browser trust cannot survive to
        # application startup.
        self.get_cors_allowed_origins()

    def get_cors_allowed_origins(self) -> list[str]:
        raw_origins: list[str] = []
        if self.cors_allowed_origins:
            raw_origins.extend(self.cors_allowed_origins.split(","))
        if self.frontend_public_url:
            raw_origins.append(self.frontend_public_url)
        environment = (self.deployment_environment or "").strip().lower()
        if not raw_origins and environment in {"development", "local", "test"}:
            raw_origins.extend(
                ["http://localhost:5173", "http://127.0.0.1:5173"]
            )

        origins: list[str] = []
        for raw in raw_origins:
            origin = raw.strip().rstrip("/")
            if not origin:
                continue
            if origin == "*":
                raise ValueError("CORS_ALLOWED_ORIGINS must not contain '*'")
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "CORS_ALLOWED_ORIGINS must contain exact HTTP(S) origins"
                )
            normalized = f"{parsed.scheme}://{parsed.netloc}"
            if normalized not in origins:
                origins.append(normalized)
        return origins

@lru_cache()
def get_settings() -> Settings:
    """Get application settings"""
    settings = Settings()
    if settings.model_provider.lower() == "anthropic":
        anthropic_key = settings.anthropic_api_key or settings.api_key
        if anthropic_key and not os.environ.get("ANTHROPIC_API_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = anthropic_key
    elif settings.api_key and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = settings.api_key
    settings.extra_headers = _parse_extra_headers()
    settings.validate()
    return settings 
