import asyncio
import json
from types import SimpleNamespace

import pytest

from app.application.services.claw_service import (
    ClawEventBus,
    ClawService,
    _ClawSubscriberQueue,
)
from app.core.config import get_settings
from app.domain.external.claw import ClawResponseTooLargeError
from app.domain.services.claw_domain_service import ClawDomainService


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("AUTH_PROVIDER", "none")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _RedisClient:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def publish(self, channel, payload):
        return 1

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, numkeys, key, owner_token, *args):
        if self.values.get(key) != owner_token:
            return 0
        if "redis.call('del'" in script:
            del self.values[key]
            return 1
        if "redis.call('expire'" in script:
            return 1
        raise AssertionError("unexpected Lua script")

    def pubsub(self):
        raise AssertionError("pubsub is not configured for this test")


def _drain(queue: _ClawSubscriberQueue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


async def test_slow_consumer_keeps_exactly_one_error_and_done_after_201_chunks(
    monkeypatch,
):
    redis = _RedisClient()
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: SimpleNamespace(client=redis),
    )
    bus = ClawEventBus()
    queue = _ClawSubscriberQueue(max_non_terminal_events=200)
    bus._subscribers["user-1"].append(queue)

    expected = "".join(str(index % 10) for index in range(251))
    for char in expected:
        await bus.publish("user-1", {"type": "text", "content": char})
    await bus.publish("user-1", {"type": "error", "error": "failed"})
    await bus.publish("user-1", {"type": "done", "stop_reason": "end_turn"})

    events = _drain(queue)
    assert "".join(
        event["content"] for event in events if event["type"] == "text"
    ) == expected
    assert [event["type"] for event in events].count("error") == 1
    assert [event["type"] for event in events].count("done") == 1


async def test_remote_redis_ingress_keeps_terminal_after_201_chunks(monkeypatch):
    class _PubSub:
        def __init__(self):
            self.messages = []
            for index in range(251):
                self.messages.append({
                    "type": "message",
                    "data": json.dumps({
                        "origin": "remote",
                        "event": {
                            "type": "text",
                            "content": str(index % 10),
                        },
                    }),
                })
            self.messages.extend([
                {
                    "type": "message",
                    "data": json.dumps({
                        "origin": "remote",
                        "event": {"type": "error", "error": "failed"},
                    }),
                },
                {
                    "type": "message",
                    "data": json.dumps({
                        "origin": "remote",
                        "event": {
                            "type": "done",
                            "stop_reason": "end_turn",
                        },
                    }),
                },
            ])

        async def subscribe(self, channel):
            pass

        async def get_message(self, **kwargs):
            if self.messages:
                return self.messages.pop(0)
            await asyncio.Event().wait()

        async def unsubscribe(self, channel):
            pass

        async def aclose(self):
            pass

    class _RemoteRedis(_RedisClient):
        def __init__(self):
            super().__init__()
            self.subscription = _PubSub()

        def pubsub(self):
            return self.subscription

    redis = _RemoteRedis()
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: SimpleNamespace(client=redis),
    )
    bus = ClawEventBus()
    queue = _ClawSubscriberQueue(max_non_terminal_events=200)
    task = asyncio.create_task(bus._redis_subscribe("user-1", queue))
    try:
        async def _wait_for_done():
            while not any(
                item.event.get("type") == "done" for item in queue._queue
            ):
                await asyncio.sleep(0)

        await asyncio.wait_for(_wait_for_done(), timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    events = _drain(queue)
    assert "".join(
        event["content"] for event in events if event["type"] == "text"
    ) == "".join(str(index % 10) for index in range(251))
    assert [event["type"] for event in events].count("error") == 1
    assert [event["type"] for event in events].count("done") == 1


async def test_remote_redis_half_open_read_is_cancelled_by_monotonic_bound(
    monkeypatch,
):
    class _HalfOpenPubSub:
        def __init__(self):
            self.read_started = asyncio.Event()
            self.read_cancelled = asyncio.Event()
            self.unsubscribed = False
            self.closed = False

        async def subscribe(self, channel):
            pass

        async def get_message(self, **kwargs):
            self.read_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.read_cancelled.set()
                raise

        async def unsubscribe(self, channel):
            self.unsubscribed = True

        async def aclose(self):
            self.closed = True

    pubsub = _HalfOpenPubSub()

    class _RemoteRedis(_RedisClient):
        def pubsub(self):
            return pubsub

    redis = _RemoteRedis()
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: SimpleNamespace(client=redis),
    )
    bus = ClawEventBus()
    bus._redis_operation_timeout_seconds = lambda: 0.01
    task = asyncio.create_task(
        bus._redis_subscribe("user-1", _ClawSubscriberQueue())
    )
    try:
        await asyncio.wait_for(pubsub.read_started.wait(), timeout=1)
        await asyncio.wait_for(pubsub.read_cancelled.wait(), timeout=1)
        # The timeout path must enter cleanup instead of remaining stuck in
        # redis-py's otherwise unbounded socket read.
        async def _wait_for_cleanup():
            while not (pubsub.unsubscribed and pubsub.closed):
                await asyncio.sleep(0)

        await asyncio.wait_for(_wait_for_cleanup(), timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_slow_subscriber_retains_only_latest_turn_with_bounded_bytes():
    queue = _ClawSubscriberQueue(
        max_non_terminal_events=200,
        max_buffer_bytes=1024,
        max_terminal_events=2,
    )

    for turn in range(100):
        for _ in range(300):
            queue.put_nowait({"type": "text", "content": str(turn % 10)})
        queue.put_nowait({"type": "error", "error": f"turn-{turn}"})
        queue.put_nowait({"type": "done", "stop_reason": "end_turn"})
        queue.put_nowait({"type": "done", "stop_reason": "end_turn"})
        assert queue.buffered_bytes <= 1024

    events = _drain(queue)
    assert [event["type"] for event in events].count("done") == 1
    assert [event["type"] for event in events].count("error") == 1
    assert events[-2]["error"] == "turn-99"
    assert events[-1] == {"type": "done", "stop_reason": "end_turn"}


def test_subscriber_byte_budget_counts_json_escape_expansion():
    queue = _ClawSubscriberQueue(max_buffer_bytes=1024)

    for _ in range(1000):
        queue.put_nowait({"type": "text", "content": "\x00"})

    assert queue.buffered_bytes <= 1024
    for event in _drain(queue):
        assert len(json.dumps(event).encode("utf-8")) <= 1024


class _ManyChunkDomain:
    claw_repository = SimpleNamespace()

    def __init__(self, count: int):
        self.count = count

    async def process_chat_stream(self, user_id, base_url, message, session_id):
        for _ in range(self.count):
            yield {"type": "text", "content": "x"}
        yield {"type": "done", "stop_reason": "end_turn"}


class _QueueEventBus:
    def __init__(self):
        self.queue = _ClawSubscriberQueue(max_non_terminal_events=200)

    async def publish(self, user_id, event):
        self.queue.put_nowait(event)


async def test_many_small_chunks_are_processed_incrementally_and_coalesced():
    domain = _ManyChunkDomain(count=10_000)
    service = ClawService(domain)
    event_bus = _QueueEventBus()
    service.event_bus = event_bus
    old_limit = service.settings.claw_chat_max_response_bytes
    service.settings.claw_chat_max_response_bytes = 20_000
    try:
        await asyncio.wait_for(
            service._process_chat("user-1", "http://claw", "hello", "default"),
            timeout=5,
        )
    finally:
        service.settings.claw_chat_max_response_bytes = old_limit

    events = _drain(event_bus.queue)
    assert events == [
        {"type": "text", "content": "x" * 10_000},
        {"type": "done", "stop_reason": "end_turn"},
    ]


class _Repository:
    def __init__(self):
        self.messages: list[tuple] = []

    async def append_message(
        self, user_id, role, content="", attachments=None
    ) -> None:
        self.messages.append((user_id, role, content))


class _OversizedApplicationDomain:
    def __init__(self, repository):
        self.claw_repository = repository

    async def validate_claw_for_chat(self, user_id):
        return SimpleNamespace(http_base_url="http://claw")

    async def process_chat_stream(self, user_id, base_url, message, session_id):
        yield {"type": "text", "content": "你"}
        yield {"type": "text", "content": "好"}
        raise AssertionError("the application cap must stop the stream")


class _CapturingEventBus:
    def __init__(self):
        self.events: list[dict] = []

    async def publish(self, user_id, event):
        self.events.append(event)


async def test_response_byte_cap_emits_error_done_and_releases_turn_lease(
    monkeypatch,
):
    redis = _RedisClient()
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: SimpleNamespace(client=redis),
    )
    repository = _Repository()
    service = ClawService(_OversizedApplicationDomain(repository))
    event_bus = _CapturingEventBus()
    service.event_bus = event_bus
    old_limit = service.settings.claw_chat_max_response_bytes
    service.settings.claw_chat_max_response_bytes = 4
    try:
        await service.send_message("user-1", "hello", "default")
        # send_message installs the reconnect-visible in-flight marker in the
        # same event-loop turn as background task creation.
        assert service.is_processing("user-1") is True
        await asyncio.wait_for(
            asyncio.gather(*service._bg_tasks, return_exceptions=False),
            timeout=1,
        )
    finally:
        service.settings.claw_chat_max_response_bytes = old_limit

    assert redis.values == {}
    assert service.is_processing("user-1") is False
    assert event_bus.events == [
        {"type": "text", "content": "你"},
        {
            "type": "error",
            "error": "Claw response exceeded the configured size limit",
        },
        {"type": "done", "stop_reason": "end_turn"},
    ]


async def test_half_open_redis_publish_fails_open_and_releases_lease(monkeypatch):
    class _HalfOpenRedis(_RedisClient):
        async def publish(self, channel, payload):
            await asyncio.Event().wait()

    redis = _HalfOpenRedis()
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: SimpleNamespace(client=redis),
    )
    repository = _Repository()
    service = ClawService(_OversizedApplicationDomain(repository))
    queue = _ClawSubscriberQueue()
    service.event_bus._subscribers["user-1"].append(queue)
    service.event_bus._redis_publish_timeout_seconds = lambda: 0.01
    old_limit = service.settings.claw_chat_max_response_bytes
    service.settings.claw_chat_max_response_bytes = 4
    try:
        await service.send_message("user-1", "hello", "default")
        await asyncio.wait_for(
            asyncio.gather(*service._bg_tasks, return_exceptions=False),
            timeout=1,
        )
    finally:
        service.settings.claw_chat_max_response_bytes = old_limit

    events = _drain(queue)
    assert redis.values == {}
    assert [event["type"] for event in events][-2:] == ["error", "done"]
    assert [event["type"] for event in events].count("done") == 1


class _EndlessDomain:
    def __init__(self, repository):
        self.claw_repository = repository
        self.closed = False

    async def validate_claw_for_chat(self, user_id):
        return SimpleNamespace(http_base_url="http://claw")

    async def process_chat_stream(self, user_id, base_url, message, session_id):
        try:
            while True:
                yield {"type": "heartbeat"}
                await asyncio.sleep(0)
        finally:
            self.closed = True


async def test_total_stream_deadline_stops_keepalives_and_releases_lease(
    monkeypatch,
):
    redis = _RedisClient()
    monkeypatch.setattr(
        "app.application.services.claw_service.get_redis",
        lambda: SimpleNamespace(client=redis),
    )
    repository = _Repository()
    domain = _EndlessDomain(repository)
    service = ClawService(domain)
    event_bus = _QueueEventBus()
    service.event_bus = event_bus
    old_duration = service.settings.claw_chat_max_duration_seconds
    service.settings.claw_chat_max_duration_seconds = 0.02
    try:
        await service.send_message("user-1", "hello", "default")
        await asyncio.wait_for(
            asyncio.gather(*service._bg_tasks, return_exceptions=False),
            timeout=1,
        )
    finally:
        service.settings.claw_chat_max_duration_seconds = old_duration

    events = _drain(event_bus.queue)
    assert domain.closed is True
    assert redis.values == {}
    assert [event["type"] for event in events][-2:] == ["error", "done"]
    assert events[-2]["error"] == "Claw response timed out; please retry"


class _CancellationResistantDomain:
    def __init__(self, repository):
        self.claw_repository = repository
        self.cancellations = 0
        self.exited = False

    async def process_chat_stream(self, user_id, base_url, message, session_id):
        try:
            yield {"type": "heartbeat"}
            end = asyncio.get_running_loop().time() + 0.08
            while asyncio.get_running_loop().time() < end:
                try:
                    await asyncio.sleep(0.005)
                except asyncio.CancelledError:
                    self.cancellations += 1
                    continue
            # This must be rejected by stop_stream after the owner has already
            # emitted error+done and released its turn.
            yield {"type": "text", "content": "late"}
        finally:
            self.exited = True


async def test_total_deadline_returns_after_two_cleanup_bounds_when_upstream_ignores_cancel():
    repository = _Repository()
    domain = _CancellationResistantDomain(repository)
    service = ClawService(domain)
    event_bus = _CapturingEventBus()
    service.event_bus = event_bus
    old_duration = service.settings.claw_chat_max_duration_seconds
    service.settings.claw_chat_max_duration_seconds = 0.005
    service._chat_task_cleanup_timeout_seconds = lambda: 0.005
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        await asyncio.wait_for(
            service._process_chat(
                "user-1", "http://claw", "hello", "default"
            ),
            timeout=0.1,
        )
        elapsed = loop.time() - started
        assert elapsed < 0.05
        assert len(service._detached_chat_tasks) == 1
        assert domain.cancellations >= 2
        assert [event["type"] for event in event_bus.events][-2:] == [
            "error",
            "done",
        ]
        assert not any(
            event.get("content") == "late" for event in event_bus.events
        )

        await asyncio.sleep(0.1)
        assert domain.exited is True
        assert service._detached_chat_tasks == set()
        assert not any(
            event.get("content") == "late" for event in event_bus.events
        )
    finally:
        service.settings.claw_chat_max_duration_seconds = old_duration


class _ImmediateDomain:
    claw_repository = SimpleNamespace()

    async def process_chat_stream(self, user_id, base_url, message, session_id):
        if False:
            yield {}


async def test_terminal_latch_is_visible_before_done_publish_finishes():
    class _BlockingDoneBus:
        def __init__(self):
            self.done_started = asyncio.Event()
            self.release_done = asyncio.Event()

        async def publish(self, user_id, event):
            if event.get("type") == "done":
                self.done_started.set()
                await self.release_done.wait()

    service = ClawService(_ImmediateDomain())
    event_bus = _BlockingDoneBus()
    service.event_bus = event_bus
    task = asyncio.create_task(
        service._process_chat(
            "user-1", "http://claw", "hello", "default"
        )
    )
    try:
        await asyncio.wait_for(event_bus.done_started.wait(), timeout=1)
        assert service.is_processing("user-1") is True
        assert service.get_terminal_event("user-1") == {
            "type": "done",
            "stop_reason": "end_turn",
        }
    finally:
        event_bus.release_done.set()
        await asyncio.wait_for(task, timeout=1)

    assert service.is_processing("user-1") is False


class _StreamingClient:
    async def chat_stream(self, base_url, message, session_id):
        yield {"type": "text", "content": "ab"}
        yield {"type": "text", "content": "cde"}


async def test_domain_cap_does_not_persist_a_truncated_assistant_message():
    repository = _Repository()
    domain = ClawDomainService(
        repository,
        claw_runtime=SimpleNamespace(),
        claw_client=_StreamingClient(),
    )
    old_limit = domain.settings.claw_chat_max_response_bytes
    domain.settings.claw_chat_max_response_bytes = 4
    try:
        with pytest.raises(ClawResponseTooLargeError):
            async for _ in domain.process_chat_stream(
                "user-1", "http://claw", "hello", "default"
            ):
                pass
    finally:
        domain.settings.claw_chat_max_response_bytes = old_limit

    assert repository.messages == []
