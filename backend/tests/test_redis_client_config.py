from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError, TimeoutError

from app.core.config import Settings
import app.infrastructure.storage.redis as redis_module


class FakeRedis:
    kwargs = None

    def __init__(self, **kwargs):
        FakeRedis.kwargs = kwargs

    async def ping(self):
        return True


@pytest.mark.asyncio
async def test_redis_client_uses_resilient_connection_options(monkeypatch):
    settings = SimpleNamespace(
        redis_host="redis",
        redis_port=6379,
        redis_db=0,
        redis_password=None,
        redis_socket_connect_timeout=4.0,
        redis_socket_timeout=12.5,
        redis_health_check_interval=15,
        redis_max_connections=42,
        redis_retry_attempts=2,
    )

    monkeypatch.setattr(redis_module, "get_settings", lambda: settings)
    monkeypatch.setattr(redis_module, "Redis", FakeRedis)

    client = redis_module.RedisClient()
    await client.initialize()

    assert FakeRedis.kwargs["socket_connect_timeout"] == 4.0
    assert FakeRedis.kwargs["socket_timeout"] == 12.5
    assert FakeRedis.kwargs["socket_keepalive"] is True
    assert FakeRedis.kwargs["health_check_interval"] == 15
    assert FakeRedis.kwargs["max_connections"] == 42
    assert FakeRedis.kwargs["retry_on_error"] == [ConnectionError, TimeoutError]


def test_redis_socket_timeout_defaults_to_5_seconds(monkeypatch):
    monkeypatch.delenv("REDIS_SOCKET_TIMEOUT", raising=False)

    assert Settings(_env_file=None).redis_socket_timeout == 5.0


@pytest.mark.parametrize("value", [0, -0.1])
def test_redis_socket_timeout_must_be_positive(value):
    with pytest.raises(ValidationError, match="redis_socket_timeout"):
        Settings(_env_file=None, redis_socket_timeout=value)
