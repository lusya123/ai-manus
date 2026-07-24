import asyncio
import time
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from app.application.services.agent_service import AgentService
from app.domain.external.coordination import (
    SessionLifecycleLeaseUnavailableError,
)
from app.domain.models.event import AgentEvent, DoneEvent, ErrorEvent, MessageEvent
from app.domain.models.session import Session
from app.infrastructure.external.coordination import RedisSessionLifecycleLease
from app.infrastructure.external.sandbox.passthrough_provisioner import (
    PassthroughSandboxProvisioner,
)


class FakeRedis:
    """Small atomic Redis model shared by two lease-manager instances."""

    def __init__(self):
        self.values: dict[str, tuple[str, float]] = {}
        self.lock = asyncio.Lock()
        self.fail = False
        self.eval_calls = 0
        self.fail_eval_at: int | None = None

    def _purge_expired(self, key: str) -> None:
        value = self.values.get(key)
        if value and value[1] <= time.monotonic():
            self.values.pop(key, None)

    async def set(self, key, value, *, nx=False, ex=None):
        if self.fail:
            raise ConnectionError("redis unavailable")
        async with self.lock:
            self._purge_expired(key)
            if nx and key in self.values:
                return None
            self.values[key] = (value, time.monotonic() + float(ex or 60))
            return True

    async def eval(self, script, key_count, key, *args):
        assert key_count == 1
        if self.fail:
            raise ConnectionError("redis unavailable")
        self.eval_calls += 1
        if self.fail_eval_at is not None and self.eval_calls >= self.fail_eval_at:
            raise ConnectionError("redis renewal unavailable")
        async with self.lock:
            self._purge_expired(key)
            current = self.values.get(key)
            owner = args[0]
            if not current or current[0] != owner:
                return 0
            if "redis.call('expire'" in script:
                self.values[key] = (
                    current[0],
                    time.monotonic() + float(args[1]),
                )
            else:
                self.values.pop(key, None)
            return 1


class SharedSessionRepository:
    def __init__(self, session: Session):
        self.stored: Session | None = session.model_copy(deep=True)
        self.deleted = False
        self.revive_attempts = 0
        self.message_events: list[MessageEvent] = []

    async def find_by_id_and_user_id(self, session_id, user_id):
        await asyncio.sleep(0)
        if (
            self.stored is None
            or self.stored.id != session_id
            or self.stored.user_id != user_id
        ):
            return None
        return self.stored.model_copy(deep=True)

    async def save(self, session):
        await asyncio.sleep(0)
        if self.deleted:
            self.revive_attempts += 1
            raise RuntimeError("deleted session must not be revived")
        self.stored = session.model_copy(deep=True)

    async def update_runtime_ownership(
        self,
        session_id,
        sandbox_id,
        task_id,
        sandbox_provider=None,
        task_sandbox_id=None,
    ):
        await asyncio.sleep(0)
        if self.stored is None or self.stored.id != session_id:
            self.revive_attempts += 1
            raise RuntimeError("deleted session must not be revived")
        self.stored.sandbox_id = sandbox_id
        self.stored.sandbox_provider = sandbox_provider
        self.stored.task_id = task_id
        self.stored.task_sandbox_id = task_sandbox_id

    async def update_latest_message(self, session_id, message, timestamp):
        if self.stored is None:
            raise RuntimeError("session was deleted")
        self.stored.latest_message = message
        self.stored.latest_message_at = timestamp

    async def add_event(self, session_id, event):
        if self.stored is None:
            raise RuntimeError("session was deleted")
        copied = event.model_copy(deep=True)
        self.stored.events.append(copied)
        if isinstance(copied, MessageEvent) and copied.role == "user":
            self.message_events.append(copied)

    async def update_unread_message_count(self, session_id, count):
        # A response reader may finish just after a serialized delete. This
        # post-processing update is deliberately non-creating in Mongo too.
        if self.stored is not None:
            self.stored.unread_message_count = count

    async def update_status(self, session_id, status):
        if self.stored is not None:
            self.stored.status = status

    async def delete(self, session_id):
        if self.stored is not None and self.stored.id == session_id:
            self.stored = None
            self.deleted = True


class SharedAgentRepository:
    def __init__(self):
        self.agents = {"agent-1": SimpleNamespace(id="agent-1")}

    async def find_by_id(self, agent_id):
        return self.agents.get(agent_id)

    async def delete(self, agent_id):
        self.agents.pop(agent_id, None)

    async def save(self, agent):
        self.agents[agent.id] = agent


class SandboxBackend:
    def __init__(self):
        self.instances = {}
        self.create_count = 0
        self.destroy_count = 0
        self.block_create = False
        self.create_started = asyncio.Event()
        self.allow_create = asyncio.Event()
        self.allow_create.set()
        self.block_destroy = False
        self.destroy_started = asyncio.Event()
        self.allow_destroy = asyncio.Event()
        self.allow_destroy.set()


def sandbox_class(backend: SandboxBackend):
    class Sandbox:
        def __init__(self, sandbox_id):
            self.id = sandbox_id

        async def destroy(self):
            backend.destroy_started.set()
            if backend.block_destroy:
                await backend.allow_destroy.wait()
            backend.destroy_count += 1
            return True

    class SandboxClass:
        @classmethod
        async def create(cls):
            backend.create_count += 1
            backend.create_started.set()
            if backend.block_create:
                await backend.allow_create.wait()
            sandbox = Sandbox(f"sandbox-{backend.create_count}")
            backend.instances[sandbox.id] = sandbox
            return sandbox

        @classmethod
        async def get(cls, sandbox_id):
            return backend.instances.get(sandbox_id)

    return SandboxClass, Sandbox


def task_class():
    class InputStream:
        def __init__(self):
            self.messages: list[tuple[str, str]] = []
            self.changed = asyncio.Condition()

        async def put(self, value):
            async with self.changed:
                event_id = f"{len(self.messages) + 1}-0"
                self.messages.append((event_id, value))
                self.changed.notify_all()
                return event_id

    class OutputStream:
        def __init__(self):
            self.events: list[tuple[str, str]] = []
            self.changed = asyncio.Condition()

        @staticmethod
        def sequence(event_id):
            return int((event_id or "0").split("-", 1)[0])

        async def get_latest_id(self):
            return self.events[-1][0] if self.events else "0"

        async def put(self, value):
            async with self.changed:
                event_id = f"{len(self.events) + 1}-0"
                self.events.append((event_id, value))
                self.changed.notify_all()
                return event_id

        async def get(self, start_id=None, block_ms=0):
            sequence = self.sequence(start_id)

            def next_event():
                return next(
                    (
                        item
                        for item in self.events
                        if self.sequence(item[0]) > sequence
                    ),
                    None,
                )

            async with self.changed:
                item = next_event()
                if item is None:
                    try:
                        await asyncio.wait_for(
                            self.changed.wait_for(lambda: next_event() is not None),
                            timeout=(block_ms or 1000) / 1000,
                        )
                    except TimeoutError:
                        return None, None
                    item = next_event()
                return item

    class Task:
        def __init__(self, task_id):
            self.id = task_id
            self.input_stream = InputStream()
            self.output_stream = OutputStream()
            self.worker: asyncio.Task | None = None
            self.processed = 0
            self.processed_changed = asyncio.Condition()
            self.cancelled = False
            self.run_count = 0

        async def is_done(self):
            return self.cancelled or (
                self.worker is not None and self.worker.done()
            )

        async def run(self):
            self.run_count += 1
            target = len(self.input_stream.messages)
            if self.worker is None or self.worker.done():
                self.cancelled = False
                self.worker = asyncio.create_task(self._work())
            async with self.processed_changed:
                await self.processed_changed.wait_for(
                    lambda: self.processed >= target
                )

        async def _work(self):
            try:
                while not self.cancelled:
                    async with self.input_stream.changed:
                        if self.processed >= len(self.input_stream.messages):
                            try:
                                await asyncio.wait_for(
                                    self.input_stream.changed.wait_for(
                                        lambda: self.processed
                                        < len(self.input_stream.messages)
                                    ),
                                    timeout=0.25,
                                )
                            except TimeoutError:
                                return
                        pending = self.input_stream.messages[self.processed :]

                    for turn_id, value in pending:
                        event = TypeAdapter(AgentEvent).validate_json(value)
                        await self.output_stream.put(
                            MessageEvent(
                                message=f"answer:{event.message}",
                                turn_id=turn_id,
                            ).model_dump_json()
                        )
                        await self.output_stream.put(
                            DoneEvent(turn_id=turn_id).model_dump_json()
                        )
                        async with self.processed_changed:
                            self.processed += 1
                            self.processed_changed.notify_all()
            except asyncio.CancelledError:
                raise

        async def cancel(self):
            self.cancelled = True
            if self.worker is not None and not self.worker.done():
                self.worker.cancel()
            return True

        async def wait_for_done(self, timeout_seconds):
            if self.worker is None:
                return True
            try:
                await asyncio.wait_for(
                    asyncio.shield(self.worker), timeout=timeout_seconds
                )
            except asyncio.CancelledError:
                if self.worker.done():
                    return True
                raise
            except TimeoutError:
                return False
            return self.worker.done()

    class TaskClass:
        registry = {}
        create_count = 0

        @classmethod
        def create(cls, params):
            cls.create_count += 1
            task = Task(f"task-{cls.create_count}")
            cls.registry[task.id] = task
            return task

        @classmethod
        async def get(cls, task_id):
            return cls.registry.get(task_id)

    return TaskClass, Task


def make_services(
    repository,
    agent_repository,
    sandbox_cls,
    task_cls,
    redis,
):
    def make_one():
        return AgentService(
            agent_repository=agent_repository,
            session_repository=repository,
            sandbox_cls=sandbox_cls,
            task_cls=task_cls,
            file_storage=SimpleNamespace(),
            mcp_repository=SimpleNamespace(),
            sandbox_provisioner=PassthroughSandboxProvisioner(
                sandbox_cls, repository
            ),
            session_lifecycle_lease=RedisSessionLifecycleLease(
                redis,
                ttl_seconds=3,
                acquire_timeout_seconds=1,
                retry_interval_seconds=0.001,
                command_timeout_seconds=0.5,
                renew_interval_seconds=0.05,
            ),
        )

    return make_one(), make_one()


def test_lease_rejects_timing_that_can_expire_before_renewal_finishes():
    with pytest.raises(ValueError, match="command timeout"):
        RedisSessionLifecycleLease(
            FakeRedis(), ttl_seconds=3, command_timeout_seconds=3
        )

    with pytest.raises(ValueError, match="renewal interval"):
        RedisSessionLifecycleLease(
            FakeRedis(), ttl_seconds=3, renew_interval_seconds=3
        )

    with pytest.raises(ValueError, match="plus Redis command timeout"):
        RedisSessionLifecycleLease(
            FakeRedis(),
            ttl_seconds=3,
            command_timeout_seconds=1.5,
            renew_interval_seconds=1.5,
        )


def test_local_task_backend_rejects_declared_multiple_api_processes(monkeypatch):
    from app.interfaces import dependencies

    monkeypatch.setattr(
        dependencies,
        "get_settings",
        lambda: SimpleNamespace(
            task_backend="local",
            backend_replica_count=2,
        ),
    )

    with pytest.raises(RuntimeError, match="TASK_BACKEND=celery"):
        dependencies._get_task_cls()


async def test_separate_local_process_registries_cannot_recover_each_others_task():
    """Document why local mode is guarded to one backend process.

    Two independently-created task classes model imports in separate Python
    processes: each registry can only resolve handles it created itself.
    """
    first_process_tasks, _ = task_class()
    second_process_tasks, _ = task_class()
    params = {
        "session_id": "session-1",
        "agent_id": "agent-1",
        "user_id": "owner",
        "sandbox_id": "sandbox-1",
    }

    first_task = first_process_tasks.create(params)

    assert await first_process_tasks.get(first_task.id) is first_task
    assert await second_process_tasks.get(first_task.id) is None


async def collect(stream):
    return [event async for event in stream]


async def test_two_replicas_create_one_sandbox_task_and_keep_turns_separate():
    redis = FakeRedis()
    repository = SharedSessionRepository(
        Session(id="session-1", user_id="owner", agent_id="agent-1")
    )
    agent_repository = SharedAgentRepository()
    sandbox_backend = SandboxBackend()
    SandboxClass, _ = sandbox_class(sandbox_backend)
    TaskClass, _ = task_class()
    first_service, second_service = make_services(
        repository, agent_repository, SandboxClass, TaskClass, redis
    )

    first, second = await asyncio.gather(
        collect(first_service.chat("session-1", "owner", message="first")),
        collect(second_service.chat("session-1", "owner", message="second")),
    )

    assert sandbox_backend.create_count == 1
    assert TaskClass.create_count == 1
    assert len(repository.message_events) == 2
    assert [event.type for event in first] == ["message", "done"]
    assert [event.type for event in second] == ["message", "done"]
    assert {event.turn_id for event in first} == {"1-0"}
    assert {event.turn_id for event in second} == {"2-0"}
    assert [
        event.message for event in first if isinstance(event, MessageEvent)
    ] == ["answer:first"]
    assert [
        event.message for event in second if isinstance(event, MessageEvent)
    ] == ["answer:second"]

    task = next(iter(TaskClass.registry.values()))
    if task.worker and not task.worker.done():
        task.worker.cancel()
        await asyncio.gather(task.worker, return_exceptions=True)


async def test_enqueue_wins_delete_race_and_deleted_session_never_revives():
    redis = FakeRedis()
    repository = SharedSessionRepository(
        Session(id="session-1", user_id="owner", agent_id="agent-1")
    )
    agent_repository = SharedAgentRepository()
    sandbox_backend = SandboxBackend()
    sandbox_backend.block_create = True
    sandbox_backend.allow_create.clear()
    SandboxClass, _ = sandbox_class(sandbox_backend)
    TaskClass, _ = task_class()
    chat_service, delete_service = make_services(
        repository, agent_repository, SandboxClass, TaskClass, redis
    )

    chat_task = asyncio.create_task(
        collect(chat_service.chat("session-1", "owner", message="first"))
    )
    await sandbox_backend.create_started.wait()
    delete_task = asyncio.create_task(
        delete_service.delete_session("session-1", "owner")
    )
    await asyncio.sleep(0.02)
    assert not delete_task.done()

    sandbox_backend.allow_create.set()
    events, _ = await asyncio.gather(chat_task, delete_task)

    assert [event.type for event in events] == ["message", "done"]
    assert repository.stored is None
    assert repository.revive_attempts == 0
    assert sandbox_backend.create_count == 1
    assert sandbox_backend.destroy_count == 1
    assert TaskClass.create_count == 1

    after_delete = await collect(
        chat_service.chat("session-1", "owner", message="must-not-revive")
    )
    assert len(after_delete) == 1
    assert isinstance(after_delete[0], ErrorEvent)
    assert after_delete[0].error == "Session not found"
    assert sandbox_backend.create_count == 1
    assert repository.revive_attempts == 0


async def test_delete_wins_enqueue_race_and_waiting_replica_rechecks_mongo():
    redis = FakeRedis()
    session = Session(
        id="session-1",
        user_id="owner",
        agent_id="agent-1",
        sandbox_id="sandbox-existing",
        sandbox_provider="docker",
        task_id="task-existing",
    )
    repository = SharedSessionRepository(session)
    agent_repository = SharedAgentRepository()
    sandbox_backend = SandboxBackend()
    sandbox_backend.block_destroy = True
    sandbox_backend.allow_destroy.clear()
    SandboxClass, Sandbox = sandbox_class(sandbox_backend)
    sandbox_backend.instances["sandbox-existing"] = Sandbox("sandbox-existing")
    TaskClass, Task = task_class()
    TaskClass.registry["task-existing"] = Task("task-existing")
    delete_service, chat_service = make_services(
        repository, agent_repository, SandboxClass, TaskClass, redis
    )

    delete_task = asyncio.create_task(
        delete_service.delete_session("session-1", "owner")
    )
    await sandbox_backend.destroy_started.wait()
    chat_task = asyncio.create_task(
        collect(chat_service.chat("session-1", "owner", message="late"))
    )
    await asyncio.sleep(0.02)
    assert sandbox_backend.create_count == 0
    assert not chat_task.done()

    sandbox_backend.allow_destroy.set()
    _, events = await asyncio.gather(delete_task, chat_task)

    assert repository.stored is None
    assert repository.revive_attempts == 0
    assert sandbox_backend.create_count == 0
    assert sandbox_backend.destroy_count == 1
    assert TaskClass.create_count == 0
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].error == "Session not found"


async def test_redis_failure_fails_closed_for_enqueue_and_delete():
    redis = FakeRedis()
    redis.fail = True
    repository = SharedSessionRepository(
        Session(id="session-1", user_id="owner", agent_id="agent-1")
    )
    agent_repository = SharedAgentRepository()
    sandbox_backend = SandboxBackend()
    SandboxClass, _ = sandbox_class(sandbox_backend)
    TaskClass, _ = task_class()
    chat_service, delete_service = make_services(
        repository, agent_repository, SandboxClass, TaskClass, redis
    )

    events = await collect(
        chat_service.chat("session-1", "owner", message="blocked")
    )
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert "coordination is unavailable" in events[0].error
    assert sandbox_backend.create_count == 0
    assert TaskClass.create_count == 0
    assert repository.message_events == []

    with pytest.raises(SessionLifecycleLeaseUnavailableError):
        await delete_service.delete_session("session-1", "owner")
    assert repository.stored is not None
    assert agent_repository.agents


async def test_renewal_failure_cancels_mutation_and_release_is_owner_safe():
    redis = FakeRedis()
    redis.fail_eval_at = 2  # Initial ownership check succeeds; renewal fails.
    lease = RedisSessionLifecycleLease(
        redis,
        ttl_seconds=3,
        acquire_timeout_seconds=0.1,
        retry_interval_seconds=0.001,
        command_timeout_seconds=0.1,
        renew_interval_seconds=0.01,
    )
    operation_started = asyncio.Event()
    operation_cancelled = asyncio.Event()

    async def long_mutation():
        operation_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            operation_cancelled.set()
            raise

    with pytest.raises(SessionLifecycleLeaseUnavailableError):
        await lease.run_exclusive("session-1", long_mutation)
    assert operation_started.is_set()
    assert operation_cancelled.is_set()

    # A stale owner must not delete a key that now belongs to another replica.
    redis.fail_eval_at = None
    key = lease._lease_key("session-owner-safety")
    await redis.set(key, "new-owner", nx=True, ex=3)
    assert await lease._release(key, "old-owner") is False
    assert redis.values[key][0] == "new-owner"
