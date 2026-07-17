from app.domain.services.agent_domain_service import AgentDomainService
from app.infrastructure.external.message_queue.redis_stream_queue import RedisStreamQueue


class _FakeRedisClient:
    def __init__(self):
        self.streams = None

    async def xread(self, streams, count=None, block=None):
        self.streams = streams
        return []


class _FakeRedis:
    def __init__(self):
        self.client = _FakeRedisClient()


async def test_redis_stream_queue_falls_back_for_client_event_id():
    queue = object.__new__(RedisStreamQueue)
    queue._stream_name = "task:output:test"
    queue._redis = _FakeRedis()

    event_id, event = await queue.get(start_id="deploy-test-123", block_ms=0)

    assert (event_id, event) == (None, None)
    assert queue._redis.client.streams == {"task:output:test": "0"}


async def test_redis_stream_queue_accepts_valid_stream_id():
    queue = object.__new__(RedisStreamQueue)
    queue._stream_name = "task:output:test"
    queue._redis = _FakeRedis()

    await queue.get(start_id="1782506372223-0", block_ms=0)

    assert queue._redis.client.streams == {"task:output:test": "1782506372223-0"}


def test_agent_domain_service_distinguishes_client_ids_from_redis_ids():
    assert AgentDomainService._is_redis_stream_id("1782506372223-0")
    assert AgentDomainService._is_redis_stream_id("0")
    assert AgentDomainService._is_redis_stream_id("$")
    assert not AgentDomainService._is_redis_stream_id("deploy-test-123")
    assert not AgentDomainService._is_redis_stream_id("43ac1414-99a8-482f-a5c6-1778e6fd2ebb")
