import pytest
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings, is_secure_jwt_secret


STRONG_SECRET = "test-only-jwt-root-secret-at-least-32-bytes"


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "api_key": "test-model-key",
        "auth_provider": "password",
        "deployment_environment": "development",
        "jwt_secret_key": STRONG_SECRET,
        "frontend_public_url": None,
        "cors_allowed_origins": None,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    "secret",
    ["", "your-secret-key-here", "change-me", "secret", "too-short"],
)
def test_known_or_short_jwt_secrets_are_not_secure(secret):
    assert is_secure_jwt_secret(secret) is False


def test_auth_enabled_rejects_default_jwt_secret():
    settings = _settings(jwt_secret_key="your-secret-key-here")

    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        settings.validate()


def test_sub2api_auth_rejects_default_jwt_secret():
    settings = _settings(
        auth_provider="sub2api",
        jwt_secret_key="your-secret-key-here",
    )

    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        settings.validate()


def test_disposable_auth_none_development_allows_default_jwt_secret():
    settings = _settings(
        auth_provider="none",
        deployment_environment="development",
        jwt_secret_key="your-secret-key-here",
    )

    settings.validate()


@pytest.mark.parametrize(
    "environment",
    ["staging", "production", "PRODUCTION", "prod", "preprod", "qa", ""],
)
def test_non_disposable_environment_rejects_default_even_without_auth(environment):
    settings = _settings(
        auth_provider="none",
        deployment_environment=environment,
        jwt_secret_key="your-secret-key-here",
    )

    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        settings.validate()


def test_auth_enabled_accepts_strong_jwt_secret():
    settings = _settings()

    settings.validate()


def test_config_rejects_unsafe_or_unknown_task_backend_topology():
    with pytest.raises(ValueError, match="TASK_BACKEND=celery"):
        _settings(task_backend="local", backend_replica_count=2).validate()

    with pytest.raises(ValueError, match="either 'local' or 'celery'"):
        _settings(task_backend="typo").validate()

    _settings(task_backend="celery", backend_replica_count=2).validate()


def test_agentbay_per_user_limit_cannot_exceed_global_cost_cap():
    with pytest.raises(ValueError, match="AGENTBAY_MAX_SESSIONS_PER_USER"):
        _settings(
            agentbay_max_sessions_total=2,
            agentbay_max_sessions_per_user=3,
        ).validate()


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("agentbay_api_key", "AGENTBAY_API_KEY"),
        ("agentbay_image_id", "AGENTBAY_IMAGE_ID"),
        ("agentbay_deployment_id", "AGENTBAY_DEPLOYMENT_ID"),
    ],
)
def test_agentbay_provider_requires_every_durable_identity_field(field, message):
    values = {
        "sandbox_provider": "agentbay",
        "agentbay_api_key": "provider-key",
        "agentbay_image_id": "image-id",
        "agentbay_deployment_id": "deployment-id",
    }
    values[field] = ""

    with pytest.raises(ValueError, match=message):
        _settings(**values).validate()


def test_agentbay_global_cost_cap_has_a_hard_maximum_of_twenty():
    with pytest.raises(ValueError, match="less than or equal to 20"):
        _settings(agentbay_max_sessions_total=21)


def test_agentbay_valid_configuration_passes_startup_validation():
    _settings(
        sandbox_provider="agentbay",
        agentbay_api_key="provider-key",
        agentbay_image_id="image-id",
        agentbay_deployment_id="deployment-id",
    ).validate()


def test_claw_hmac_keyring_rejects_weak_rotation_keys():
    settings = _settings(
        claw_api_key_hmac_keys=(
            "current-secret-at-least-32-bytes,too-short"
        )
    )

    with pytest.raises(ValueError, match="CLAW_API_KEY_HMAC_KEYS"):
        settings.validate()


def test_cors_origins_are_exact_and_wildcards_are_rejected():
    settings = _settings(
        cors_allowed_origins=(
            "https://manus.example.com,http://localhost:5173/"
        )
    )
    assert settings.get_cors_allowed_origins() == [
        "https://manus.example.com",
        "http://localhost:5173",
    ]

    with pytest.raises(ValueError, match="must not contain"):
        _settings(cors_allowed_origins="*").validate()
    with pytest.raises(ValueError, match="exact HTTP"):
        _settings(cors_allowed_origins="https://example.com/path").validate()


@pytest.mark.asyncio
async def test_auth_none_localhost_cannot_be_driven_by_arbitrary_web_origin():
    settings = _settings(
        auth_provider="none",
        deployment_environment="development",
        cors_allowed_origins="http://localhost:5173",
        frontend_public_url=None,
    )
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.get_cors_allowed_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/resource")
    async def resource():
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backend.local"
    ) as client:
        rejected = await client.options(
            "/resource",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        allowed = await client.options(
            "/resource",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
