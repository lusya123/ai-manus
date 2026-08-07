import asyncio
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.errors.exceptions import UnauthorizedError
from app.application.services.auth_service import AuthService
from app.application.services.token_service import TokenService
from app.core.config import get_settings
from app.domain.models.auth import AuthToken
from app.domain.models.auth_session import AuthSession
from app.domain.models.user import User
from app.infrastructure.external.session_auth.redis_session_store import (
    RedisSessionStore,
)


class _Repo:
    def __init__(self, user: User):
        self.user = user

    async def get_user_by_id(self, user_id):
        return self.user if user_id == self.user.id else None

    async def get_user_by_email(self, email):
        return self.user if email == self.user.email else None

    async def update_user(self, user):
        self.user = user
        return user


class _Redis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.fail = False

    async def set(self, key, value, ex=None, nx=False):
        if self.fail:
            raise ConnectionError("redis unavailable")
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def exists(self, key):
        if self.fail:
            raise ConnectionError("redis unavailable")
        return key in self.values

    async def get(self, key):
        if self.fail:
            raise ConnectionError("redis unavailable")
        return self.values.get(key)

    async def eval(self, script, numkeys, *items):
        if self.fail:
            raise ConnectionError("redis unavailable")
        keys = items[:numkeys]
        args = items[numkeys:]
        if "jwt-migration-commit-v2" in script:
            if keys[0] in self.values:
                return [0, ""]
            previous = self.values.get(keys[1], "")
            self.values[keys[1]] = args[0]
            return [1, previous]
        if "jwt-browser-session-claim-v1" in script:
            if keys[0] in self.values:
                return ""
            current = self.values.get(keys[1])
            replace_session_id = args[2]
            if current:
                if replace_session_id and current == replace_session_id:
                    self.values[keys[1]] = args[0]
                    return args[0]
                return current
            self.values[keys[1]] = args[0]
            return args[0]
        if "jwt-family-revoke-v1" in script:
            self.values[keys[0]] = "1"
            return self.values.pop(keys[1], "")
        raise AssertionError("unexpected Redis script")


class _SessionStore:
    def __init__(self):
        self.sessions: dict[str, AuthSession] = {}
        self.indexes: dict[str, set[str]] = {}
        self.generations: dict[str, int] = {}
        self.lock = asyncio.Lock()
        self.create_started: asyncio.Event | None = None
        self.create_continue: asyncio.Event | None = None
        self.rotate_started: asyncio.Event | None = None
        self.rotate_continue: asyncio.Event | None = None
        self.touch_started: asyncio.Event | None = None
        self.touch_continue: asyncio.Event | None = None

    async def get_user_generation(self, user_id):
        async with self.lock:
            return self.generations.get(user_id, 0)

    async def create(
        self, session, ttl_seconds, *, expected_generation=None
    ):
        if self.create_started is not None:
            self.create_started.set()
        if self.create_continue is not None:
            await self.create_continue.wait()
        generation = (
            session.revocation_generation
            if expected_generation is None
            else expected_generation
        )
        async with self.lock:
            if self.generations.get(session.user_id, 0) != generation:
                return False
            if session.revocation_generation != generation:
                return False
            self.sessions[session.session_id] = session
            self.indexes.setdefault(session.user_id, set()).add(
                session.session_id
            )
            return True

    async def get(self, session_id):
        async with self.lock:
            session = self.sessions.get(session_id)
            if not session:
                return None
            if (
                session.revocation_generation
                != self.generations.get(session.user_id, 0)
            ):
                self.sessions.pop(session_id, None)
                self.indexes.get(session.user_id, set()).discard(session_id)
                return None
            return session.model_copy(deep=True)

    async def touch(self, session_id, ttl_seconds):
        if self.touch_started is not None:
            self.touch_started.set()
        if self.touch_continue is not None:
            await self.touch_continue.wait()
        async with self.lock:
            session = self.sessions.get(session_id)
            if not session or (
                session.revocation_generation
                != self.generations.get(session.user_id, 0)
            ):
                return None
            now = datetime.now(UTC)
            session.last_seen_at = now
            session.expires_at = now + timedelta(seconds=ttl_seconds)
            return session.model_copy(deep=True)

    async def rotate(
        self,
        old_session_id,
        session,
        ttl_seconds,
        *,
        expected_generation,
    ):
        if self.rotate_started is not None:
            self.rotate_started.set()
        if self.rotate_continue is not None:
            await self.rotate_continue.wait()
        async with self.lock:
            old_session = self.sessions.get(old_session_id)
            if not old_session:
                return False
            if old_session.user_id != session.user_id:
                return False
            if (
                self.generations.get(session.user_id, 0)
                != expected_generation
                or old_session.revocation_generation != expected_generation
                or session.revocation_generation != expected_generation
            ):
                return False
            del self.sessions[old_session_id]
            self.sessions[session.session_id] = session
            index = self.indexes.setdefault(session.user_id, set())
            index.discard(old_session_id)
            index.add(session.session_id)
            return True

    async def delete(self, session_id):
        async with self.lock:
            session = self.sessions.pop(session_id, None)
            if session:
                self.indexes.get(session.user_id, set()).discard(session_id)
            return session is not None

    async def delete_all_for_user(self, user_id):
        async with self.lock:
            self.generations[user_id] = self.generations.get(user_id, 0) + 1
            indexed = len(self.indexes.get(user_id, set()))
            self.indexes.pop(user_id, None)
            # Production intentionally leaves the opaque keys to their TTL.
            # The generation mismatch makes them unusable immediately and a
            # later get/touch removes each orphan opportunistically.
            return indexed


@pytest.fixture(autouse=True)
def password_auth_settings(monkeypatch):
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("AUTH_PROVIDER", "password")
    monkeypatch.setenv(
        "JWT_SECRET_KEY", "test-only-password-jwt-secret-at-least-32-bytes"
    )
    get_settings.cache_clear()
    redis = _Redis()
    monkeypatch.setattr(
        "app.infrastructure.storage.redis.get_redis",
        lambda: SimpleNamespace(client=redis),
    )
    yield redis
    get_settings.cache_clear()


@pytest.fixture
def user():
    return User(
        id="user-1",
        fullname="Token User",
        email="token@example.com",
        is_active=True,
    )


@pytest.fixture
def session_store():
    return _SessionStore()


@pytest.fixture
def service(user, session_store):
    return AuthService(_Repo(user), TokenService(), session_store)


def _pair(service: AuthService, user: User) -> tuple[str, str]:
    return service.token_service.create_token_pair(user)


def test_access_and_refresh_pair_share_family_but_have_unique_ids(service, user):
    access, refresh = _pair(service, user)
    access_claims = service.token_service.verify_token(
        access, expected_type="access"
    )
    refresh_claims = service.token_service.verify_token(
        refresh, expected_type="refresh"
    )

    assert access_claims["sid"] == refresh_claims["sid"]
    assert access_claims["jti"] != refresh_claims["jti"]
    assert service.token_service.verify_token(
        refresh, expected_type="access"
    ) is None


@pytest.mark.asyncio
async def test_refresh_rotates_once_and_logout_revokes_whole_family(service, user):
    original_access, original_refresh = _pair(service, user)

    rotated = await service.refresh_access_token(original_refresh)
    assert rotated.refresh_token
    with pytest.raises(UnauthorizedError, match="already been used"):
        await service.refresh_access_token(original_refresh)

    assert await service.logout(rotated.access_token) is True
    assert await service.verify_token(original_access) is None
    assert await service.verify_token(rotated.access_token) is None
    with pytest.raises(UnauthorizedError, match="Invalid refresh token"):
        await service.refresh_access_token(rotated.refresh_token)


@pytest.mark.asyncio
async def test_opaque_rotation_preserves_migrated_jwt_family(service, user):
    original_access, original_refresh = _pair(service, user)
    migrated = await service.refresh_access_token(original_refresh)
    rotated = await service.refresh_access_token(
        migrated.refresh_token, rotate=True
    )

    assert await service.logout(original_access) is True
    assert await service.verify_token(rotated.access_token) is None


@pytest.mark.asyncio
async def test_refresh_replaces_access_migration_without_orphan_session(
    service, user, session_store, password_auth_settings
):
    access, refresh = _pair(service, user)
    payload = service.token_service.verify_token(
        access, expected_type="access"
    )
    family_id = str(payload["sid"])
    migrated_from = f"jwt-family:{family_id}"

    access_session = await service.create_auth_session(
        user,
        rotated_from=migrated_from,
    )
    winner = await service.claim_jwt_session_migration(
        family_id,
        access_session.session_id,
        service._payload_ttl(payload),
    )
    assert winner == access_session.session_id

    # Re-committing the current winner must never delete that same session.
    committed, displaced = await service._commit_jwt_session_migration(
        family_id,
        access_session.session_id,
        service._payload_ttl(payload),
    )
    assert committed is True
    assert displaced is None
    assert access_session.session_id in session_store.sessions

    refreshed = await service.refresh_access_token(refresh)

    mapping_key = service._jwt_migrated_session_key(family_id)
    assert password_auth_settings.values[mapping_key] == refreshed.access_token
    assert access_session.session_id not in session_store.sessions
    assert await service.verify_token(access_session.session_id) is None

    assert await service.logout(refreshed.access_token) is True
    assert await service.verify_token(access) is None
    assert await service.verify_token(refreshed.access_token) is None


@pytest.mark.asyncio
async def test_logout_can_revoke_family_with_refresh_when_access_is_unusable(
    service, user
):
    access, refresh = _pair(service, user)

    assert await service.logout(None, refresh_token=refresh) is True
    assert await service.verify_token(access) is None
    with pytest.raises(UnauthorizedError, match="Invalid refresh token"):
        await service.refresh_access_token(refresh)


@pytest.mark.asyncio
async def test_concurrent_refresh_is_single_use_across_service_instances(
    user, session_store
):
    service_a = AuthService(_Repo(user), TokenService(), session_store)
    service_b = AuthService(_Repo(user), TokenService(), session_store)
    # Both services use the same monkeypatched Redis singleton from the fixture;
    # reset its values via the object exposed by the closure is unnecessary.
    access, refresh = _pair(service_a, user)
    del access

    results = await asyncio.gather(
        service_a.refresh_access_token(refresh),
        service_b.refresh_access_token(refresh),
        return_exceptions=True,
    )

    assert sum(isinstance(result, AuthToken) for result in results) == 1
    assert sum(isinstance(result, UnauthorizedError) for result in results) == 1


@pytest.mark.asyncio
async def test_refresh_logout_race_cannot_resurrect_family(service, user):
    access, refresh = _pair(service, user)

    refresh_result, logout_result = await asyncio.gather(
        service.refresh_access_token(refresh),
        service.logout(access),
        return_exceptions=True,
    )

    assert logout_result is True
    if isinstance(refresh_result, AuthToken):
        assert await service.verify_token(refresh_result.access_token) is None
        with pytest.raises(UnauthorizedError):
            await service.refresh_access_token(refresh_result.refresh_token)
    else:
        assert isinstance(refresh_result, UnauthorizedError)


@pytest.mark.asyncio
async def test_logout_all_fences_login_verified_before_session_create(
    service, user, session_store
):
    user.password_hash = service._hash_password("old-password")
    session_store.create_started = asyncio.Event()
    session_store.create_continue = asyncio.Event()

    login_task = asyncio.create_task(
        service.login_with_session(user.email, "old-password")
    )
    await session_store.create_started.wait()

    assert await service.logout_all(user.id) == 0
    session_store.create_continue.set()

    with pytest.raises(UnauthorizedError, match="revoked during session creation"):
        await login_task
    assert session_store.sessions == {}
    assert session_store.generations[user.id] == 1


@pytest.mark.asyncio
async def test_logout_all_invalidates_orphan_session_without_scanning_keys(
    service, user, session_store
):
    session = await service.create_auth_session(user)

    assert await service.logout_all(user.id) == 1
    # Redis keeps the opaque key until TTL expiry so revoke-all stays O(1),
    # but the generation fence makes it unusable immediately.
    assert session.session_id in session_store.sessions
    assert user.id not in session_store.indexes
    assert await session_store.get(session.session_id) is None
    assert session.session_id not in session_store.sessions


@pytest.mark.asyncio
async def test_redis_revoke_all_uses_constant_time_unlink():
    client = SimpleNamespace(eval=AsyncMock(return_value=17))
    redis = SimpleNamespace(initialize=AsyncMock(), client=client)
    store = RedisSessionStore.__new__(RedisSessionStore)
    store._redis = redis

    assert await store.delete_all_for_user("large-user") == 17

    script, num_keys, index_key, generation_key = client.eval.await_args.args
    normalized = script.upper()
    assert num_keys == 2
    assert index_key == "user_sessions:large-user"
    assert generation_key == "user_session_generation:large-user"
    assert "SCARD" in normalized
    assert "INCR" in normalized
    assert "UNLINK" in normalized
    assert "SMEMBERS" not in normalized
    assert "SESSION:" not in normalized


@pytest.mark.asyncio
async def test_password_change_fences_login_verified_with_old_password(
    service, user, session_store
):
    user.password_hash = service._hash_password("old-password")
    session_store.create_started = asyncio.Event()
    session_store.create_continue = asyncio.Event()

    login_task = asyncio.create_task(
        service.login_with_session(user.email, "old-password")
    )
    await session_store.create_started.wait()

    assert await service.change_password(
        user.id, "old-password", "new-password"
    ) is True
    session_store.create_continue.set()

    with pytest.raises(UnauthorizedError, match="revoked during session creation"):
        await login_task
    assert session_store.sessions == {}
    assert session_store.generations[user.id] == 1
    assert service._verify_password("new-password", user.password_hash) is True


@pytest.mark.asyncio
async def test_resolve_does_not_revive_session_deleted_before_atomic_touch(
    service, user, session_store
):
    session = await service.create_auth_session(user)
    session_store.touch_started = asyncio.Event()
    session_store.touch_continue = asyncio.Event()

    resolve_task = asyncio.create_task(
        service.resolve_session_token(session.session_id)
    )
    await session_store.touch_started.wait()
    assert await session_store.delete(session.session_id) is True
    session_store.touch_continue.set()

    assert await resolve_task is None
    assert session.session_id not in session_store.sessions


@pytest.mark.asyncio
async def test_sliding_refresh_fails_if_session_is_deleted_before_touch(
    service, user, session_store
):
    session = await service.create_auth_session(user)
    session_store.touch_started = asyncio.Event()
    session_store.touch_continue = asyncio.Event()

    refresh_task = asyncio.create_task(
        service.refresh_access_token(session.session_id)
    )
    await session_store.touch_started.wait()
    assert await session_store.delete(session.session_id) is True
    session_store.touch_continue.set()

    with pytest.raises(UnauthorizedError, match="logged out during refresh"):
        await refresh_task
    assert session.session_id not in session_store.sessions


@pytest.mark.asyncio
async def test_single_logout_wins_before_opaque_rotation_commit(
    service, user, session_store
):
    session = await service.create_auth_session(user)
    session_store.rotate_started = asyncio.Event()
    session_store.rotate_continue = asyncio.Event()

    refresh_task = asyncio.create_task(
        service.refresh_access_token(session.session_id, rotate=True)
    )
    await session_store.rotate_started.wait()
    assert await service.logout(session.session_id) is True
    session_store.rotate_continue.set()

    with pytest.raises(UnauthorizedError, match="logged out during refresh"):
        await refresh_task
    assert session_store.sessions == {}


@pytest.mark.asyncio
async def test_ordinary_opaque_rotation_keeps_current_generation(
    service, user, session_store
):
    original = await service.create_auth_session(user)

    rotated = await service.refresh_access_token(
        original.session_id, rotate=True
    )

    assert original.session_id not in session_store.sessions
    replacement = session_store.sessions[rotated.access_token]
    assert replacement.revocation_generation == original.revocation_generation == 0
    assert await service.verify_token(rotated.access_token) is not None


@pytest.mark.asyncio
async def test_revocation_store_failure_fails_closed(
    service, user, password_auth_settings
):
    access, _ = _pair(service, user)
    password_auth_settings.fail = True

    assert await service.verify_token(access) is None
