import asyncio
from types import SimpleNamespace

import pytest

from app.application.errors.exceptions import UnauthorizedError
from app.application.services.auth_service import AuthService
from app.application.services.token_service import TokenService
from app.core.config import get_settings
from app.domain.models.auth import AuthToken
from app.domain.models.user import User


class _Repo:
    def __init__(self, user: User):
        self.user = user

    async def get_user_by_id(self, user_id):
        return self.user if user_id == self.user.id else None


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
def service(user):
    return AuthService(_Repo(user), TokenService())


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
async def test_logout_can_revoke_family_with_refresh_when_access_is_unusable(
    service, user
):
    access, refresh = _pair(service, user)

    assert await service.logout(None, refresh_token=refresh) is True
    assert await service.verify_token(access) is None
    with pytest.raises(UnauthorizedError, match="Invalid refresh token"):
        await service.refresh_access_token(refresh)


@pytest.mark.asyncio
async def test_concurrent_refresh_is_single_use_across_service_instances(user):
    service_a = AuthService(_Repo(user), TokenService())
    service_b = AuthService(_Repo(user), TokenService())
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
async def test_revocation_store_failure_fails_closed(
    service, user, password_auth_settings
):
    access, _ = _pair(service, user)
    password_auth_settings.fail = True

    assert await service.verify_token(access) is None
