"""Real-Redis checks for the cross-process Celery task state machine."""

import asyncio
import json
import shutil
import socket
import subprocess
import tempfile
import time
from types import SimpleNamespace

import pytest
from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from app.infrastructure.external.task import celery_task


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def standalone_redis_url():
    executable = shutil.which("redis-server")
    if not executable:
        pytest.skip("redis-server is not installed")
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="manus-task-redis-") as data_dir:
        process = subprocess.Popen(
            [
                executable,
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--dir",
                data_dir,
                "--save",
                "",
                "--appendonly",
                "no",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"redis://127.0.0.1:{port}/0"
        deadline = time.monotonic() + 10
        while True:
            try:
                probe = Redis.from_url(url, socket_connect_timeout=0.2)
                probe.ping()
                probe.close()
                break
            except Exception:
                if process.poll() is not None or time.monotonic() >= deadline:
                    process.terminate()
                    pytest.skip("temporary redis-server did not start")
                time.sleep(0.05)
        try:
            yield url
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


@pytest.fixture
async def task_redis(monkeypatch, standalone_redis_url):
    client = AsyncRedis.from_url(standalone_redis_url, decode_responses=True)
    await client.flushdb()
    wrapper = SimpleNamespace(client=client)
    monkeypatch.setattr(celery_task, "get_redis", lambda: wrapper)
    try:
        yield client
    finally:
        await client.aclose()


async def _status(client, task_id: str) -> dict:
    return json.loads(await client.get(celery_task.meta_key(task_id)))


async def test_dispatch_claim_rerun_and_old_generation_are_atomic(task_redis):
    task_id = "state-machine"
    params = {"session_id": "session-1", "user_id": "user-1"}

    first = await celery_task.request_dispatch(task_id, params, token="generation-1")
    assert first.decision == "dispatch"
    assert (
        await celery_task.request_dispatch(task_id, params, token="generation-2")
    ).decision == "wait"
    assert await celery_task.publish_dispatch(task_id, first.token)
    assert (
        await celery_task.request_dispatch(task_id, params, token="generation-2")
    ).decision == "rerun"
    assert (
        await celery_task.claim_dispatch(task_id, first.token, "worker-1")
        == "claimed"
    )
    # A committed claim whose response was lost is repeatable by the same
    # worker nonce, while a different delivery must wait for the lease.
    assert (
        await celery_task.claim_dispatch(task_id, first.token, "worker-1")
        == "claimed"
    )
    assert (
        await celery_task.claim_dispatch(task_id, first.token, "worker-2")
        == "busy"
    )
    assert (
        await celery_task.finish_cycle(
            task_id, first.token, "worker-1", "cycle-1"
        )
        == "continue"
    )
    # A lost Redis response must be safely repeatable and must not consume the
    # decision for the next worker generation.
    assert (
        await celery_task.finish_cycle(
            task_id, first.token, "worker-1", "cycle-1"
        )
        == "continue"
    )
    assert (
        await celery_task.finish_cycle(
            task_id, first.token, "worker-1", "cycle-2"
        )
        == "done"
    )

    second = await celery_task.request_dispatch(task_id, params, token="generation-2")
    assert second.decision == "dispatch"
    assert (
        await celery_task.finish_cycle(
            task_id, first.token, "worker-1", "old-cycle"
        )
        == "stale"
    )
    meta = await _status(task_redis, task_id)
    assert meta["status"] == celery_task.STATUS_DISPATCHING
    assert meta["dispatch_token"] == "generation-2"


async def test_broker_failure_aborts_only_its_generation(task_redis):
    task_id = "broker-failure"
    params = {"session_id": "session-1"}
    first = await celery_task.request_dispatch(task_id, params, token="generation-1")
    assert await celery_task.abort_dispatch(task_id, first.token)
    assert (await _status(task_redis, task_id))["status"] == celery_task.STATUS_DONE

    second = await celery_task.request_dispatch(task_id, params, token="generation-2")
    assert second.decision == "dispatch"
    assert not await celery_task.abort_dispatch(task_id, first.token)
    assert (await _status(task_redis, task_id))["dispatch_token"] == second.token


async def test_expired_dispatch_lease_can_be_taken_over(
    task_redis, monkeypatch
):
    monkeypatch.setattr(celery_task, "DISPATCH_LEASE_SECONDS", 0)
    task_id = "expired-dispatch"
    params = {"session_id": "session-1"}
    assert (
        await celery_task.request_dispatch(task_id, params, token="generation-1")
    ).decision == "dispatch"
    replacement = await celery_task.request_dispatch(
        task_id, params, token="generation-2"
    )
    assert replacement.decision == "dispatch"
    assert (await _status(task_redis, task_id))["dispatch_token"] == "generation-2"


async def test_expired_worker_claim_can_be_taken_over(task_redis, monkeypatch):
    monkeypatch.setattr(celery_task, "WORKER_CLAIM_LEASE_SECONDS", 0)
    task_id = "expired-worker"
    params = {"session_id": "session-1"}
    dispatch = await celery_task.request_dispatch(
        task_id, params, token="generation-1"
    )
    assert await celery_task.publish_dispatch(task_id, dispatch.token)
    assert (
        await celery_task.claim_dispatch(task_id, dispatch.token, "worker-1")
        == "claimed"
    )
    assert (
        await celery_task.claim_dispatch(task_id, dispatch.token, "worker-2")
        == "claimed"
    )
    assert not await celery_task.renew_dispatch_claim(
        task_id, dispatch.token, "worker-1"
    )
    assert await celery_task.renew_dispatch_claim(
        task_id, dispatch.token, "worker-2"
    )


async def test_physical_execution_lease_serializes_same_logical_claim(task_redis):
    task_id = "duplicate-delivery"
    assert await celery_task.acquire_execution_lease(task_id, "invocation-1")
    assert not await celery_task.acquire_execution_lease(task_id, "invocation-2")
    assert await celery_task.renew_execution_lease(task_id, "invocation-1")
    assert not await celery_task.renew_execution_lease(task_id, "invocation-2")
    assert not await celery_task.release_execution_lease(task_id, "invocation-2")
    assert await celery_task.release_execution_lease(task_id, "invocation-1")
    assert await celery_task.acquire_execution_lease(task_id, "invocation-2")


async def test_cancel_atomically_retires_unclaimed_delivery(task_redis):
    task_id = "cancel-before-claim"
    params = {"session_id": "session-1"}
    dispatch = await celery_task.request_dispatch(
        task_id, params, token="generation-1"
    )
    assert dispatch.decision == "dispatch"

    assert await celery_task.request_cancel(task_id) is True
    assert (await _status(task_redis, task_id))["status"] == celery_task.STATUS_DONE
    assert (
        await celery_task.claim_dispatch(task_id, dispatch.token, "worker-1")
        == "stale"
    )
    assert not await task_redis.exists(celery_task.cancel_key(task_id))
    assert await celery_task.request_cancel(task_id) is False


async def test_claim_cancel_race_has_single_authoritative_outcome(task_redis):
    params = {"session_id": "session-1"}
    for index in range(30):
        task_id = f"claim-cancel-race-{index}"
        token = f"generation-{index}"
        dispatch = await celery_task.request_dispatch(task_id, params, token=token)
        assert await celery_task.publish_dispatch(task_id, dispatch.token)

        claim, cancelled = await asyncio.gather(
            celery_task.claim_dispatch(task_id, token, "worker-1"),
            celery_task.request_cancel(task_id),
        )
        meta = await _status(task_redis, task_id)
        if claim == "claimed":
            assert cancelled is True
            assert meta["status"] == celery_task.STATUS_RUNNING
            assert await task_redis.exists(celery_task.cancel_key(task_id))
        else:
            assert claim == "stale"
            assert cancelled is True
            assert meta["status"] == celery_task.STATUS_DONE
            assert not await task_redis.exists(celery_task.cancel_key(task_id))


async def test_submitter_finish_tail_race_never_leaves_done_with_rerun(
    task_redis,
):
    params = {"session_id": "session-1", "user_id": "user-1"}
    for index in range(40):
        task_id = f"tail-race-{index}"
        old_token = f"old-{index}"
        new_token = f"new-{index}"
        dispatch = await celery_task.request_dispatch(
            task_id, params, token=old_token
        )
        assert dispatch.decision == "dispatch"
        assert await celery_task.publish_dispatch(task_id, old_token)
        assert (
            await celery_task.claim_dispatch(task_id, old_token, "worker-1")
            == "claimed"
        )

        submit, finish = await asyncio.gather(
            celery_task.request_dispatch(task_id, params, token=new_token),
            celery_task.finish_cycle(
                task_id,
                old_token,
                "worker-1",
                f"cycle-{index}",
            ),
        )
        meta = await _status(task_redis, task_id)
        rerun_exists = bool(
            await task_redis.exists(celery_task.rerun_key(task_id))
        )

        if submit.decision == "rerun":
            assert finish == "continue"
            assert meta["status"] == celery_task.STATUS_RUNNING
        else:
            assert submit.decision == "dispatch"
            assert finish == "done"
            assert meta["status"] == celery_task.STATUS_DISPATCHING
            assert meta["dispatch_token"] == new_token
        assert not (meta["status"] == celery_task.STATUS_DONE and rerun_exists)
