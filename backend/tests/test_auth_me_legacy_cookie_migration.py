import asyncio
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from fastapi import Response
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.application.services.auth_service import AuthService
from app.application.services.token_service import TokenService
from app.core.config import get_settings
from app.domain.models.auth_session import AuthSession
from app.domain.models.user import User, UserRole
from app.interfaces.api.auth_routes import get_current_user_info


class _Repo:
    def __init__(self, user: User):
        self.user = user

    async def get_user_by_id(self, user_id: str):
        return self.user if user_id == self.user.id else None


class _SessionStore:
    def __init__(self):
        self.sessions: dict[str, AuthSession] = {}
        self.generations: dict[str, int] = {}

    async def get_user_generation(self, user_id: str) -> int:
        return self.generations.get(user_id, 0)

    async def create(
        self,
        session: AuthSession,
        ttl_seconds: int,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        del ttl_seconds
        expected = (
            session.revocation_generation
            if expected_generation is None
            else expected_generation
        )
        if self.generations.get(session.user_id, 0) != expected:
            return False
        self.sessions[session.session_id] = session
        return True

    async def get(self, session_id: str):
        session = self.sessions.get(session_id)
        if session and (
            session.revocation_generation
            != self.generations.get(session.user_id, 0)
        ):
            self.sessions.pop(session_id, None)
            return None
        return session

    async def touch(self, session_id: str, ttl_seconds: int):
        session = await self.get(session_id)
        if session:
            now = datetime.now(UTC)
            session.last_seen_at = now
            session.expires_at = now + timedelta(seconds=ttl_seconds)
        return session

    async def rotate(
        self,
        old_session_id: str,
        session: AuthSession,
        ttl_seconds: int,
        *,
        expected_generation: int,
    ) -> bool:
        del ttl_seconds
        old = await self.get(old_session_id)
        if (
            old is None
            or old.user_id != session.user_id
            or self.generations.get(session.user_id, 0) != expected_generation
        ):
            return False
        self.sessions.pop(old_session_id, None)
        self.sessions[session.session_id] = session
        return True

    async def delete(self, session_id: str) -> bool:
        return self.sessions.pop(session_id, None) is not None

    async def delete_all_for_user(self, user_id: str) -> int:
        self.generations[user_id] = self.generations.get(user_id, 0) + 1
        session_ids = [
            session_id
            for session_id, session in self.sessions.items()
            if session.user_id == user_id
        ]
        for session_id in session_ids:
            self.sessions.pop(session_id, None)
        return len(session_ids)

    async def list_ids_for_user(self, user_id: str) -> list[str]:
        return [
            session_id
            for session_id, session in self.sessions.items()
            if session.user_id == user_id
        ]


class _Redis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def exists(self, key: str):
        return key in self.values

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, ex=None, nx=False):
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script: str, numkeys: int, *items):
        keys = items[:numkeys]
        args = items[numkeys:]
        if "jwt-browser-session-claim-v1" not in script:
            raise AssertionError("unexpected Redis script")
        revoked_key, mapping_key = keys
        candidate, _ttl, replace_session_id = args
        if revoked_key in self.values:
            return ""
        current = self.values.get(mapping_key)
        if current:
            if replace_session_id and current == replace_session_id:
                self.values[mapping_key] = candidate
                return candidate
            return current
        self.values[mapping_key] = candidate
        return candidate


def _request(*, cookie: str | None = None) -> Request:
    headers = [(b"user-agent", b"legacy-browser")]
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/api/v1/auth/me",
            "raw_path": b"/api/v1/auth/me",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("manus.example", 443),
        }
    )


def _cookie_value(response: Response) -> str | None:
    header = response.headers.get("set-cookie")
    if not header:
        return None
    parsed = SimpleCookie()
    parsed.load(header)
    return parsed[get_settings().session_cookie_name].value


@pytest.fixture(params=["password", "local"])
def auth_context(monkeypatch, request):
    provider = request.param
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("AUTH_PROVIDER", provider)
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "test")
    monkeypatch.setenv(
        "JWT_SECRET_KEY", "test-only-legacy-cookie-secret-at-least-32-bytes"
    )
    get_settings.cache_clear()

    user = User(
        id="user-1" if provider == "password" else "local_admin",
        fullname="Legacy User",
        email="legacy@example.com",
        role=UserRole.ADMIN if provider == "local" else UserRole.USER,
        is_active=True,
        auth_provider=provider,
    )
    store = _SessionStore()
    redis = _Redis()
    monkeypatch.setattr(
        "app.infrastructure.storage.redis.get_redis",
        lambda: SimpleNamespace(client=redis),
    )
    service = AuthService(_Repo(user), TokenService(), store)
    access_token, _refresh_token = service.token_service.create_token_pair(user)
    yield SimpleNamespace(
        provider=provider,
        user=user,
        store=store,
        redis=redis,
        service=service,
        access_token=access_token,
    )
    get_settings.cache_clear()


async def _auth_me(context, response: Response, request: Request | None = None):
    return await get_current_user_info(
        response=response,
        http_request=request or _request(),
        current_user=context.user,
        bearer_credentials=HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=context.access_token
        ),
        auth_service=context.service,
    )


@pytest.mark.asyncio
async def test_auth_me_exchanges_legacy_access_jwt_for_http_only_cookie(auth_context):
    response = Response()

    result = await _auth_me(auth_context, response)

    session_id = _cookie_value(response)
    assert session_id
    assert session_id != auth_context.access_token
    assert "HttpOnly" in response.headers["set-cookie"]
    assert result.data.id == auth_context.user.id
    session = auth_context.store.sessions[session_id]
    payload = auth_context.service.token_service.verify_token(
        auth_context.access_token, expected_type="access"
    )
    assert session.rotated_from == f"jwt-family:{payload['sid']}"
    resolved = await auth_context.service.resolve_session_token(session_id)
    assert resolved is not None
    assert resolved.user_id == auth_context.user.id


@pytest.mark.asyncio
async def test_auth_me_does_not_remint_an_opaque_bearer_session(auth_context):
    opaque = await auth_context.service.create_auth_session(auth_context.user)
    auth_context.access_token = opaque.session_id
    response = Response()

    await _auth_me(auth_context, response)

    assert _cookie_value(response) is None
    assert list(auth_context.store.sessions) == [opaque.session_id]


@pytest.mark.asyncio
async def test_concurrent_legacy_exchanges_reuse_one_family_winner(auth_context):
    first = Response()
    second = Response()

    await asyncio.gather(
        _auth_me(auth_context, first),
        _auth_me(auth_context, second),
    )

    assert _cookie_value(first) == _cookie_value(second)
    assert list(auth_context.store.sessions) == [_cookie_value(first)]


@pytest.mark.asyncio
async def test_legacy_exchange_replaces_only_a_proven_stale_mapping(auth_context):
    payload = auth_context.service.token_service.verify_token(
        auth_context.access_token, expected_type="access"
    )
    mapping_key = auth_context.service._jwt_migrated_session_key(payload["sid"])
    auth_context.redis.values[mapping_key] = "expired-session"
    response = Response()

    await _auth_me(auth_context, response)

    session_id = _cookie_value(response)
    assert session_id and session_id != "expired-session"
    assert auth_context.redis.values[mapping_key] == session_id
    assert list(auth_context.store.sessions) == [session_id]


@pytest.mark.asyncio
async def test_revoked_legacy_family_never_creates_a_browser_session(auth_context):
    payload = auth_context.service.token_service.verify_token(
        auth_context.access_token, expected_type="access"
    )
    auth_context.redis.values[
        auth_context.service._jwt_family_revocation_key(payload["sid"])
    ] = "1"
    response = Response()

    await _auth_me(auth_context, response)

    assert _cookie_value(response) is None
    assert auth_context.store.sessions == {}


@pytest.mark.asyncio
async def test_existing_same_user_cookie_avoids_an_extra_migration(auth_context):
    existing = await auth_context.service.create_auth_session(auth_context.user)
    cookie_name = get_settings().session_cookie_name
    response = Response()

    await _auth_me(
        auth_context,
        response,
        _request(cookie=f"{cookie_name}={existing.session_id}"),
    )

    assert _cookie_value(response) is None
    assert list(auth_context.store.sessions) == [existing.session_id]
