import pytest
import httpx

from app.application.services.auth_service import AuthService
from app.application.services.token_service import TokenService
from app.core.config import get_settings


class _Repo:
    pass


class _FakeAsyncClient:
    responses: list[httpx.Response] = []
    requests: list[tuple[str, str, dict | None, dict | None]] = []

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None):
        self.requests.append(("GET", url, headers, None))
        return self.responses.pop(0)

    async def post(self, url, json=None):
        self.requests.append(("POST", url, None, json))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def sub2api_settings(monkeypatch):
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("AUTH_PROVIDER", "sub2api")
    monkeypatch.setenv("SUB2API_BASE_URL", "https://sub2api.example")
    get_settings.cache_clear()
    _FakeAsyncClient.responses = []
    _FakeAsyncClient.requests = []
    yield
    get_settings.cache_clear()


@pytest.fixture
def auth_service(monkeypatch):
    monkeypatch.setattr("app.application.services.auth_service.httpx.AsyncClient", _FakeAsyncClient)
    return AuthService(_Repo(), TokenService())


@pytest.mark.asyncio
async def test_verify_sub2api_token_maps_complete_user(auth_service):
    _FakeAsyncClient.responses.append(
        httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "data": {
                    "id": 123,
                    "email": "Demo@Example.com",
                    "username": "Demo User",
                    "role": "admin",
                    "status": "active",
                    "balance": 19.5,
                    "concurrency": 5,
                    "created_at": "2026-05-13T01:02:03Z",
                },
            },
        )
    )

    user = await auth_service.verify_token("sub2api-token")

    assert user is not None
    assert user.id == "sub2api:123"
    assert user.external_id == "123"
    assert user.email == "demo@example.com"
    assert user.fullname == "Demo User"
    assert user.role == "admin"
    assert user.is_active is True
    assert user.auth_provider == "sub2api"
    assert user.external_user["balance"] == 19.5
    assert _FakeAsyncClient.requests == [
        (
            "GET",
            "https://sub2api.example/api/v1/auth/me",
            {"Authorization": "Bearer sub2api-token"},
            None,
        )
    ]


@pytest.mark.asyncio
async def test_verify_sub2api_token_rejects_inactive_user(auth_service):
    _FakeAsyncClient.responses.append(
        httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "id": 456,
                    "email": "inactive@example.com",
                    "username": "Inactive",
                    "role": "user",
                    "status": "disabled",
                },
            },
        )
    )

    user = await auth_service.verify_token("sub2api-token")

    assert user is not None
    assert user.id == "sub2api:456"
    assert user.is_active is False


@pytest.mark.asyncio
async def test_verify_sub2api_token_returns_none_on_auth_failure(auth_service):
    _FakeAsyncClient.responses.append(httpx.Response(401, json={"code": 401, "message": "unauthorized"}))

    assert await auth_service.verify_token("bad-token") is None


@pytest.mark.asyncio
async def test_refresh_sub2api_token_passes_through_rotated_refresh_token(auth_service):
    _FakeAsyncClient.responses.append(
        httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            },
        )
    )

    result = await auth_service.refresh_access_token("old-refresh")

    assert result.access_token == "new-access"
    assert result.refresh_token == "new-refresh"
    assert result.token_type == "bearer"
    assert _FakeAsyncClient.requests == [
        (
            "POST",
            "https://sub2api.example/api/v1/auth/refresh",
            None,
            {"refresh_token": "old-refresh"},
        )
    ]
