import pytest
from types import SimpleNamespace

from app.application.errors.exceptions import (
    BadRequestError,
    ServiceUnavailableError,
    TooManyRequestsError,
)
from app.application.services.auth_service import AuthService
from app.application.services.token_service import TokenService
from app.core.config import get_settings
from app.domain.models.user import User


class _Repo:
    def __init__(self, user=None):
        self.user = user
        self.updated = []

    async def get_user_by_email(self, email):
        return self.user if self.user and self.user.email == email else None

    async def update_user(self, user):
        self.updated.append(user.model_copy(deep=True))
        self.user = user
        return user


class _SessionStore:
    async def get_user_generation(self, user_id):
        return 0

    async def create(
        self, session, ttl_seconds, *, expected_generation=None
    ):
        return True

    async def get(self, session_id):
        return None

    async def touch(self, session_id, ttl_seconds):
        return None

    async def rotate(
        self,
        old_session_id,
        session,
        ttl_seconds,
        *,
        expected_generation,
    ):
        return False

    async def delete(self, session_id):
        return False

    async def delete_all_for_user(self, user_id):
        return 0


@pytest.fixture(autouse=True)
def password_settings(monkeypatch):
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("AUTH_PROVIDER", "password")
    monkeypatch.setenv("PASSWORD_SALT", "legacy-global-salt")
    monkeypatch.setenv("PASSWORD_LEGACY_HASH_ROUNDS", "10")
    monkeypatch.setenv("PASSWORD_HASH_ROUNDS", "600000")
    monkeypatch.setenv(
        "JWT_SECRET_KEY", "test-only-password-hashing-secret-at-least-32-bytes"
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _service(repo=None):
    return AuthService(repo or _Repo(), TokenService(), _SessionStore())


def test_password_hashes_use_random_salts_high_cost_and_constant_time_verification():
    service = _service()

    first = service._hash_password("correct horse battery staple")
    second = service._hash_password("correct horse battery staple")

    assert first != second
    scheme, rounds, salt, digest = first.split("$")
    assert scheme == "pbkdf2_sha256"
    assert int(rounds) >= 600_000
    assert salt and digest
    assert service._verify_password("correct horse battery staple", first) is True
    assert service._verify_password("wrong password", first) is False


@pytest.mark.asyncio
async def test_successful_legacy_login_progressively_rehashes_password():
    service = _service()
    legacy_hash = service._legacy_password_hash("old-password")
    user = User(
        id="legacy-user",
        fullname="Legacy User",
        email="legacy@example.com",
        password_hash=legacy_hash,
    )
    repo = _Repo(user)
    service = _service(repo)

    authenticated = await service.authenticate_user(
        "legacy@example.com", "old-password"
    )

    assert authenticated is user
    assert user.password_hash != legacy_hash
    assert user.password_hash.startswith("pbkdf2_sha256$600000$")
    assert service._verify_password("old-password", user.password_hash) is True
    assert repo.updated[-1].password_hash == user.password_hash


@pytest.mark.asyncio
async def test_wrong_legacy_password_is_rejected_without_rehash():
    service = _service()
    legacy_hash = service._legacy_password_hash("right-password")
    user = User(
        id="legacy-user",
        fullname="Legacy User",
        email="legacy@example.com",
        password_hash=legacy_hash,
    )
    repo = _Repo(user)
    service = _service(repo)

    assert await service.authenticate_user(
        "legacy@example.com", "wrong-password"
    ) is None
    assert user.password_hash == legacy_hash
    assert repo.updated == []


def test_malformed_or_pathologically_expensive_hash_is_rejected():
    service = _service()

    assert service._verify_password("password", "pbkdf2_sha256$bad$x$y") is False
    assert service._verify_password(
        "password", "pbkdf2_sha256$10000001$YWJj$YWJj"
    ) is False


def test_nonlocal_environment_rejects_default_local_admin_password(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "local")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "production")
    monkeypatch.setenv("LOCAL_AUTH_PASSWORD", "admin")
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="LOCAL_AUTH_PASSWORD"):
        get_settings()


@pytest.mark.asyncio
async def test_public_registration_is_closed_by_default():
    service = _service()

    with pytest.raises(BadRequestError, match="Public registration is disabled"):
        await service.register_user(
            fullname="New User",
            password="password123",
            email="new@example.com",
        )


@pytest.mark.asyncio
async def test_auth_rate_limit_is_shared_and_fails_closed(monkeypatch):
    class Redis:
        def __init__(self):
            self.counts = {}
            self.fail = False

        async def eval(self, script, numkeys, key, window):
            if self.fail:
                raise ConnectionError("redis down")
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]

    redis = Redis()
    monkeypatch.setattr(
        "app.infrastructure.storage.redis.get_redis",
        lambda: SimpleNamespace(client=redis),
    )
    service_a = _service()
    service_b = _service()

    await service_a.enforce_rate_limit(
        "login-account", "same@example.com", limit=2, window_seconds=300
    )
    await service_b.enforce_rate_limit(
        "login-account", "same@example.com", limit=2, window_seconds=300
    )
    with pytest.raises(TooManyRequestsError):
        await service_a.enforce_rate_limit(
            "login-account", "same@example.com", limit=2, window_seconds=300
        )

    redis.fail = True
    with pytest.raises(ServiceUnavailableError):
        await service_a.enforce_rate_limit(
            "login-ip", "127.0.0.1", limit=10, window_seconds=300
        )
