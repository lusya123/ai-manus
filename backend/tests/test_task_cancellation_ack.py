import asyncio
import pytest

from app.infrastructure.external.task.celery_task import (
    CeleryTask,
    DispatchRequest,
)
from app.infrastructure.external.task import celery_worker
from app.infrastructure.external.task.celery_app import celery_app
from app.infrastructure.external.task.redis_task import RedisStreamTask


@pytest.fixture(autouse=True)
def stub_celery_execution_lease(monkeypatch):
    async def acquired(*args, **kwargs):
        return True

    monkeypatch.setattr(celery_worker, "acquire_execution_lease", acquired)
    monkeypatch.setattr(celery_worker, "release_execution_lease", acquired)


async def test_local_task_cancel_waits_for_coroutine_acknowledgement():
    async def work():
        await asyncio.Event().wait()

    task = object.__new__(RedisStreamTask)
    task._id = "local-task"
    task._execution_task = asyncio.create_task(work())
    await asyncio.sleep(0)

    assert await task.cancel() is True
    assert await task.wait_for_done(0.5) is True
    assert task._execution_task.done()


async def test_local_task_wait_is_bounded_when_cleanup_is_stuck():
    release = asyncio.Event()

    async def stubborn_work():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    task = object.__new__(RedisStreamTask)
    task._id = "stuck-task"
    task._execution_task = asyncio.create_task(stubborn_work())
    await asyncio.sleep(0)

    assert await task.cancel() is True
    assert await task.wait_for_done(0.001) is False

    release.set()
    assert await task.wait_for_done(0.5) is True


async def test_celery_task_waits_for_worker_done_ack(monkeypatch):
    states = iter(({"status": "running"}, {"status": "done"}))

    async def fake_read_meta(task_id):
        return next(states)

    monkeypatch.setattr(
        "app.infrastructure.external.task.celery_task.read_meta", fake_read_meta
    )
    task = CeleryTask("remote-task", params={})

    assert await task.wait_for_done(0.5) is True


def test_celery_worker_loss_is_redelivered_to_renewable_claim_state_machine():
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_cancel_long_running_tasks_on_connection_loss is True


async def test_local_task_shutdown_closes_runner_only_after_task_stops():
    trace = []

    async def work():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            trace.append("cancel-ack-start")
            await asyncio.sleep(0.01)
            trace.append("cancel-ack-done")

    class Runner:
        async def aclose(self):
            trace.append("close")

    task = object.__new__(RedisStreamTask)
    task._id = "shutdown-task"
    task._runner = Runner()
    task._execution_task = asyncio.create_task(work())
    await asyncio.sleep(0)
    RedisStreamTask._task_registry = {task.id: task}
    try:
        await RedisStreamTask.destroy()
    finally:
        RedisStreamTask._task_registry.clear()

    assert trace == ["cancel-ack-start", "cancel-ack-done", "close"]


async def test_wait_for_done_does_not_swallow_caller_cancellation():
    release = asyncio.Event()

    async def work():
        await release.wait()

    task = object.__new__(RedisStreamTask)
    task._id = "live-task"
    task._execution_task = asyncio.create_task(work())
    waiter = asyncio.create_task(task.wait_for_done(10))
    await asyncio.sleep(0)
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    release.set()
    await task._execution_task


async def test_local_task_rechecks_input_after_run_request_while_active():
    first_run_started = asyncio.Event()
    release_first_run = asyncio.Event()

    class Runner:
        def __init__(self):
            self.run_count = 0

        async def run(self, task):
            self.run_count += 1
            if self.run_count == 1:
                first_run_started.set()
                await release_first_run.wait()

        async def on_done(self, task):
            return None

    runner = Runner()
    task = object.__new__(RedisStreamTask)
    task._id = "rerun-task"
    task._params = {}
    task._runner = runner
    task._execution_task = None
    task._rerun_requested = False
    task._cancel_requested = False
    RedisStreamTask._task_registry = {}

    try:
        await task.run()
        await first_run_started.wait()

        # This used to be a no-op.  If the first runner had just observed an
        # empty queue, the newly submitted message could remain stranded.
        await task.run()
        release_first_run.set()
        await task._execution_task
        await asyncio.sleep(0)
    finally:
        RedisStreamTask._task_registry.clear()

    assert runner.run_count == 2


async def test_local_task_restarts_when_run_arrives_during_runner_finalization():
    finalizing = asyncio.Event()
    release_finalizer = asyncio.Event()
    runners = []

    class Runner:
        def __init__(self, *, block_finalize=False):
            self.block_finalize = block_finalize
            self.run_count = 0
            self.close_count = 0

        async def run(self, task):
            self.run_count += 1

        async def on_done(self, task):
            if self.block_finalize:
                finalizing.set()
                await release_finalizer.wait()

        async def aclose(self):
            self.close_count += 1

    first_runner = Runner(block_finalize=True)
    runners.append(first_runner)

    class Factory:
        async def create_runner(self, params):
            runner = Runner()
            runners.append(runner)
            return runner

    previous_factory = RedisStreamTask._runner_factory
    RedisStreamTask._task_registry = {}
    RedisStreamTask.set_runner_factory(Factory())
    task = object.__new__(RedisStreamTask)
    task._id = "finalization-tail-task"
    task._params = {}
    task._runner = first_runner
    task._runner_closed = False
    task._execution_task = None
    task._rerun_requested = False
    task._cancel_requested = False
    try:
        await task.run()
        execution = task._execution_task
        await finalizing.wait()

        await task.run()
        assert task._rerun_requested is True
        release_finalizer.set()
        await execution
    finally:
        RedisStreamTask._runner_factory = previous_factory
        RedisStreamTask._task_registry.clear()

    assert [runner.run_count for runner in runners] == [1, 1]
    assert [runner.close_count for runner in runners] == [1, 1]
    assert task._rerun_requested is False


async def test_local_task_closes_each_rebuilt_runner_across_multiple_turns():
    runners = []

    class Runner:
        def __init__(self):
            self.close_count = 0
            self.on_done_count = 0

        async def run(self, task):
            return None

        async def on_done(self, task):
            self.on_done_count += 1

        async def aclose(self):
            self.close_count += 1

    class Factory:
        async def create_runner(self, params):
            runner = Runner()
            runners.append(runner)
            return runner

    previous_factory = RedisStreamTask._runner_factory
    RedisStreamTask._task_registry = {}
    RedisStreamTask.set_runner_factory(Factory())
    try:
        task = RedisStreamTask({})
        await task.run()
        await task._execution_task

        # A stale handle obtained before registry cleanup can still be asked
        # to run again. It must rebuild, not reuse, its already-closed runner.
        await task.run()
        await task._execution_task
    finally:
        RedisStreamTask._runner_factory = previous_factory
        RedisStreamTask._task_registry.clear()

    assert len(runners) == 2
    assert [runner.on_done_count for runner in runners] == [1, 1]
    assert [runner.close_count for runner in runners] == [1, 1]


async def test_local_task_closes_runner_after_execution_exception():
    class Runner:
        close_count = 0
        on_done_count = 0

        async def run(self, task):
            raise RuntimeError("runner failed")

        async def on_done(self, task):
            self.on_done_count += 1

        async def aclose(self):
            self.close_count += 1

    runner = Runner()
    task = object.__new__(RedisStreamTask)
    task._id = "failed-task"
    task._runner = runner
    task._runner_closed = False
    task._rerun_requested = False
    task._cancel_requested = False
    RedisStreamTask._task_registry = {task.id: task}
    try:
        await task._execute_task()
    finally:
        RedisStreamTask._task_registry.clear()

    assert runner.on_done_count == 1
    assert runner.close_count == 1


async def test_local_task_closes_runner_after_cancellation():
    started = asyncio.Event()

    class Runner:
        close_count = 0
        on_done_count = 0

        async def run(self, task):
            started.set()
            await asyncio.Event().wait()

        async def on_done(self, task):
            self.on_done_count += 1

        async def aclose(self):
            self.close_count += 1

    runner = Runner()
    task = object.__new__(RedisStreamTask)
    task._id = "cancelled-runner-task"
    task._runner = runner
    task._runner_closed = False
    task._execution_task = asyncio.create_task(task._execute_task())
    task._rerun_requested = False
    task._cancel_requested = False
    RedisStreamTask._task_registry = {task.id: task}
    try:
        await started.wait()
        await task.cancel()
        assert await task.wait_for_done(0.5) is True
    finally:
        RedisStreamTask._task_registry.clear()

    assert runner.on_done_count == 1
    assert runner.close_count == 1


async def test_celery_worker_closes_runner_after_execution_exception(monkeypatch):
    class Runner:
        close_count = 0
        on_done_count = 0

        async def run(self, task):
            raise RuntimeError("worker runner failed")

        async def on_done(self, task):
            self.on_done_count += 1

        async def aclose(self):
            self.close_count += 1

    runner = Runner()

    class Factory:
        async def create_runner(self, params):
            return runner

    async def noop(*args, **kwargs):
        return None

    async def claim(*args, **kwargs):
        return "claimed"

    async def done(*args, **kwargs):
        return "done"

    async def wait_forever(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(celery_worker, "_ensure_initialized", noop)
    monkeypatch.setattr(celery_worker, "claim_dispatch", claim)
    monkeypatch.setattr(celery_worker, "finish_cycle", done)
    monkeypatch.setattr(celery_worker, "_watch_cancel", wait_forever)
    monkeypatch.setattr(
        CeleryTask,
        "get_runner_factory",
        classmethod(lambda cls: Factory()),
    )

    with pytest.raises(celery_worker.WorkerStateRetry) as retry:
        await celery_worker._run_agent(
            "celery-task",
            {"session_id": "session-1"},
            "generation-1",
        )

    assert retry.value.mode == "run"
    assert retry.value.claim_id
    assert runner.on_done_count == 1
    assert runner.close_count == 1


async def test_celery_worker_preserves_rerun_after_generation_exception(
    monkeypatch,
):
    run_trace = []

    class Runner:
        def __init__(self, generation):
            self.generation = generation
            self.close_count = 0

        async def run(self, task):
            run_trace.append(("run", self.generation))
            if self.generation == 1:
                raise RuntimeError("first generation failed")

        async def on_done(self, task):
            run_trace.append(("done", self.generation))

        async def aclose(self):
            self.close_count += 1

    runners = []

    class Factory:
        async def create_runner(self, params):
            runner = Runner(len(runners) + 1)
            runners.append(runner)
            return runner

    decisions = iter(("continue", "done"))

    async def noop(*args, **kwargs):
        return None

    async def claim(*args, **kwargs):
        return "claimed"

    async def finish(*args, **kwargs):
        assert kwargs.get("force_done") is False
        return next(decisions)

    async def wait_forever(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(celery_worker, "_ensure_initialized", noop)
    monkeypatch.setattr(celery_worker, "claim_dispatch", claim)
    monkeypatch.setattr(celery_worker, "finish_cycle", finish)
    monkeypatch.setattr(celery_worker, "_watch_cancel", wait_forever)
    monkeypatch.setattr(
        CeleryTask,
        "get_runner_factory",
        classmethod(lambda cls: Factory()),
    )

    with pytest.raises(celery_worker.WorkerStateRetry) as retry:
        await celery_worker._run_agent(
            "celery-task",
            {"session_id": "session-1"},
            "generation-1",
        )
    assert retry.value.mode == "run"

    await celery_worker._run_agent(
        "celery-task",
        {"session_id": "session-1"},
        "generation-1",
        retry.value.claim_id,
        retry.value.mode,
        retry.value.cycle_id,
    )

    assert run_trace == [
        ("run", 1),
        ("done", 1),
        ("run", 2),
        ("done", 2),
        ("run", 3),
        ("done", 3),
    ]
    assert [runner.close_count for runner in runners] == [1, 1, 1]


async def test_celery_task_aborts_generation_when_broker_send_fails(monkeypatch):
    trace = []

    async def dispatch(*args, **kwargs):
        trace.append("reserve")
        return DispatchRequest("dispatch", "generation-1")

    async def abort(task_id, token):
        trace.append(("abort", task_id, token))
        return True

    async def unexpected_publish(*args, **kwargs):
        raise AssertionError("failed broker dispatch must not be published")

    def fail_send(*args, **kwargs):
        trace.append("send")
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        "app.infrastructure.external.task.celery_task.request_dispatch", dispatch
    )
    monkeypatch.setattr(
        "app.infrastructure.external.task.celery_task.abort_dispatch", abort
    )
    monkeypatch.setattr(
        "app.infrastructure.external.task.celery_task.publish_dispatch",
        unexpected_publish,
    )
    monkeypatch.setattr(
        "app.infrastructure.external.task.celery_task.celery_app.send_task", fail_send
    )

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await CeleryTask("dispatch-failure", params={"session_id": "s"}).run()

    assert trace == [
        "reserve",
        "send",
        ("abort", "dispatch-failure", "generation-1"),
    ]


async def test_celery_task_marks_rerun_without_duplicate_broker_send(monkeypatch):
    async def dispatch(*args, **kwargs):
        return DispatchRequest("rerun", "unused")

    def unexpected_send(*args, **kwargs):
        raise AssertionError("active task must not get a duplicate Celery delivery")

    monkeypatch.setattr(
        "app.infrastructure.external.task.celery_task.request_dispatch", dispatch
    )
    monkeypatch.setattr(
        "app.infrastructure.external.task.celery_task.celery_app.send_task",
        unexpected_send,
    )

    await CeleryTask("active-task", params={"session_id": "s"}).run()


async def test_celery_worker_factory_failure_terminalizes_before_done(monkeypatch):
    trace = []

    class Factory:
        async def create_runner(self, params):
            trace.append("factory")
            raise RuntimeError("sandbox bootstrap failed")

    class Repository:
        async def recover_factory_failure_for_task(self, session_id, **kwargs):
            trace.append(
                (
                    "terminal",
                    session_id,
                    kwargs["user_id"],
                    kwargs["task_id"],
                    kwargs["error"],
                )
            )
            return True

    async def initialized():
        return None

    async def claim(*args, **kwargs):
        trace.append("claim")
        return "claimed"

    async def finish(*args, **kwargs):
        trace.append(("finish", kwargs.get("force_done")))
        return "done"

    monkeypatch.setattr(celery_worker, "_ensure_initialized", initialized)
    monkeypatch.setattr(celery_worker, "claim_dispatch", claim)
    monkeypatch.setattr(celery_worker, "finish_cycle", finish)
    monkeypatch.setattr(celery_worker, "_turn_submission_repository", Repository())
    monkeypatch.setattr(
        CeleryTask,
        "get_runner_factory",
        classmethod(lambda cls: Factory()),
    )

    await celery_worker._run_agent(
        "task-1",
        {"session_id": "session-1", "user_id": "user-1"},
        "generation-1",
    )

    assert trace[0:2] == ["claim", "factory"]
    assert trace[2][0:4] == ("terminal", "session-1", "user-1", "task-1")
    assert trace[2][4] == celery_worker._FACTORY_FAILURE_MESSAGE
    assert trace[3] == ("finish", False)


async def test_celery_worker_retries_without_done_when_terminalization_fails(
    monkeypatch,
):
    finish_calls = []

    class Factory:
        async def create_runner(self, params):
            raise RuntimeError("sandbox bootstrap failed")

    class Repository:
        async def recover_factory_failure_for_task(self, session_id, **kwargs):
            raise RuntimeError("mongo unavailable")

    async def initialized():
        return None

    async def claim(*args, **kwargs):
        return "claimed"

    async def finish(*args, **kwargs):
        finish_calls.append((args, kwargs))
        return "done"

    monkeypatch.setattr(celery_worker, "_ensure_initialized", initialized)
    monkeypatch.setattr(celery_worker, "claim_dispatch", claim)
    monkeypatch.setattr(celery_worker, "finish_cycle", finish)
    monkeypatch.setattr(celery_worker, "_turn_submission_repository", Repository())
    monkeypatch.setattr(
        CeleryTask,
        "get_runner_factory",
        classmethod(lambda cls: Factory()),
    )

    with pytest.raises(celery_worker.WorkerStateRetry) as retry:
        await celery_worker._run_agent(
            "task-1",
            {"session_id": "session-1", "user_id": "user-1"},
            "generation-1",
        )

    assert retry.value.mode == "factory"
    assert finish_calls == []


async def test_celery_worker_keeps_running_while_prior_mongo_claim_is_live(
    monkeypatch,
):
    finish_calls = []

    class Factory:
        async def create_runner(self, params):
            raise RuntimeError("sandbox bootstrap failed")

    class Repository:
        async def recover_factory_failure_for_task(self, session_id, **kwargs):
            return False

    async def initialized():
        return None

    async def claim(*args, **kwargs):
        return "claimed"

    async def finish(*args, **kwargs):
        finish_calls.append((args, kwargs))
        return "done"

    monkeypatch.setattr(celery_worker, "_ensure_initialized", initialized)
    monkeypatch.setattr(celery_worker, "claim_dispatch", claim)
    monkeypatch.setattr(celery_worker, "finish_cycle", finish)
    monkeypatch.setattr(celery_worker, "_turn_submission_repository", Repository())
    monkeypatch.setattr(
        CeleryTask,
        "get_runner_factory",
        classmethod(lambda cls: Factory()),
    )

    with pytest.raises(celery_worker.WorkerStateRetry) as retry:
        await celery_worker._run_agent(
            "task-1",
            {"session_id": "session-1", "user_id": "user-1"},
            "generation-1",
            "worker-1",
        )

    assert retry.value.mode == "factory"
    assert retry.value.claim_id == "worker-1"
    assert finish_calls == []


async def test_celery_worker_mongo_fencing_loss_retries_without_finish(
    monkeypatch,
):
    finish_calls = []

    class Runner:
        async def run(self, task):
            raise asyncio.CancelledError("mongo_claim_lost")

        async def on_done(self, task):
            return None

        async def aclose(self):
            return None

    class Factory:
        async def create_runner(self, params):
            return Runner()

    async def initialized():
        return None

    async def claim(*args, **kwargs):
        return "claimed"

    async def finish(*args, **kwargs):
        finish_calls.append((args, kwargs))
        return "done"

    async def wait_forever(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(celery_worker, "_ensure_initialized", initialized)
    monkeypatch.setattr(celery_worker, "claim_dispatch", claim)
    monkeypatch.setattr(celery_worker, "finish_cycle", finish)
    monkeypatch.setattr(celery_worker, "_watch_cancel", wait_forever)
    monkeypatch.setattr(
        CeleryTask,
        "get_runner_factory",
        classmethod(lambda cls: Factory()),
    )

    with pytest.raises(celery_worker.WorkerStateRetry) as retry:
        await celery_worker._run_agent(
            "task-1", {}, "generation-1", "worker-1"
        )

    assert retry.value.mode == "run"
    assert retry.value.claim_id == "worker-1"
    assert finish_calls == []


async def test_celery_worker_reuses_claim_nonce_after_lost_claim_response(
    monkeypatch,
):
    claim_ids = []
    runner_calls = []

    class Runner:
        async def run(self, task):
            runner_calls.append("run")

        async def on_done(self, task):
            return None

        async def aclose(self):
            return None

    class Factory:
        async def create_runner(self, params):
            return Runner()

    async def initialized():
        return None

    async def claim(task_id, token, claim_id):
        claim_ids.append(claim_id)
        if len(claim_ids) == 1:
            # Simulate Redis committing RUNNING and the network dropping the
            # EVAL response. The Celery retry must carry the same owner nonce.
            raise ConnectionError("claim response lost")
        return "claimed"

    async def finish(*args, **kwargs):
        return "done"

    async def wait_forever(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(celery_worker, "_ensure_initialized", initialized)
    monkeypatch.setattr(celery_worker, "claim_dispatch", claim)
    monkeypatch.setattr(celery_worker, "finish_cycle", finish)
    monkeypatch.setattr(celery_worker, "_watch_cancel", wait_forever)
    monkeypatch.setattr(
        CeleryTask,
        "get_runner_factory",
        classmethod(lambda cls: Factory()),
    )

    with pytest.raises(celery_worker.WorkerStateRetry) as retry:
        await celery_worker._run_agent(
            "task-claim-loss", {}, "generation-1"
        )
    assert retry.value.mode == "claim"
    assert retry.value.claim_id

    await celery_worker._run_agent(
        "task-claim-loss",
        {},
        "generation-1",
        retry.value.claim_id,
        retry.value.mode,
        retry.value.cycle_id,
    )

    assert claim_ids == [retry.value.claim_id, retry.value.claim_id]
    assert runner_calls == ["run"]


async def test_celery_worker_replays_same_finish_cycle_after_lost_response(
    monkeypatch,
):
    runners = []
    finish_cycles = []

    class Runner:
        def __init__(self, generation):
            self.generation = generation

        async def run(self, task):
            return None

        async def on_done(self, task):
            return None

        async def aclose(self):
            return None

    class Factory:
        async def create_runner(self, params):
            runner = Runner(len(runners) + 1)
            runners.append(runner)
            return runner

    async def initialized():
        return None

    async def claim(*args, **kwargs):
        return "claimed"

    async def finish(task_id, token, claim_id, cycle_id, **kwargs):
        finish_cycles.append(cycle_id)
        if len(finish_cycles) == 1:
            # Redis consumed rerun and persisted last_finish_cycle=continue,
            # but the caller did not receive that response.
            raise ConnectionError("finish response lost")
        if len(finish_cycles) == 2:
            return "continue"
        return "done"

    async def wait_forever(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(celery_worker, "_ensure_initialized", initialized)
    monkeypatch.setattr(celery_worker, "claim_dispatch", claim)
    monkeypatch.setattr(celery_worker, "finish_cycle", finish)
    monkeypatch.setattr(celery_worker, "_watch_cancel", wait_forever)
    monkeypatch.setattr(
        CeleryTask,
        "get_runner_factory",
        classmethod(lambda cls: Factory()),
    )

    with pytest.raises(celery_worker.WorkerStateRetry) as retry:
        await celery_worker._run_agent(
            "task-finish-loss", {}, "generation-1", "worker-1"
        )
    assert retry.value.mode == "finish"
    assert retry.value.cycle_id

    await celery_worker._run_agent(
        "task-finish-loss",
        {},
        "generation-1",
        retry.value.claim_id,
        retry.value.mode,
        retry.value.cycle_id,
    )

    assert finish_cycles[0] == finish_cycles[1]
    assert len(runners) == 2


async def test_factory_failure_rechecks_new_rerun_before_task_done(monkeypatch):
    terminalizations = []
    finish_decisions = iter(("continue", "done"))

    class Factory:
        async def create_runner(self, params):
            raise RuntimeError("factory unavailable")

    class Repository:
        async def recover_factory_failure_for_task(self, session_id, **kwargs):
            terminalizations.append((session_id, kwargs["task_id"]))
            return True

    async def initialized():
        return None

    async def claim(*args, **kwargs):
        return "claimed"

    async def finish(*args, **kwargs):
        assert kwargs["force_done"] is False
        return next(finish_decisions)

    monkeypatch.setattr(celery_worker, "_ensure_initialized", initialized)
    monkeypatch.setattr(celery_worker, "claim_dispatch", claim)
    monkeypatch.setattr(celery_worker, "finish_cycle", finish)
    monkeypatch.setattr(celery_worker, "_turn_submission_repository", Repository())
    monkeypatch.setattr(
        CeleryTask,
        "get_runner_factory",
        classmethod(lambda cls: Factory()),
    )

    await celery_worker._run_agent(
        "task-1",
        {"session_id": "session-1", "user_id": "user-1"},
        "generation-1",
        "worker-1",
    )

    assert terminalizations == [
        ("session-1", "task-1"),
        ("session-1", "task-1"),
    ]
