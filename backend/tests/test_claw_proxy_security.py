"""Security regression tests for the Claw-only model proxy capability."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Optional

import pytest
from starlette.requests import Request

from app.domain.models.claw import Claw, ClawStatus
from app.domain.services.claw_domain_service import ClawDomainService
from app.application.services.claw_service import ClawService
from app.core.config import get_settings
from app.interfaces.api import openai_routes
from app.interfaces.api.claw_routes import router as claw_router


@pytest.fixture(autouse=True)
def secure_unit_test_settings(monkeypatch):
    monkeypatch.setenv(
        "JWT_SECRET_KEY", "claw-proxy-tests-only-secret-at-least-32-bytes"
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _Repository:
    def __init__(self, claw: Optional[Claw] = None):
        self.claw = claw

    async def get_by_api_key(self, api_key: str) -> Optional[Claw]:
        if self.claw and self.claw.api_key == api_key:
            return self.claw
        return None

    async def get_by_user_id(self, user_id: str) -> Optional[Claw]:
        if self.claw and self.claw.user_id == user_id:
            return self.claw
        return None

    async def count_by_statuses(self, statuses) -> int:
        return int(bool(self.claw and self.claw.status in statuses))

    async def create(self, claw: Claw) -> Claw:
        self.claw = claw
        return claw

    async def update(self, claw: Claw) -> Claw:
        self.claw = claw
        return claw


def _claw(**overrides) -> Claw:
    values = {
        "id": "claw-1",
        "user_id": "user-1",
        "api_key": "runtime-secret",
        "status": ClawStatus.RUNNING,
        "container_ip": "10.0.0.5",
    }
    values.update(overrides)
    return Claw(**values)


def _domain(claw: Optional[Claw]) -> ClawDomainService:
    return ClawDomainService(
        _Repository(claw),
        claw_runtime=SimpleNamespace(),
        claw_client=SimpleNamespace(),
    )


def _request(
    raw_body: bytes,
    *,
    token: str = "runtime-secret",
    content_length: Optional[int] = None,
) -> Request:
    headers = [
        (b"authorization", f"Bearer {token}".encode()),
        (b"content-type", b"application/json"),
    ]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    return Request(scope, receive)


def _json_request(body: dict, **kwargs) -> Request:
    return _request(json.dumps(body).encode(), **kwargs)


def _response_json(response) -> dict:
    return json.loads(response.body.decode())


class _ProxyClawService:
    def __init__(
        self,
        user_id: Optional[str] = "user-1",
        quota_token: Optional[str] = "lease-1",
    ):
        self.user_id = user_id
        self.quota_token = quota_token
        self.released = []

    async def verify_api_key(self, api_key: str) -> Optional[str]:
        return self.user_id if api_key == "runtime-secret" else None

    async def acquire_proxy_quota(self, user_id: str) -> Optional[str]:
        return self.quota_token

    async def release_proxy_quota(self, user_id: str, lease_token: str) -> None:
        self.released.append((user_id, lease_token))


class _QuotaRedis:
    def __init__(self, result=1, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def eval(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.result


def test_user_facing_claw_api_key_endpoint_is_removed():
    paths = {getattr(route, "path", None) for route in claw_router.routes}
    assert "/claw/api-key" not in paths


async def test_proxy_key_fails_closed_when_claw_feature_is_disabled(monkeypatch):
    service = _domain(_claw())
    monkeypatch.setattr(service.settings, "claw_enabled", False)

    assert await service.verify_api_key("runtime-secret") is None


@pytest.mark.parametrize(
    "claw",
    [
        None,
        _claw(status=ClawStatus.STOPPED),
        _claw(status=ClawStatus.CREATING),
        _claw(container_ip=None),
    ],
)
async def test_proxy_key_requires_a_running_provisioned_owned_claw(
    monkeypatch, claw,
):
    service = _domain(claw)
    monkeypatch.setattr(service.settings, "claw_enabled", True)

    assert await service.verify_api_key("runtime-secret") is None


async def test_static_setting_is_not_a_process_wide_proxy_bypass(monkeypatch):
    service = _domain(_claw(api_key="owned-secret"))
    monkeypatch.setattr(service.settings, "claw_enabled", True)
    monkeypatch.setattr(service.settings, "claw_api_key", "static-secret")

    assert await service.verify_api_key("static-secret") is None
    assert await service.verify_api_key("owned-secret") == "user-1"


async def test_proxy_key_reconciles_legacy_expiry_when_ttl_is_disabled(
    monkeypatch,
):
    claw = _claw(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    service = _domain(claw)
    monkeypatch.setattr(service.settings, "claw_enabled", True)
    monkeypatch.setattr(service.settings, "claw_ttl_seconds", 0)

    assert await service.verify_api_key("runtime-secret") == "user-1"
    assert claw.expires_at is None


async def test_fixed_runtime_bootstrap_key_is_bound_to_its_running_record(
    monkeypatch,
):
    repository = _Repository()
    service = ClawDomainService(
        repository,
        claw_runtime=SimpleNamespace(),
        claw_client=SimpleNamespace(),
    )
    monkeypatch.setattr(service.settings, "claw_enabled", True)
    monkeypatch.setattr(service.settings, "claw_address", "fixed-claw:18788")
    monkeypatch.setattr(service.settings, "claw_api_key", "fixed-secret")
    monkeypatch.setattr(service.settings, "auth_provider", "none")

    claw = await service.prepare_claw_for_creation("anonymous")
    assert claw.api_key == "fixed-secret"
    assert await service.verify_api_key("fixed-secret") is None

    claw.status = ClawStatus.RUNNING
    claw.container_ip = "fixed-claw:18788"
    await repository.update(claw)
    assert await service.verify_api_key("fixed-secret") == "anonymous"


async def test_fixed_runtime_rejects_stale_or_wrong_owner_records(monkeypatch):
    service = _domain(_claw(user_id="legacy-user", api_key="fixed-secret"))
    monkeypatch.setattr(service.settings, "claw_enabled", True)
    monkeypatch.setattr(service.settings, "claw_address", "fixed-claw:18788")
    monkeypatch.setattr(service.settings, "claw_api_key", "fixed-secret")
    monkeypatch.setattr(service.settings, "auth_provider", "none")

    assert await service.verify_api_key("fixed-secret") is None
    with pytest.raises(RuntimeError, match="single-user account"):
        await service.prepare_claw_for_creation("legacy-user")


async def test_fixed_runtime_fails_closed_in_multi_tenant_auth_mode(monkeypatch):
    service = _domain(_claw(api_key="fixed-secret"))
    monkeypatch.setattr(service.settings, "claw_enabled", True)
    monkeypatch.setattr(service.settings, "claw_address", "fixed-claw:18788")
    monkeypatch.setattr(service.settings, "claw_api_key", "fixed-secret")
    monkeypatch.setattr(service.settings, "auth_provider", "password")

    assert await service.verify_api_key("fixed-secret") is None
    with pytest.raises(RuntimeError, match="single-user"):
        await service.prepare_claw_for_creation("another-user")


async def test_proxy_rejects_key_without_an_owned_instance(monkeypatch):
    async def get_service():
        return _ProxyClawService(user_id=None)

    async def must_not_call_upstream(*args, **kwargs):
        raise AssertionError("unauthorized requests must not reach the LLM")

    monkeypatch.setattr(openai_routes, "_get_claw_service", get_service)
    monkeypatch.setattr(openai_routes, "_get_llm_response", must_not_call_upstream)
    response = await openai_routes.chat_completions(
        _json_request({"model": "anything", "messages": [{"role": "user", "content": "hi"}]})
    )

    assert response.status_code == 401


async def test_proxy_forces_configured_model_provider_and_token_cap(monkeypatch):
    captured = {}
    settings = SimpleNamespace(
        model_name="deployment-cheap-model",
        model_provider="openai",
        max_tokens=512,
        claw_proxy_max_input_bytes=128 * 1024,
    )

    async def get_service():
        return _ProxyClawService()

    async def capture_upstream(body, actual_settings):
        captured["body"] = body
        captured["settings"] = actual_settings
        return {"id": "ok", "choices": []}

    monkeypatch.setattr(openai_routes, "_get_claw_service", get_service)
    monkeypatch.setattr(openai_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(openai_routes, "_get_llm_response", capture_upstream)

    response = await openai_routes.chat_completions(
        _json_request({
            "model": "most-expensive-model",
            "model_provider": "attacker-provider",
            "provider": "attacker-provider",
            "api_base": "https://attacker.invalid/v1",
            "api_key": "attacker-key",
            "service_tier": "priority",
            "extra_body": {"model": "hidden-expensive-model"},
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 999_999,
            "n": 1,
        })
    )

    assert response.status_code == 200
    assert captured["settings"] is settings
    assert captured["body"]["model"] == "deployment-cheap-model"
    assert captured["body"]["max_tokens"] == 512
    assert captured["body"]["n"] == 1
    for forbidden in (
        "model_provider", "provider", "api_base", "api_key",
        "service_tier", "extra_body",
    ):
        assert forbidden not in captured["body"]


async def test_proxy_denies_when_distributed_quota_is_unavailable(monkeypatch):
    async def get_service():
        return _ProxyClawService(quota_token=None)

    async def must_not_call_upstream(*args, **kwargs):
        raise AssertionError("quota-denied requests must not reach the LLM")

    monkeypatch.setattr(openai_routes, "_get_claw_service", get_service)
    monkeypatch.setattr(openai_routes, "_get_llm_response", must_not_call_upstream)
    response = await openai_routes.chat_completions(
        _json_request({
            "model": "default",
            "messages": [{"role": "user", "content": "hi"}],
        })
    )

    assert response.status_code == 429


async def test_distributed_proxy_quota_acquires_and_releases_lease(monkeypatch):
    redis = _QuotaRedis()
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: SimpleNamespace(client=redis),
    )
    service = ClawService(SimpleNamespace(claw_repository=SimpleNamespace()))

    lease = await service.acquire_proxy_quota("user-1")
    assert lease
    acquire_call = redis.calls[0]
    assert acquire_call[1] == 2
    assert acquire_call[2].startswith("claw:proxy:{user-1}:rate:")
    assert acquire_call[3] == "claw:proxy:{user-1}:active"

    await service.release_proxy_quota("user-1", lease)
    release_call = redis.calls[1]
    assert release_call[1:] == (
        1,
        "claw:proxy:{user-1}:active",
        lease,
    )


async def test_distributed_proxy_quota_fails_closed_on_redis_error(monkeypatch):
    redis = _QuotaRedis(error=ConnectionError("redis offline"))
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: SimpleNamespace(client=redis),
    )
    service = ClawService(SimpleNamespace(claw_repository=SimpleNamespace()))

    assert await service.acquire_proxy_quota("user-1") is None


async def test_proxy_rejects_prompt_over_input_budget_and_releases_lease(
    monkeypatch,
):
    service = _ProxyClawService()
    settings = SimpleNamespace(
        model_name="deployment-model",
        model_provider="openai",
        max_tokens=512,
        claw_proxy_max_input_bytes=1024,
    )

    async def get_service():
        return service

    monkeypatch.setattr(openai_routes, "_get_claw_service", get_service)
    monkeypatch.setattr(openai_routes, "get_settings", lambda: settings)
    response = await openai_routes.chat_completions(
        _json_request({
            "model": "default",
            "messages": [{"role": "user", "content": "x" * 2048}],
        })
    )

    assert response.status_code == 413
    assert service.released == [("user-1", "lease-1")]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("n", 2),
        ("best_of", 2),
        ("candidate_count", 2),
        ("num_return_sequences", 2),
    ],
)
async def test_proxy_rejects_multi_completion_cost_multipliers(
    monkeypatch, field_name, value,
):
    async def get_service():
        return _ProxyClawService()

    async def must_not_call_upstream(*args, **kwargs):
        raise AssertionError("invalid requests must not reach the LLM")

    monkeypatch.setattr(openai_routes, "_get_claw_service", get_service)
    monkeypatch.setattr(openai_routes, "_get_llm_response", must_not_call_upstream)
    response = await openai_routes.chat_completions(
        _json_request({
            "model": "default",
            "messages": [{"role": "user", "content": "hi"}],
            field_name: value,
        })
    )

    assert response.status_code == 400
    assert field_name in _response_json(response)["error"]["message"]


async def test_proxy_rejects_excessive_message_count(monkeypatch):
    async def get_service():
        return _ProxyClawService()

    monkeypatch.setattr(openai_routes, "_get_claw_service", get_service)
    response = await openai_routes.chat_completions(
        _json_request({
            "model": "default",
            "messages": [
                {"role": "user", "content": "x"}
                for _ in range(openai_routes.CLAW_PROXY_MAX_MESSAGES + 1)
            ],
        })
    )

    assert response.status_code == 400


async def test_proxy_rejects_declared_or_streamed_oversized_body(monkeypatch):
    async def get_service():
        return _ProxyClawService()

    monkeypatch.setattr(openai_routes, "_get_claw_service", get_service)

    declared = await openai_routes.chat_completions(
        _request(
            b"{}",
            content_length=openai_routes.CLAW_PROXY_MAX_REQUEST_BYTES + 1,
        )
    )
    assert declared.status_code == 413

    monkeypatch.setattr(openai_routes, "CLAW_PROXY_MAX_REQUEST_BYTES", 32)
    streamed = await openai_routes.chat_completions(
        _request(b'{"messages":[{"role":"user","content":"too large"}]}')
    )
    assert streamed.status_code == 413
