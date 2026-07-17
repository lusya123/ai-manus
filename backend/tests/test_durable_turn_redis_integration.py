"""Real-Redis consumer-group recovery checks (skips without redis-server)."""

import shutil
import socket
import subprocess
import time
from types import SimpleNamespace

import pytest
from redis.asyncio import Redis

from app.infrastructure.external.message_queue.redis_stream_queue import (
    RedisStreamQueue,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def standalone_redis_address():
    executable = shutil.which("redis-server")
    if not executable:
        pytest.skip("redis-server is not installed")
    port = _free_port()
    process = subprocess.Popen(
        [
            executable,
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--save",
            "",
            "--appendonly",
            "no",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while True:
        try:
            import redis

            probe = redis.Redis(host="127.0.0.1", port=port)
            probe.ping()
            probe.close()
            break
        except Exception:
            if process.poll() is not None or time.monotonic() >= deadline:
                process.terminate()
                pytest.skip("temporary redis-server did not start")
            time.sleep(0.05)
    try:
        yield "127.0.0.1", port
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


async def test_xreadgroup_xautoclaim_and_xack_preserve_at_least_once_entry(
    standalone_redis_address,
):
    host, port = standalone_redis_address
    client = Redis(host=host, port=port, decode_responses=True)
    queue = RedisStreamQueue("task:input:durable-test")
    queue._redis = SimpleNamespace(client=client)
    try:
        message_id = await queue.put("payload")
        first_id, first_payload = await queue.read_group(
            "agent-turn-workers",
            "worker-1",
            min_idle_ms=0,
            block_ms=1,
        )
        assert (first_id, first_payload) == (message_id, "payload")

        # Simulate worker-1 dying after delivery and before Mongo claim/XACK.
        reclaimed_id, reclaimed_payload = await queue.read_group(
            "agent-turn-workers",
            "worker-2",
            min_idle_ms=0,
            block_ms=1,
        )
        assert (reclaimed_id, reclaimed_payload) == (message_id, "payload")
        assert await queue.ack("agent-turn-workers", message_id)

        assert await queue.read_group(
            "agent-turn-workers",
            "worker-3",
            min_idle_ms=0,
            block_ms=1,
        ) == (None, None)
        # Mongo is authoritative after terminal commit, so the acknowledged
        # prompt is removed and the empty consumer-group key is bounded.
        assert await client.xlen("task:input:durable-test") == 0
        ttl = await client.ttl("task:input:durable-test")
        assert 0 < ttl <= queue._EMPTY_STREAM_TTL_SECONDS

        # New work atomically removes the empty-key expiry before it can race
        # with a reused task ID.
        second_id = await queue.put("next")
        assert second_id
        assert await client.ttl("task:input:durable-test") == -1
    finally:
        await client.aclose()
