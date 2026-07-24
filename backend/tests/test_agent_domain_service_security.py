import asyncio
from types import MethodType, SimpleNamespace
from weakref import WeakValueDictionary

import pytest
from pydantic import TypeAdapter

from app.domain.models.event import (
    AgentEvent,
    DoneEvent,
    ErrorEvent,
    MessageEvent,
    TitleEvent,
)
from app.domain.models.file import FileInfo
from app.domain.models.session import Session, SessionStatus
from app.domain.models.turn_submission import TurnSubmission
from app.domain.models.turn_submission import (
    TurnSubmissionConflictError,
    TurnSubmissionUnavailableError,
)
from app.domain.services.agent_domain_service import AgentDomainService
from app.application.services.agent_service import AgentService
from app.domain.external.sandbox import (
    SandboxProvisioningError,
    SandboxUnavailableError,
)
from app.infrastructure.external.sandbox.passthrough_provisioner import (
    PassthroughSandboxProvisioner,
)


def _service(repository, sandbox_cls, task_cls):
    provisioner = PassthroughSandboxProvisioner(sandbox_cls, repository)
    return AgentDomainService(
        agent_repository=SimpleNamespace(),
        session_repository=repository,
        sandbox_cls=sandbox_cls,
        task_cls=task_cls,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        sandbox_provisioner=provisioner,
    )


async def _collect(stream):
    return [event async for event in stream]


async def test_non_owner_chat_never_mutates_target_session():
    class Repository:
        def __init__(self):
            self.mutations = []

        async def find_by_id_and_user_id(self, session_id, user_id):
            return None

        async def add_event(self, session_id, event):
            self.mutations.append(("event", session_id, event))

        async def update_unread_message_count(self, session_id, count):
            self.mutations.append(("unread", session_id, count))

    repository = Repository()
    service = _service(repository, SimpleNamespace(), SimpleNamespace())

    events = await _collect(
        service.chat("victim-session", "attacker", message="tamper")
    )

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].error == "Session not found"
    assert repository.mutations == []


def _attachment_acceptance_service(file_storage):
    session = Session(
        id="session-attachment",
        user_id="owner",
        agent_id="agent-attachment",
    )

    class SessionRepository:
        def __init__(self):
            self.events = []
            self.session = session

        async def find_by_id_and_user_id(self, session_id, user_id):
            if session_id == session.id and user_id == session.user_id:
                return self.session
            return None

    class TurnRepository:
        def __init__(self):
            self.candidates = []
            self.turn = None

        async def find(self, session_id, submission_id):
            if (
                self.turn is not None
                and self.turn.session_id == session_id
                and self.turn.submission_id == submission_id
            ):
                return self.turn
            return None

        async def accept(self, candidate):
            self.candidates.append(candidate.model_copy(deep=True))
            if self.turn is not None:
                if (
                    self.turn.user_id != candidate.user_id
                    or self.turn.agent_id != candidate.agent_id
                    or self.turn.request_hash != candidate.request_hash
                ):
                    raise TurnSubmissionConflictError("conflicting retry")
                return self.turn, False
            self.turn = candidate.model_copy(deep=True)
            return self.turn, True

    turn_repository = TurnRepository()
    session_repository = SessionRepository()
    service = object.__new__(AgentDomainService)
    service._session_repository = session_repository
    service._turn_submission_repository = turn_repository
    service._file_storage = file_storage
    service._session_lifecycle_lease = None
    service._session_locks = WeakValueDictionary()

    async def continue_without_dispatch(self, _session, turn):
        return turn

    service._continue_durable_turn_locked = MethodType(
        continue_without_dispatch, service
    )
    return service, session_repository, turn_repository


async def test_deleting_session_rejects_turn_before_durable_acceptance():
    service, session_repository, turn_repository = (
        _attachment_acceptance_service(SimpleNamespace())
    )
    session_repository.session.deleting = True

    with pytest.raises(
        TurnSubmissionUnavailableError, match="deletion is in progress"
    ):
        await service.accept_chat_submission(
            session_id="session-attachment",
            user_id="owner",
            submission_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            message="must not be accepted",
        )

    assert turn_repository.candidates == []


async def test_foreign_attachment_is_rejected_before_durable_acceptance():
    class OwnerScopedStorage:
        async def get_file_info(self, file_id, user_id):
            assert (file_id, user_id) == ("foreign-file", "owner")
            return None

    service, session_repository, turn_repository = (
        _attachment_acceptance_service(OwnerScopedStorage())
    )

    with pytest.raises(ValueError, match="not found or access denied"):
        await service.accept_chat_submission(
            session_id="session-attachment",
            user_id="owner",
            submission_id="11111111-1111-4111-8111-111111111111",
            message="use the attachment",
            attachments=[
                FileInfo(file_id="foreign-file", filename="claimed.txt")
            ],
        )

    assert turn_repository.candidates == []
    assert session_repository.events == []


async def test_owned_attachment_uses_storage_canonical_metadata():
    class OwnerScopedStorage:
        async def get_file_info(self, file_id, user_id):
            assert (file_id, user_id) == ("owned-file", "owner")
            return FileInfo(
                file_id="owned-file",
                filename="canonical.pdf",
                content_type="application/pdf",
                size=42,
                user_id="owner",
                file_url="https://storage.invalid/private-capability",
            )

    service, _, turn_repository = _attachment_acceptance_service(
        OwnerScopedStorage()
    )

    accepted = await service.accept_chat_submission(
        session_id="session-attachment",
        user_id="owner",
        submission_id="22222222-2222-4222-8222-222222222222",
        message="use the attachment",
        attachments=[
            FileInfo(file_id="owned-file", filename="spoofed.exe")
        ],
    )

    assert isinstance(accepted, TurnSubmission)
    input_event = TypeAdapter(AgentEvent).validate_json(accepted.input_json)
    assert isinstance(input_event, MessageEvent)
    assert input_event.attachments == [
        FileInfo(
            file_id="owned-file",
            filename="canonical.pdf",
            content_type="application/pdf",
            size=42,
        )
    ]
    assert "spoofed.exe" not in accepted.input_json
    assert "private-capability" not in accepted.input_json
    assert len(turn_repository.candidates) == 1


async def test_attachment_retry_uses_persisted_turn_when_storage_changes():
    class MutableStorage:
        def __init__(self):
            self.calls = 0

        async def get_file_info(self, file_id, user_id):
            self.calls += 1
            if self.calls == 1:
                return FileInfo(
                    file_id=file_id,
                    filename="canonical-at-accept.txt",
                )
            raise ConnectionError("storage unavailable after acceptance")

    storage = MutableStorage()
    service, _, turn_repository = _attachment_acceptance_service(storage)
    submission_id = "33333333-3333-4333-8333-333333333333"

    first = await service.accept_chat_submission(
        session_id="session-attachment",
        user_id="owner",
        submission_id=submission_id,
        message="use it",
        attachments=[FileInfo(file_id="owned-file", filename="client-a.txt")],
    )
    retried = await service.accept_chat_submission(
        session_id="session-attachment",
        user_id="owner",
        submission_id=submission_id,
        message="use it",
        attachments=[FileInfo(file_id="owned-file", filename="client-b.txt")],
    )

    assert storage.calls == 1
    assert retried.input_json == first.input_json
    assert retried.request_hash == first.request_hash
    assert "canonical-at-accept.txt" in retried.input_json
    assert len(turn_repository.candidates) == 2


async def test_non_owner_cannot_clear_unread_message_count():
    class Repository:
        def __init__(self):
            self.updates = []

        async def find_by_id_and_user_id(self, session_id, user_id):
            return None

        async def update_unread_message_count(self, session_id, count):
            self.updates.append((session_id, count))

    repository = Repository()
    service = AgentService(
        agent_repository=SimpleNamespace(),
        session_repository=repository,
        sandbox_cls=SimpleNamespace(),
        task_cls=SimpleNamespace(),
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
    )

    try:
        await service.clear_unread_message_count("victim-session", "attacker")
    except RuntimeError as exc:
        assert str(exc) == "Session not found"
    else:
        raise AssertionError("non-owner clear must fail")

    assert repository.updates == []


async def test_transient_sandbox_lookup_never_creates_replacement():
    session = Session(
        id="session-1",
        user_id="owner",
        agent_id="agent-1",
        sandbox_id="billable-sandbox",
        sandbox_provider="docker",
    )

    class Repository:
        async def save(self, session):
            raise AssertionError("lookup failure must not mutate ownership")

    class SandboxClass:
        create_count = 0

        @classmethod
        async def get(cls, sandbox_id):
            raise SandboxUnavailableError("provider timeout")

        @classmethod
        async def create(cls):
            cls.create_count += 1
            raise AssertionError("must not create on inconclusive lookup")

    service = _service(Repository(), SandboxClass, SimpleNamespace())

    with pytest.raises(SandboxUnavailableError):
        await service._create_task(session)

    assert session.sandbox_id == "billable-sandbox"
    assert SandboxClass.create_count == 0


async def test_unconfirmed_provisioning_failure_never_persists_adoptable_pointer():
    session = Session(id="session-1", user_id="owner", agent_id="agent-1")

    class Repository:
        def __init__(self):
            self.saved = []

        async def save(self, value):
            self.saved.append(value.model_copy(deep=True))

    class SandboxClass:
        @classmethod
        async def create(cls):
            raise SandboxProvisioningError(
                "orphan-candidate", "rollback not confirmed"
            )

        @classmethod
        async def get(cls, sandbox_id):
            assert sandbox_id == "orphan-candidate"
            return None

    repository = Repository()
    service = _service(repository, SandboxClass, SimpleNamespace())

    with pytest.raises(SandboxProvisioningError):
        await service._create_task(session)

    assert repository.saved == []
    assert session.sandbox_id is None
    assert session.sandbox_provider is None


async def test_failed_initial_save_and_failed_destroy_uses_tombstone_only():
    session = Session(id="session-1", user_id="owner", agent_id="agent-1")

    class Repository:
        def __init__(self):
            self.save_calls = 0
            self.publish_calls = 0
            self.current = session.model_copy(deep=True)

        async def save(self, value):
            self.save_calls += 1
            raise RuntimeError("temporary mongo failure")

        async def find_by_id(self, session_id):
            assert session_id == session.id
            return self.current

        async def claim_runtime_destroy(self, *_args):
            raise AssertionError(
                "an absent pointer must be atomically published as a tombstone"
            )

        async def publish_runtime_destroy_claim(
            self,
            session_id,
            expected_task_id,
            expected_task_sandbox_id,
            expected_sandbox_provider,
            sandbox_id,
            sandbox_provider,
        ):
            assert session_id == session.id
            assert self.current.sandbox_id is None
            assert self.current.sandbox_provider is expected_sandbox_provider
            assert self.current.task_id == expected_task_id
            assert self.current.task_sandbox_id == expected_task_sandbox_id
            self.publish_calls += 1
            self.current.sandbox_id = sandbox_id
            self.current.sandbox_provider = sandbox_provider
            self.current.sandbox_destroying = True
            return True

    class Sandbox:
        id = "sandbox-needs-cleanup"

        def __init__(self):
            self.destroy_calls = 0

        async def destroy(self):
            self.destroy_calls += 1
            return False

    sandbox = Sandbox()

    class SandboxClass:
        @classmethod
        async def create(cls):
            return sandbox

    repository = Repository()

    service = _service(repository, SandboxClass, SimpleNamespace())
    service._sandbox_provisioner._OWNERSHIP_RECONCILE_INTERVAL_SECONDS = 0
    service._sandbox_provisioner._OWNERSHIP_RECONCILE_MAX_ATTEMPTS = 2

    with pytest.raises(
        SandboxProvisioningError,
        match="rollback and ownership persistence",
    ):
        await service._create_task(session)

    assert repository.save_calls == 1
    assert repository.publish_calls == 1
    assert repository.current.sandbox_id == "sandbox-needs-cleanup"
    assert repository.current.sandbox_provider == "docker"
    assert repository.current.sandbox_destroying is True
    assert sandbox.destroy_calls == 2


async def test_task_creation_closes_provisioning_handle_without_deleting_sandbox():
    session = Session(id="session-1", user_id="owner", agent_id="agent-1")

    class Repository:
        async def save(self, value):
            return None

    class Sandbox:
        id = "persisted-sandbox"

        def __init__(self):
            self.close_count = 0
            self.destroy_count = 0

        async def aclose(self):
            self.close_count += 1

        async def destroy(self):
            self.destroy_count += 1
            return True

    sandbox = Sandbox()

    class SandboxClass:
        @classmethod
        async def create(cls):
            return sandbox

    class Task:
        id = "task-1"

    class TaskClass:
        @classmethod
        def create(cls, params):
            return Task()

    service = _service(Repository(), SandboxClass, TaskClass)

    task = await service._create_task(session)

    assert task.id == "task-1"
    assert session.sandbox_id == "persisted-sandbox"
    assert sandbox.close_count == 1
    assert sandbox.destroy_count == 0


async def test_replacement_retires_old_task_stream_before_persisting_new_params():
    trace = []
    session = Session(
        id="session-1",
        user_id="owner",
        agent_id="agent-1",
        sandbox_id="old-provider",
        sandbox_provider="agentbay",
        task_id="old-task",
    )

    class Repository:
        async def update_runtime_ownership(
            self,
            session_id,
            sandbox_id,
            task_id,
            sandbox_provider=None,
            task_sandbox_id=None,
        ):
            trace.append(
                ("persist", sandbox_id, task_id, sandbox_provider)
            )

    class Task:
        async def cancel(self):
            trace.append("cancel")
            return True

        async def wait_for_done(self, timeout_seconds):
            trace.append("wait")
            return True

    class TaskClass:
        @classmethod
        async def get(cls, task_id):
            assert task_id == "old-task"
            return Task()

    class TurnRepository:
        async def cancel_queued(
            self, session_id, *, user_id=None, exclude_submission_id=None
        ):
            trace.append(("cancel-queued", exclude_submission_id))
            return 0

        async def count_running(self, session_id, *, user_id=None):
            trace.append("count-running")
            return 0

    class Handle:
        id = "new-provider"

        async def aclose(self):
            trace.append("close")

    class Provisioner:
        async def ensure_locked(self, current):
            trace.append("ensure")
            current.sandbox_id = "new-provider"
            current.sandbox_provider = "agentbay"
            return Handle()

    service = AgentDomainService(
        agent_repository=SimpleNamespace(),
        session_repository=Repository(),
        sandbox_cls=SimpleNamespace(),
        task_cls=TaskClass,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        turn_submission_repository=TurnRepository(),
        sandbox_provisioner=Provisioner(),
    )

    assert await service._ensure_sandbox_locked(session) is True
    await service._retire_replaced_task_stream(
        session, preserve_submission_id="new-turn"
    )

    assert session.sandbox_id == "new-provider"
    assert session.task_id is None
    assert trace == [
        "ensure",
        "close",
        "cancel",
        "wait",
        ("cancel-queued", "new-turn"),
        "count-running",
        ("persist", "new-provider", None, "agentbay"),
    ]


async def test_replacement_never_clears_task_while_a_turn_is_running():
    session = Session(
        id="session-1",
        user_id="owner",
        agent_id="agent-1",
        sandbox_id="new-provider",
        task_id="old-task",
    )

    class Repository:
        async def update_runtime_ownership(self, *_args):
            raise AssertionError("running ownership must remain recoverable")

    class TurnRepository:
        async def cancel_queued(self, *_args, **_kwargs):
            return 0

        async def count_running(self, *_args, **_kwargs):
            return 1

    class TaskClass:
        @classmethod
        async def get(cls, task_id):
            return None

    service = AgentDomainService(
        agent_repository=SimpleNamespace(),
        session_repository=Repository(),
        sandbox_cls=SimpleNamespace(),
        task_cls=TaskClass,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        turn_submission_repository=TurnRepository(),
        sandbox_provisioner=SimpleNamespace(),
    )

    with pytest.raises(RuntimeError, match="still running"):
        await service._retire_replaced_task_stream(
            session, preserve_submission_id="new-turn"
        )

    assert session.task_id == "old-task"


async def test_stopped_task_reconciles_expired_exact_running_turn():
    trace = []

    class TurnRepository:
        def __init__(self):
            self.running = 1

        async def cancel_queued(self, session_id, *, user_id=None):
            trace.append(("cancel", session_id, user_id))
            return 0

        async def count_running(self, session_id, *, user_id=None):
            trace.append(("count", self.running))
            return self.running

        async def recover_factory_failure_for_task(
            self, session_id, *, user_id, task_id, error
        ):
            trace.append(("recover", session_id, user_id, task_id, error))
            self.running = 0
            return True

        async def list_active(self, session_id, *, user_id=None):
            return []

    class SessionRepository:
        async def update_status(self, session_id, status):
            trace.append(("status", session_id, status))

    turns = TurnRepository()
    service = AgentDomainService(
        agent_repository=SimpleNamespace(),
        session_repository=SessionRepository(),
        sandbox_cls=SimpleNamespace(),
        task_cls=SimpleNamespace(),
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        turn_submission_repository=turns,
    )

    await service._finalize_turns_after_task_stop(
        "session-1",
        user_id="owner",
        task_id="lost-local-task",
    )

    assert trace[:4] == [
        ("cancel", "session-1", "owner"),
        ("count", 1),
        (
            "recover",
            "session-1",
            "owner",
            "lost-local-task",
            "Stopped task did not persist terminal turn state",
        ),
        ("count", 0),
    ]
    assert trace[-1] == (
        "status",
        "session-1",
        SessionStatus.COMPLETED,
    )


async def test_task_without_runtime_is_replaced_after_crash_recovery():
    session = Session(
        id="session-crash-gap",
        user_id="owner",
        agent_id="agent-1",
        sandbox_id=None,
        sandbox_provider="docker",
        task_id="old-task",
    )

    class Handle:
        id = "new-generation"

        async def aclose(self):
            return None

    class Provisioner:
        async def ensure_locked(self, current):
            current.sandbox_id = "new-generation"
            return Handle()

    service = AgentDomainService(
        agent_repository=SimpleNamespace(),
        session_repository=SimpleNamespace(),
        sandbox_cls=SimpleNamespace(),
        task_cls=SimpleNamespace(),
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        sandbox_provisioner=Provisioner(),
    )

    assert await service._ensure_sandbox_locked(session) is True


async def test_task_bound_to_old_generation_is_retired_after_pointer_crash():
    trace = []
    session = Session(
        id="session-pointer-crash",
        user_id="owner",
        agent_id="agent-1",
        sandbox_id="generation-new",
        sandbox_provider="docker",
        task_id="task-old",
        task_sandbox_id="generation-old",
    )

    class Handle:
        id = "generation-new"

        async def aclose(self):
            trace.append("close")

    class Provisioner:
        async def ensure_locked(self, current):
            return Handle()

    class Task:
        async def cancel(self):
            trace.append("cancel-old")

        async def wait_for_done(self, timeout_seconds):
            trace.append("wait-old")
            return True

    class TaskClass:
        @classmethod
        async def get(cls, task_id):
            assert task_id == "task-old"
            return Task()

    class Repository:
        async def update_runtime_ownership(
            self,
            session_id,
            sandbox_id,
            task_id,
            sandbox_provider,
            task_sandbox_id,
        ):
            trace.append(
                ("persist", task_id, task_sandbox_id, sandbox_id)
            )

    service = AgentDomainService(
        agent_repository=SimpleNamespace(),
        session_repository=Repository(),
        sandbox_cls=SimpleNamespace(),
        task_cls=TaskClass,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        sandbox_provisioner=Provisioner(),
    )

    assert await service._ensure_sandbox_locked(session) is True
    await service._retire_replaced_task_stream(
        session, preserve_submission_id=None
    )

    assert session.task_id is None
    assert session.task_sandbox_id is None
    assert trace == [
        "close",
        "cancel-old",
        "wait-old",
        ("persist", None, None, "generation-new"),
    ]


async def test_concurrent_first_messages_share_one_sandbox_and_task():
    class Repository:
        def __init__(self):
            self.stored = Session(
                id="session-1", user_id="owner", agent_id="agent-1"
            )
            self.message_events = []

        async def find_by_id_and_user_id(self, session_id, user_id):
            await asyncio.sleep(0)
            if session_id != self.stored.id or user_id != self.stored.user_id:
                return None
            # Simulate a real database read, not a shared mutable object.
            return self.stored.model_copy(deep=True)

        async def save(self, session):
            await asyncio.sleep(0)
            self.stored = session.model_copy(deep=True)

        async def update_latest_message(self, session_id, message, timestamp):
            await asyncio.sleep(0)

        async def add_event(self, session_id, event):
            self.message_events.append(event)

        async def update_unread_message_count(self, session_id, count):
            return None

        async def update_status(self, session_id, status):
            self.stored.status = status

    class Sandbox:
        def __init__(self, sandbox_id):
            self.id = sandbox_id

        async def destroy(self):
            return True

    class SandboxClass:
        instances = {}
        create_count = 0

        @classmethod
        async def create(cls):
            cls.create_count += 1
            await asyncio.sleep(0)
            sandbox = Sandbox(f"sandbox-{cls.create_count}")
            cls.instances[sandbox.id] = sandbox
            return sandbox

        @classmethod
        async def get(cls, sandbox_id):
            return cls.instances.get(sandbox_id)

    class InputStream:
        def __init__(self):
            self.messages = []
            self.changed = asyncio.Condition()

        async def put(self, value):
            async with self.changed:
                self.messages.append(value)
                event_id = f"{len(self.messages)}-0"
                self.changed.notify_all()
                return event_id

        async def wait_for_count(self, count):
            async with self.changed:
                await self.changed.wait_for(lambda: len(self.messages) >= count)

    class OutputStream:
        def __init__(self):
            self.start_ids = []
            self.events = []
            self.changed = asyncio.Condition()

        @staticmethod
        def _sequence(event_id):
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
            self.start_ids.append(start_id)
            sequence = self._sequence(start_id)
            async with self.changed:
                def next_event():
                    return next(
                        (
                            item
                            for item in self.events
                            if self._sequence(item[0]) > sequence
                        ),
                        None,
                    )

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
            self.run_count = 0
            self.worker_start_count = 0
            self.worker_task = None

        async def is_done(self):
            return self.worker_task is not None and self.worker_task.done()

        async def run(self):
            self.run_count += 1
            if self.worker_task is None:
                self.worker_start_count += 1
                self.worker_task = asyncio.create_task(self._work())

        async def _work(self):
            # Keep the first execution alive until the simultaneous second
            # submission is queued, then emit complete, correlated turns.
            await self.input_stream.wait_for_count(2)
            for event_id, value in zip(
                ("1-0", "2-0"), self.input_stream.messages
            ):
                input_event = TypeAdapter(AgentEvent).validate_json(value)
                for output_event in (
                    TitleEvent(
                        title=f"title:{input_event.message}", turn_id=event_id
                    ),
                    MessageEvent(
                        message=f"answer:{input_event.message}", turn_id=event_id
                    ),
                    DoneEvent(turn_id=event_id),
                ):
                    await self.output_stream.put(output_event.model_dump_json())

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

    repository = Repository()
    service = _service(repository, SandboxClass, TaskClass)

    first, second = await asyncio.gather(
        _collect(service.chat("session-1", "owner", message="first")),
        _collect(service.chat("session-1", "owner", message="second")),
    )

    assert [event.type for event in first] == ["title", "message", "done"]
    assert [event.type for event in second] == ["title", "message", "done"]
    assert [event.turn_id for event in first] == ["1-0"] * 3
    assert [event.turn_id for event in second] == ["2-0"] * 3
    assert [
        event.message for event in first if isinstance(event, MessageEvent)
    ] == ["answer:first"]
    assert [
        event.message for event in second if isinstance(event, MessageEvent)
    ] == ["answer:second"]
    assert SandboxClass.create_count == 1
    assert TaskClass.create_count == 1
    task = TaskClass.registry[repository.stored.task_id]
    assert task.run_count == 2
    assert task.worker_start_count == 1
    assert len(task.input_stream.messages) == 2
    assert len(repository.message_events) == 2
    await task.worker_task


async def test_client_event_uuid_replays_instead_of_skipping_to_stream_tail():
    session = Session(
        id="session-1",
        user_id="owner",
        agent_id="agent-1",
        task_id="task-1",
        status=SessionStatus.RUNNING,
    )

    class Repository:
        async def find_by_id_and_user_id(self, session_id, user_id):
            return session

        async def update_unread_message_count(self, session_id, count):
            return None

        async def add_event(self, session_id, event):
            raise AssertionError("a successful replay must not append an error")

    class OutputStream:
        def __init__(self):
            self.start_ids = []

        async def get(self, start_id=None, block_ms=0):
            self.start_ids.append(start_id)
            return "100-0", DoneEvent().model_dump_json()

    output_stream = OutputStream()
    task = SimpleNamespace(
        output_stream=output_stream,
        is_done=lambda: _async_value(False),
    )

    class TaskClass:
        @classmethod
        async def get(cls, task_id):
            return task

    service = _service(Repository(), SimpleNamespace(), TaskClass)

    events = await _collect(
        service.chat(
            "session-1",
            "owner",
            latest_event_id="43ac1414-99a8-482f-a5c6-1778e6fd2ebb",
        )
    )

    assert [event.type for event in events] == ["done"]
    assert output_stream.start_ids == [None]


async def test_reconnect_reader_does_not_wait_for_message_submission_lock():
    session = Session(
        id="session-1",
        user_id="owner",
        agent_id="agent-1",
        task_id="task-1",
        status=SessionStatus.RUNNING,
    )

    class Repository:
        async def find_by_id_and_user_id(self, session_id, user_id):
            return session

        async def update_unread_message_count(self, session_id, count):
            return None

    class OutputStream:
        async def get(self, start_id=None, block_ms=0):
            return "1-0", DoneEvent().model_dump_json()

    task = SimpleNamespace(
        output_stream=OutputStream(),
        is_done=lambda: _async_value(False),
    )

    class TaskClass:
        @classmethod
        async def get(cls, task_id):
            return task

    service = _service(Repository(), SimpleNamespace(), TaskClass)
    submission_lock = service._get_session_lock(session.id)
    await submission_lock.acquire()
    try:
        events = await asyncio.wait_for(
            _collect(service.chat(session.id, session.user_id)), timeout=0.5
        )
    finally:
        submission_lock.release()

    assert [event.type for event in events] == ["done"]


async def test_message_turn_restarts_once_if_task_drains_before_its_output():
    session = Session(
        id="session-1",
        user_id="owner",
        agent_id="agent-1",
        sandbox_id="sandbox-1",
    )

    class Repository:
        def __init__(self):
            self.events = []

        async def find_by_id_and_user_id(self, session_id, user_id):
            return session

        async def update_latest_message(self, session_id, message, timestamp):
            return None

        async def add_event(self, session_id, event):
            self.events.append(event)

        async def update_unread_message_count(self, session_id, count):
            return None

    class InputStream:
        async def put(self, value):
            return "1-0"

    class OutputStream:
        def __init__(self):
            self.events = []

        async def get_latest_id(self):
            return self.events[-1][0] if self.events else "0"

        async def put(self, event):
            event_id = f"{len(self.events) + 1}-0"
            self.events.append((event_id, event.model_dump_json()))

        async def get(self, start_id=None, block_ms=0):
            sequence = int((start_id or "0").split("-", 1)[0])
            return next(
                (
                    item
                    for item in self.events
                    if int(item[0].split("-", 1)[0]) > sequence
                ),
                (None, None),
            )

    class Task:
        def __init__(self):
            self.id = "task-1"
            self.input_stream = InputStream()
            self.output_stream = OutputStream()
            self.run_count = 0
            self.done = False

        async def is_done(self):
            return self.done

        async def run(self):
            self.run_count += 1
            self.done = True
            if self.run_count == 2:
                await self.output_stream.put(
                    MessageEvent(message="answer:late", turn_id="1-0")
                )
                await self.output_stream.put(DoneEvent(turn_id="1-0"))

    repository = Repository()
    task = Task()
    service = _service(repository, SimpleNamespace(), SimpleNamespace())

    class ExistingSandboxProvisioner:
        async def ensure_locked(self, current_session):
            return SimpleNamespace(id=current_session.sandbox_id)

    service._sandbox_provisioner = ExistingSandboxProvisioner()

    async def create_task(created_session, *, sandbox_ready=False):
        created_session.task_id = task.id
        return task

    service._create_task = create_task

    events = await _collect(
        service.chat(session.id, session.user_id, message="late")
    )

    assert [event.type for event in events] == ["message", "done"]
    assert [event.turn_id for event in events] == ["1-0", "1-0"]
    assert task.run_count == 2


async def _async_value(value):
    return value
