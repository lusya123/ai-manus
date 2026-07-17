import asyncio
import pytest
import httpx
from types import SimpleNamespace

from app.application.services.auth_service import AuthService
from app.application.services.token_service import TokenService
from app.core.config import get_settings


class _Repo:
    pass


class _FakeAsyncClient:
    responses: list[httpx.Response] = []
    requests: list[tuple[str, str, dict | None, dict | None]] = []
    post_started: asyncio.Event | None = None
    post_continue: asyncio.Event | None = None

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
        if self.post_started is not None:
            self.post_started.set()
        if self.post_continue is not None:
            await self.post_continue.wait()
        return self.responses.pop(0)


class _FakeRedis:
    values: dict[str, str] = {}
    expirations: dict[str, int] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = int(ex)
        return True

    async def exists(self, key):
        return key in self.values

    async def get(self, key):
        return self.values.get(key)

    async def eval(self, script, numkeys, *items):
        if "sub2api-rotation-commit-v1" in script:
            keys = items[:numkeys]
            family_id, ttl, has_refresh = items[numkeys:]
            ttl = int(ttl)
            self.values[keys[0]] = family_id
            self.expirations[keys[0]] = ttl
            if has_refresh == "1":
                self.values[keys[1]] = family_id
                self.expirations[keys[1]] = ttl
            self.values[keys[5]] = "used"
            self.expirations[keys[5]] = ttl
            if keys[2] in self.values:
                self.values[keys[3]] = "1"
                self.expirations[keys[3]] = ttl
                if has_refresh == "1":
                    self.values[keys[4]] = "1"
                    self.expirations[keys[4]] = ttl
                return 0
            return 1
        key, owner = items
        if self.values.get(key) == owner:
            self.values.pop(key, None)
            self.expirations.pop(key, None)
            return 1
        return 0


@pytest.fixture(autouse=True)
def sub2api_settings(monkeypatch):
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("AUTH_PROVIDER", "sub2api")
    monkeypatch.setenv(
        "JWT_SECRET_KEY", "test-only-sub2api-jwt-secret-at-least-32-bytes"
    )
    monkeypatch.setenv("SUB2API_BASE_URL", "https://sub2api.example")
    get_settings.cache_clear()
    _FakeAsyncClient.responses = []
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.post_started = None
    _FakeAsyncClient.post_continue = None
    _FakeRedis.values = {}
    _FakeRedis.expirations = {}
    monkeypatch.setattr(
        "app.infrastructure.storage.redis.get_redis",
        lambda: SimpleNamespace(client=_FakeRedis()),
    )
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
    with pytest.raises(Exception, match="Invalid refresh token"):
        await auth_service.refresh_access_token("old-refresh")
    assert len(_FakeAsyncClient.requests) == 1


@pytest.mark.asyncio
async def test_logout_revokes_sub2api_access_and_refresh_locally(auth_service):
    assert await auth_service.logout(
        "logged-out-token", refresh_token="logged-out-refresh"
    ) is True
    assert await auth_service.verify_token("logged-out-token") is None
    with pytest.raises(Exception, match="Invalid refresh token"):
        await auth_service.refresh_access_token("logged-out-refresh")
    assert _FakeAsyncClient.requests == []
    assert min(_FakeRedis.expirations.values()) >= 90 * 24 * 60 * 60


@pytest.mark.asyncio
async def test_logout_requires_sub2api_refresh_token(auth_service):
    with pytest.raises(Exception, match="refresh_token is required"):
        await auth_service.logout("access-only")


@pytest.mark.asyncio
async def test_concurrent_sub2api_refresh_and_logout_cannot_resurrect(auth_service):
    _FakeAsyncClient.responses.append(
        httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                },
            },
        )
    )
    _FakeAsyncClient.post_started = asyncio.Event()
    _FakeAsyncClient.post_continue = asyncio.Event()

    refresh_task = asyncio.create_task(
        auth_service.refresh_access_token("old-refresh")
    )
    await _FakeAsyncClient.post_started.wait()
    await auth_service.logout("old-access", refresh_token="old-refresh")
    _FakeAsyncClient.post_continue.set()

    with pytest.raises(Exception, match="logged out during refresh"):
        await refresh_task
    assert await auth_service.verify_token("new-access") is None
    with pytest.raises(Exception, match="Invalid refresh token"):
        await auth_service.refresh_access_token("new-refresh")
