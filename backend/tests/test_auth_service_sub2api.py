import asyncio
from http.cookies import SimpleCookie
import pytest
import httpx
from types import SimpleNamespace
from fastapi import Response
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.application.services.auth_service import AuthService
from app.application.services.token_service import TokenService
from app.core.config import get_settings
from app.domain.models.auth_session import AuthClientType
from app.interfaces.api.auth_routes import get_current_user_info, refresh_token
from app.interfaces.schemas.auth import RefreshTokenRequest


class _Repo:
    pass


class _SessionStore:
    def __init__(self):
        self.sessions = {}
        self.generations = {}

    async def get_user_generation(self, user_id):
        return self.generations.get(user_id, 0)

    async def create(
        self, session, ttl_seconds, *, expected_generation=None
    ):
        generation = (
            session.revocation_generation
            if expected_generation is None
            else expected_generation
        )
        if self.generations.get(session.user_id, 0) != generation:
            return False
        self.sessions[session.session_id] = session
        return True

    async def get(self, session_id):
        return self.sessions.get(session_id)

    async def touch(self, session_id, ttl_seconds):
        session = self.sessions.get(session_id)
        if session and (
            session.revocation_generation
            == self.generations.get(session.user_id, 0)
        ):
            return session
        return None

    async def rotate(
        self,
        old_session_id,
        session,
        ttl_seconds,
        *,
        expected_generation,
    ):
        old = self.sessions.get(old_session_id)
        if not old or self.generations.get(session.user_id, 0) != expected_generation:
            return False
        del self.sessions[old_session_id]
        self.sessions[session.session_id] = session
        return True

    async def delete(self, session_id):
        return self.sessions.pop(session_id, None) is not None

    async def delete_all_for_user(self, user_id):
        self.generations[user_id] = self.generations.get(user_id, 0) + 1
        ids = [
            session_id
            for session_id, session in self.sessions.items()
            if session.user_id == user_id
        ]
        for session_id in ids:
            del self.sessions[session_id]
        return len(ids)


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
def session_store():
    return _SessionStore()


@pytest.fixture
def auth_service(monkeypatch, session_store):
    monkeypatch.setattr("app.application.services.auth_service.httpx.AsyncClient", _FakeAsyncClient)
    return AuthService(_Repo(), TokenService(), session_store)


def _request(*, method="GET", cookie=None):
    headers = []
    if cookie:
        headers.append((b"cookie", cookie.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/api/v1/auth/me",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
        }
    )


def _sub2api_user_payload(user_id=123):
    return {
        "code": 0,
        "message": "success",
        "data": {
            "id": user_id,
            "email": "Demo@Example.com",
            "username": "Demo User",
            "role": "admin",
            "status": "active",
        },
    }


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
async def test_auth_me_issues_local_cookie_without_provider_token(
    auth_service, session_store
):
    user = auth_service._map_sub2api_user(_sub2api_user_payload()["data"])
    response = Response()

    result = await get_current_user_info(
        response=response,
        http_request=_request(),
        current_user=user,
        bearer_credentials=HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="provider-access-secret"
        ),
        auth_service=auth_service,
    )

    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    session_id = cookie[get_settings().session_cookie_name].value
    assert session_id != "provider-access-secret"
    assert "provider-access-secret" not in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert session_store.sessions[session_id].user_snapshot["id"] == "sub2api:123"
    assert "provider-access-secret" not in session_store.sessions[session_id].model_dump_json()
    assert result.data.id == "sub2api:123"

    # Native WebSockets resolve only the opaque Cookie session and do not need
    # an Authorization header or another provider round-trip.
    resolved_user = await auth_service.verify_token(session_id)
    assert resolved_user is not None
    assert resolved_user.id == "sub2api:123"


@pytest.mark.asyncio
async def test_sub2api_refresh_rotates_local_cookie_but_returns_provider_tokens(
    auth_service, session_store
):
    user = auth_service._map_sub2api_user(_sub2api_user_payload()["data"])
    old_session = await auth_service.create_auth_session(
        user, client=AuthClientType.WEB
    )
    _FakeAsyncClient.responses.extend(
        [
            httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "access_token": "new-provider-access",
                        "refresh_token": "new-provider-refresh",
                        "token_type": "Bearer",
                    },
                },
            ),
            httpx.Response(200, json=_sub2api_user_payload()),
        ]
    )
    response = Response()
    cookie_name = get_settings().session_cookie_name

    result = await refresh_token(
        response=response,
        http_request=_request(
            method="POST", cookie=f"{cookie_name}={old_session.session_id}"
        ),
        request=RefreshTokenRequest(refresh_token="old-provider-refresh"),
        bearer_credentials=None,
        auth_service=auth_service,
    )

    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    new_session_id = cookie[cookie_name].value
    assert result.data.access_token == "new-provider-access"
    assert result.data.refresh_token == "new-provider-refresh"
    assert new_session_id not in {old_session.session_id, "new-provider-access"}
    assert old_session.session_id not in session_store.sessions
    assert new_session_id in session_store.sessions
    assert "new-provider-access" not in response.headers["set-cookie"]
    assert "new-provider-refresh" not in response.headers["set-cookie"]


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

    assert await auth_service.verify_token("sub2api-token") is None


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
async def test_logout_also_revokes_sub2api_opaque_cookie_session(
    auth_service, session_store
):
    user = auth_service._map_sub2api_user(_sub2api_user_payload()["data"])
    session = await auth_service.create_auth_session(user)

    assert await auth_service.logout(
        "access-token",
        refresh_token="refresh-token",
        cookie_session_id=session.session_id,
    ) is True
    assert session.session_id not in session_store.sessions


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
