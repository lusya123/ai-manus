import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from weakref import WeakValueDictionary

import pytest

from app.domain.models.event import DoneEvent
from app.domain.models.session import Session
from app.domain.models.turn_submission import (
    TurnSubmission,
    TurnSubmissionState,
    TurnSubmissionUnavailableError,
)
from app.domain.services.agent_domain_service import AgentDomainService


TURN_ID = "99999999-9999-4999-8999-999999999999"


class TurnRepository:
    def __init__(self):
        self.turn = TurnSubmission(
            session_id="session-1",
            submission_id=TURN_ID,
            user_id="user-1",
            agent_id="agent-1",
            task_id="task-1",
            stream_id="1-0",
            request_hash="hash",
            input_json="{}",
            state=TurnSubmissionState.ENQUEUED,
        )
        self.outputs = []

    async def list_outputs(self, session_id, submission_id):
        return list(self.outputs)

    async def find(self, session_id, submission_id):
        return self.turn

    async def append_output(self, session_id, submission_id, event):
        self.outputs.append(event)
        return event


class SessionRepository:
    def __init__(self):
        self.session = Session(
            id="session-1",
            user_id="user-1",
            agent_id="agent-1",
            sandbox_id="dev-sandbox",
            sandbox_provider="docker",
            task_id="task-1",
            task_sandbox_id="dev-sandbox",
        )

    async def find_by_id_and_user_id(self, session_id, user_id):
        return self.session

    async def add_event_once(self, session_id, event):
        return event

    async def update_unread_message_count(self, session_id, count):
        return None


@pytest.mark.asyncio
async def test_reconnect_kicks_an_enqueued_turn_after_producer_crash():
    turns = TurnRepository()

    class Task:
        id = "task-1"

        def __init__(self):
            self.run_calls = 0

        async def run(self):
            self.run_calls += 1
            turns.turn.state = TurnSubmissionState.COMPLETED
            turns.turn.terminal_event_id = f"{TURN_ID}:done"

    task = Task()

    class TaskClass:
        @classmethod
        async def get(cls, task_id):
            return task

    service = object.__new__(AgentDomainService)
    service._turn_submission_repository = turns
    service._session_repository = SessionRepository()
    service._task_cls = TaskClass
    service._session_lifecycle_lease = None

    events = []
    async for event in service._stream_durable_turn(
        session_id="session-1",
        user_id="user-1",
        turn=turns.turn,
    ):
        events.append(event)

    assert task.run_calls == 1
    assert [event.type for event in events] == ["accepted", "done"]
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_reconnect_recovers_expired_running_turn_after_local_restart():
    turns = TurnRepository()
    turns.turn.state = TurnSubmissionState.RUNNING
    turns.turn.claim_owner = "dead-api-process"
    turns.turn.claim_until = datetime.now(UTC) - timedelta(seconds=1)

    class Task:
        id = "task-1"

        def __init__(self):
            self.run_calls = 0

        async def run(self):
            self.run_calls += 1
            # The real AgentTaskRunner reaches this state through
            # claim_for_execution; an expired RUNNING claim is fenced as
            # failed_unknown and its Redis input is acknowledged.
            turns.turn.state = TurnSubmissionState.FAILED_UNKNOWN
            turns.turn.terminal_error = "expired execution ownership"

    task = Task()

    class TaskClass:
        get_calls = 0
        recover_calls = 0

        @classmethod
        async def get(cls, task_id):
            cls.get_calls += 1
            return None

        @classmethod
        def recover(cls, task_id, params):
            cls.recover_calls += 1
            return task

    service = object.__new__(AgentDomainService)
    service._turn_submission_repository = turns
    service._session_repository = SessionRepository()
    service._task_cls = TaskClass
    service._session_lifecycle_lease = None

    events = []
    async for event in service._stream_durable_turn(
        session_id="session-1",
        user_id="user-1",
        turn=turns.turn,
    ):
        events.append(event)

    assert TaskClass.get_calls == 1
    assert TaskClass.recover_calls == 1
    assert task.run_calls == 1
    assert turns.turn.state == TurnSubmissionState.FAILED_UNKNOWN
    assert [event.type for event in events] == ["accepted", "error"]


@pytest.mark.asyncio
async def test_stale_reconnect_cannot_restore_replaced_task_ownership():
    turns = TurnRepository()
    sessions = SessionRepository()
    sessions.session.task_id = "replacement-task"
    ownership_updates = []

    async def update_runtime_ownership(*args, **kwargs):
        ownership_updates.append((args, kwargs))

    sessions.update_runtime_ownership = update_runtime_ownership

    class TaskClass:
        get_calls = 0
        recover_calls = 0

        @classmethod
        async def get(cls, task_id):
            cls.get_calls += 1
            return None

        @classmethod
        def recover(cls, task_id, params):
            cls.recover_calls += 1
            return SimpleNamespace(id=task_id)

    service = object.__new__(AgentDomainService)
    service._session_repository = sessions
    service._task_cls = TaskClass

    with pytest.raises(TurnSubmissionUnavailableError, match="no longer owns"):
        await service._recover_task(sessions.session, "task-1")

    assert sessions.session.task_id == "replacement-task"
    assert TaskClass.get_calls == 0
    assert TaskClass.recover_calls == 0
    assert ownership_updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deleting", "destroying", "task_sandbox_id"),
    [
        (True, False, "dev-sandbox"),
        (False, True, "dev-sandbox"),
        (False, False, "previous-sandbox"),
        (False, False, None),
    ],
)
async def test_task_recovery_fails_closed_during_cleanup_or_generation_mismatch(
    deleting,
    destroying,
    task_sandbox_id,
):
    sessions = SessionRepository()
    sessions.session.deleting = deleting
    sessions.session.sandbox_destroying = destroying
    sessions.session.task_sandbox_id = task_sandbox_id

    class TaskClass:
        get_calls = 0
        recover_calls = 0

        @classmethod
        async def get(cls, task_id):
            cls.get_calls += 1
            return SimpleNamespace(id=task_id)

        @classmethod
        def recover(cls, task_id, params):
            cls.recover_calls += 1
            return SimpleNamespace(id=task_id)

    service = object.__new__(AgentDomainService)
    service._session_repository = sessions
    service._task_cls = TaskClass

    with pytest.raises(TurnSubmissionUnavailableError):
        await service._recover_task(sessions.session, "task-1")

    assert TaskClass.get_calls == 0
    assert TaskClass.recover_calls == 0


@pytest.mark.asyncio
async def test_reconnect_dispatch_rechecks_delete_claim_inside_lifecycle_lease():
    turns = TurnRepository()
    sessions = SessionRepository()

    class Task:
        id = "task-1"

        def __init__(self):
            self.run_calls = 0

        async def run(self):
            self.run_calls += 1

    task = Task()

    class TaskClass:
        @classmethod
        async def get(cls, task_id):
            return task

    class DeleteWinsLease:
        async def run_exclusive(self, session_id, operation):
            sessions.session.deleting = True
            return await operation()

    service = object.__new__(AgentDomainService)
    service._turn_submission_repository = turns
    service._session_repository = sessions
    service._task_cls = TaskClass
    service._session_lifecycle_lease = DeleteWinsLease()

    async def collect_events():
        return [
            event
            async for event in service._stream_durable_turn(
                session_id="session-1",
                user_id="user-1",
                turn=turns.turn,
            )
        ]

    events = await asyncio.wait_for(collect_events(), timeout=2)

    assert [event.type for event in events] == ["accepted"]
    assert task.run_calls == 0


@pytest.mark.asyncio
async def test_pending_snapshot_does_not_hide_concurrent_enqueued_dispatch_gap():
    """A stale PENDING read must still kick an ENQUEUED producer-crash gap."""
    turns = TurnRepository()
    turns.turn.state = TurnSubmissionState.PENDING
    turns.turn.task_id = None
    turns.turn.stream_id = None
    original_find = turns.find
    first_find = True

    async def race_find(session_id, submission_id):
        nonlocal first_find
        current = await original_find(session_id, submission_id)
        if first_find:
            first_find = False
            stale_pending = current.model_copy(deep=True)
            # Simulate another API replica committing XADD + ENQUEUED, then
            # dying before task.run(), after this stream read its old snapshot
            # but before it acquired the lifecycle lease.
            current.state = TurnSubmissionState.ENQUEUED
            current.task_id = "task-1"
            current.stream_id = "1-0"
            return stale_pending
        return current

    turns.find = race_find

    class Task:
        id = "task-1"

        def __init__(self):
            self.run_calls = 0

        async def run(self):
            self.run_calls += 1
            turns.turn.state = TurnSubmissionState.COMPLETED
            turns.turn.terminal_event_id = f"{TURN_ID}:done"

    task = Task()

    class TaskClass:
        @classmethod
        async def get(cls, task_id):
            return task

    service = object.__new__(AgentDomainService)
    service._turn_submission_repository = turns
    service._session_repository = SessionRepository()
    service._task_cls = TaskClass
    service._session_lifecycle_lease = None
    service._session_locks = WeakValueDictionary()

    async def collect_events():
        return [
            event
            async for event in service._stream_durable_turn(
                session_id="session-1",
                user_id="user-1",
                turn=turns.turn,
            )
        ]

    events = await asyncio.wait_for(collect_events(), timeout=2)

    assert task.run_calls == 1
    assert [event.type for event in events] == ["accepted", "done"]
