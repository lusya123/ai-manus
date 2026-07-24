import asyncio

import pytest

from app.infrastructure.external.task.redis_task import RedisStreamTask


@pytest.fixture(autouse=True)
def isolate_local_task_registry_and_factory():
    previous_factory = RedisStreamTask._runner_factory
    previous_registry = RedisStreamTask._task_registry
    RedisStreamTask._task_registry = {}
    try:
        yield
    finally:
        RedisStreamTask._runner_factory = previous_factory
        RedisStreamTask._task_registry = previous_registry


async def test_concurrent_cold_start_runs_share_factory_and_execution():
    factory_entered = asyncio.Event()
    release_factory = asyncio.Event()
    runner_entered = asyncio.Event()
    release_runner = asyncio.Event()

    class Runner:
        def __init__(self):
            self.run_calls = 0
            self.close_calls = 0

        async def run(self, task):
            self.run_calls += 1
            runner_entered.set()
            await release_runner.wait()

        async def on_done(self, task):
            return None

        async def aclose(self):
            self.close_calls += 1

    runner = Runner()

    class Factory:
        def __init__(self):
            self.calls = 0

        async def create_runner(self, params):
            self.calls += 1
            factory_entered.set()
            await release_factory.wait()
            return runner

    factory = Factory()
    RedisStreamTask.set_runner_factory(factory)
    task = RedisStreamTask({"session_id": "single-flight"})

    first_run = asyncio.create_task(task.run())
    await factory_entered.wait()
    second_run = asyncio.create_task(task.run())
    await asyncio.sleep(0)

    # This is the exact former failure boundary: both run() calls used to be
    # suspended in independent create_runner() calls here.
    assert factory.calls == 1
    assert await task.is_done() is False
    assert await task.wait_for_done(0.0) is False

    release_factory.set()
    await asyncio.gather(first_run, second_run)
    await runner_entered.wait()
    assert factory.calls == 1
    assert runner.run_calls == 1

    assert await task.cancel() is True
    release_runner.set()
    assert await task.wait_for_done(1.0) is True
    assert runner.close_calls == 1
    assert task.id not in RedisStreamTask._task_registry


async def test_stale_run_delegates_to_recovered_registry_incumbent():
    factory_entered = asyncio.Event()
    release_factory = asyncio.Event()
    runner_entered = asyncio.Event()
    release_runner = asyncio.Event()
    runners = []

    class Runner:
        def __init__(self):
            self.run_calls = 0
            self.close_calls = 0

        async def run(self, task):
            self.run_calls += 1
            runner_entered.set()
            await release_runner.wait()

        async def on_done(self, task):
            return None

        async def aclose(self):
            self.close_calls += 1

    class Factory:
        def __init__(self):
            self.calls = 0

        async def create_runner(self, params):
            self.calls += 1
            runner = Runner()
            runners.append(runner)
            factory_entered.set()
            await release_factory.wait()
            return runner

    factory = Factory()
    RedisStreamTask.set_runner_factory(factory)
    stale = RedisStreamTask(
        {"session_id": "registry-aba"}, task_id="shared-task-id"
    )
    stale._cleanup_registry()
    recovered = RedisStreamTask.recover(
        "shared-task-id", {"session_id": "registry-aba"}
    )

    assert recovered is not stale
    assert RedisStreamTask._task_registry[stale.id] is recovered

    # Both handles race at the former ABA boundary. The stale run used to
    # overwrite `recovered` in the registry and each object called the factory.
    stale_run = asyncio.create_task(stale.run())
    recovered_run = asyncio.create_task(recovered.run())
    await factory_entered.wait()
    await asyncio.sleep(0)

    assert factory.calls == 1
    assert len(runners) == 1
    assert RedisStreamTask._task_registry[stale.id] is recovered

    release_factory.set()
    await asyncio.gather(stale_run, recovered_run)
    await runner_entered.wait()
    assert factory.calls == 1
    assert runners[0].run_calls == 1
    assert stale._runner is None
    assert RedisStreamTask._task_registry[stale.id] is recovered

    assert await recovered.cancel() is True
    release_runner.set()
    assert await recovered.wait_for_done(1.0) is True
    assert runners[0].close_calls == 1
    assert stale.id not in RedisStreamTask._task_registry


async def test_cancel_during_factory_is_tracked_until_factory_cleanup():
    factory_entered = asyncio.Event()
    cancellation_seen = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class Factory:
        def __init__(self):
            self.calls = 0
            self.lease_held = False

        async def create_runner(self, params):
            self.calls += 1
            self.lease_held = True
            factory_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                # Model a factory's cancellation-safe partial-construction
                # cleanup before it acknowledges cancellation.
                await asyncio.sleep(0)
                self.lease_held = False
                cleanup_finished.set()
                raise

    factory = Factory()
    RedisStreamTask.set_runner_factory(factory)
    task = RedisStreamTask({"session_id": "cancel-construction"})
    run_call = asyncio.create_task(task.run())
    await factory_entered.wait()

    assert await task.is_done() is False
    assert await task.cancel() is True
    # Repeated stop/delete requests must not interrupt the factory's cleanup
    # with a second cancellation delivery.
    assert await task.cancel() is True
    assert await task.wait_for_done(1.0) is True
    await cancellation_seen.wait()
    await cleanup_finished.wait()
    result = await asyncio.gather(run_call, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert factory.calls == 1
    assert factory.lease_held is False
    assert task._runner is None
    assert task._execution_task is None
    assert await task.is_done() is True
    assert task.id not in RedisStreamTask._task_registry


async def test_cancel_closes_runner_returned_by_cancellation_suppressing_factory():
    factory_entered = asyncio.Event()

    class Runner:
        def __init__(self):
            self.lease_held = True
            self.run_calls = 0
            self.close_calls = 0

        async def run(self, task):
            self.run_calls += 1

        async def on_done(self, task):
            return None

        async def aclose(self):
            self.close_calls += 1
            self.lease_held = False

    runner = Runner()

    class Factory:
        async def create_runner(self, params):
            factory_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # A custom factory may suppress cancellation after finishing
                # construction. RedisStreamTask must not start or leak it.
                return runner

    RedisStreamTask.set_runner_factory(Factory())
    task = RedisStreamTask({"session_id": "returned-after-cancel"})
    run_call = asyncio.create_task(task.run())
    await factory_entered.wait()

    assert await task.cancel() is True
    await run_call
    assert await task.wait_for_done(1.0) is True

    assert runner.run_calls == 0
    assert runner.close_calls == 1
    assert runner.lease_held is False
    assert task._execution_task is None
    assert task.id not in RedisStreamTask._task_registry
