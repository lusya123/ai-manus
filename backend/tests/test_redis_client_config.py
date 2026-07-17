from types import SimpleNamespace

import pytest
from redis.exceptions import ConnectionError, TimeoutError

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
        redis_health_check_interval=15,
        redis_max_connections=42,
        redis_retry_attempts=2,
    )

    monkeypatch.setattr(redis_module, "get_settings", lambda: settings)
    monkeypatch.setattr(redis_module, "Redis", FakeRedis)

    client = redis_module.RedisClient()
    await client.initialize()

    assert FakeRedis.kwargs["socket_connect_timeout"] == 4.0
    assert FakeRedis.kwargs["socket_keepalive"] is True
    assert FakeRedis.kwargs["health_check_interval"] == 15
    assert FakeRedis.kwargs["max_connections"] == 42
    assert FakeRedis.kwargs["retry_on_error"] == [ConnectionError, TimeoutError]
    assert "socket_timeout" not in FakeRedis.kwargs
