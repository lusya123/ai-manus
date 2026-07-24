"""Fault-injection checks for Mongo acceptance -> Redis dispatch handoff."""

from types import SimpleNamespace

import pytest

from app.domain.models.session import Session, SessionStatus
from app.domain.models.turn_submission import (
    TurnSubmission,
    TurnSubmissionState,
    TurnSubmissionUnavailableError,
)
from app.domain.models.event import MessageEvent
from app.domain.services.agent_domain_service import AgentDomainService
from app.domain.services.agent_task_runner import RunnerCleanupCapacityError
from app.infrastructure.external.sandbox.passthrough_provisioner import (
    PassthroughSandboxProvisioner,
)


SUBMISSION_ID = "22222222-2222-4222-8222-222222222222"


class TurnRepository:
    def __init__(self, *, cas_response_lost=False):
        self.turn = None
        self.cas_response_lost = cas_response_lost
        self.cas_calls = 0
        self.terminal_states = []

    async def accept(self, candidate):
        if self.turn is None:
            self.turn = candidate.model_copy(deep=True)
            return self.turn, True
        return self.turn, False

    async def find(self, session_id, submission_id):
        if (
            self.turn is not None
            and self.turn.session_id == session_id
            and self.turn.submission_id == submission_id
        ):
            return self.turn
        return None

    async def list_active(self, session_id, *, user_id=None):
        if (
            self.turn is not None
            and self.turn.session_id == session_id
            and (user_id is None or self.turn.user_id == user_id)
            and self.turn.state
            in {
                TurnSubmissionState.PENDING,
                TurnSubmissionState.ENQUEUED,
                TurnSubmissionState.RUNNING,
            }
        ):
            return [self.turn]
        return []

    async def mark_enqueued(
        self, session_id, submission_id, *, task_id, stream_id
    ):
        self.cas_calls += 1
        self.turn.state = TurnSubmissionState.ENQUEUED
        self.turn.task_id = task_id
        self.turn.stream_id = stream_id
        if self.cas_response_lost and self.cas_calls == 1:
            raise ConnectionError("mongo response lost")
        return self.turn

    async def mark_unclaimed_terminal(
        self, session_id, submission_id, *, state, error
    ):
        if self.turn.state in {
            TurnSubmissionState.PENDING,
            TurnSubmissionState.ENQUEUED,
        }:
            self.turn.state = state
            self.turn.terminal_error = error
            self.terminal_states.append(state)
            return True
        return False


class SessionRepository:
    def __init__(
        self,
        *,
        task_id="task-1",
        status: SessionStatus = SessionStatus.PENDING,
    ):
        self.session = Session(
            id="session-1",
            user_id="user-1",
            agent_id="agent-1",
            sandbox_id="sandbox-1",
            sandbox_provider="docker",
            task_id=task_id,
            task_sandbox_id="sandbox-1" if task_id is not None else None,
            status=status,
        )
        self.events = []
        self.statuses = []

    async def find_by_id_and_user_id(self, session_id, user_id):
        return self.session

    async def add_event_once(self, session_id, event):
        if all(existing.id != event.id for existing in self.events):
            self.events.append(event)
        return event

    async def update_latest_message(self, session_id, message, timestamp):
        return None

    async def update_status(self, session_id, status):
        self.statuses.append(status)

    async def update_runtime_ownership(
        self,
        session_id,
        sandbox_id,
        task_id,
        sandbox_provider=None,
        task_sandbox_id=None,
    ):
        self.session.sandbox_id = sandbox_id
        self.session.sandbox_provider = sandbox_provider
        self.session.task_id = task_id
        self.session.task_sandbox_id = task_sandbox_id


class InputStream:
    def __init__(self, *, fail_after_put_once=False):
        self.fail_after_put_once = fail_after_put_once
        self.values = []

    async def put(self, value):
        self.values.append(value)
        if self.fail_after_put_once and len(self.values) == 1:
            raise ConnectionError("redis response lost")
        return f"{len(self.values)}-0"


class FakeTask:
    def __init__(
        self,
        *,
        fail_run_once=False,
        fail_after_put_once=False,
        run_error_once=None,
    ):
        self.id = "task-1"
        self.input_stream = InputStream(
            fail_after_put_once=fail_after_put_once
        )
        self.output_stream = SimpleNamespace()
        self.fail_run_once = fail_run_once
        self.run_error_once = run_error_once
        self.run_calls = 0

    async def run(self):
        self.run_calls += 1
        if self.run_error_once is not None and self.run_calls == 1:
            raise self.run_error_once
        if self.fail_run_once and self.run_calls == 1:
            raise ConnectionError("dispatch response lost")


def task_class(task, *, create_error=None):
    class TaskClass:
        @classmethod
        async def get(cls, task_id):
            return task if task is not None and task.id == task_id else None

        @classmethod
        def recover(cls, task_id, params):
            return task

        @classmethod
        def create(cls, params):
            if create_error is not None:
                raise create_error
            return task

    return TaskClass


class SandboxHandle:
    id = "sandbox-1"

    async def aclose(self):
        return None


class SandboxClass:
    @classmethod
    async def get(cls, sandbox_id):
        return SandboxHandle()

    @classmethod
    async def create(cls):
        return SandboxHandle()


def service(session_repository, turn_repository, task_cls):
    return AgentDomainService(
        agent_repository=SimpleNamespace(),
        session_repository=session_repository,
        sandbox_cls=SandboxClass,
        task_cls=task_cls,
        file_storage=SimpleNamespace(),
        mcp_repository=SimpleNamespace(),
        turn_submission_repository=turn_repository,
        sandbox_provisioner=PassthroughSandboxProvisioner(
            SandboxClass, session_repository
        ),
    )


async def accept(domain_service):
    return await domain_service.accept_chat_submission(
        session_id="session-1",
        user_id="user-1",
        submission_id=SUBMISSION_ID,
        message="hello",
    )


@pytest.mark.asyncio
async def test_reconnect_resumes_pending_turn_from_persisted_input():
    repository = TurnRepository()
    session_repository = SessionRepository()
    task = FakeTask()
    input_event = MessageEvent(
        id=SUBMISSION_ID,
        turn_id=SUBMISSION_ID,
        role="user",
        message="hello",
    )
    repository.turn = TurnSubmission(
        session_id="session-1",
        submission_id=SUBMISSION_ID,
        user_id="user-1",
        agent_id="agent-1",
        request_hash="persisted-hash",
        input_json=input_event.model_dump_json(),
    )
    domain_service = service(
        session_repository,
        repository,
        task_class(task),
    )

    resumed, dispatch_confirmed = await domain_service._resume_pending_durable_turn(
        session_id="session-1",
        user_id="user-1",
        submission_id=SUBMISSION_ID,
    )

    assert dispatch_confirmed is True
    assert resumed.state == TurnSubmissionState.ENQUEUED
    assert resumed.task_id == task.id
    assert task.input_stream.values == [repository.turn.input_json]
    assert task.run_calls == 1


@pytest.mark.asyncio
async def test_accept_captures_waiting_resume_once_and_retry_preserves_it():
    repository = TurnRepository()
    session_repository = SessionRepository(status=SessionStatus.WAITING)
    task = FakeTask()
    domain_service = service(
        session_repository,
        repository,
        task_class(task),
    )

    accepted = await accept(domain_service)

    assert accepted.resumes_waiting is True
    assert repository.turn.resumes_waiting is True

    # Durable status projection and/or another replica may observe RUNNING by
    # the time the client retries the same logical request. The retry must use
    # the first persisted acceptance decision, not recompute resume intent.
    session_repository.session.status = SessionStatus.RUNNING
    retried = await accept(domain_service)

    assert retried.submission_id == accepted.submission_id
    assert retried.request_hash == accepted.request_hash
    assert retried.resumes_waiting is True


@pytest.mark.asyncio
async def test_xadd_response_loss_retries_same_logical_payload():
    repository = TurnRepository()
    task = FakeTask(fail_after_put_once=True)
    domain_service = service(
        SessionRepository(), repository, task_class(task)
    )

    with pytest.raises(TurnSubmissionUnavailableError):
        await accept(domain_service)
    assert repository.turn.state == TurnSubmissionState.PENDING

    accepted = await accept(domain_service)
    assert accepted.state == TurnSubmissionState.ENQUEUED
    assert task.input_stream.values[0] == task.input_stream.values[1]
    assert task.run_calls == 1


@pytest.mark.asyncio
async def test_mongo_cas_response_loss_does_not_xadd_again():
    repository = TurnRepository(cas_response_lost=True)
    task = FakeTask()
    domain_service = service(
        SessionRepository(), repository, task_class(task)
    )

    with pytest.raises(TurnSubmissionUnavailableError):
        await accept(domain_service)
    assert repository.turn.state == TurnSubmissionState.ENQUEUED

    accepted = await accept(domain_service)
    assert accepted.state == TurnSubmissionState.ENQUEUED
    assert len(task.input_stream.values) == 1
    assert task.run_calls == 1


@pytest.mark.asyncio
async def test_ambiguous_task_dispatch_terminalizes_failed_unknown():
    repository = TurnRepository()
    task = FakeTask(fail_run_once=True)
    domain_service = service(
        SessionRepository(), repository, task_class(task)
    )

    with pytest.raises(TurnSubmissionUnavailableError):
        await accept(domain_service)

    assert repository.turn.state == TurnSubmissionState.FAILED_UNKNOWN
    assert repository.terminal_states == [TurnSubmissionState.FAILED_UNKNOWN]
    terminal = await accept(domain_service)
    assert terminal.state == TurnSubmissionState.FAILED_UNKNOWN
    assert task.run_calls == 1


@pytest.mark.asyncio
async def test_local_cleanup_backpressure_keeps_enqueued_turn_retryable():
    repository = TurnRepository()
    task = FakeTask(
        run_error_once=RunnerCleanupCapacityError(
            "cleanup bundles are full"
        )
    )
    domain_service = service(
        SessionRepository(), repository, task_class(task)
    )

    with pytest.raises(TurnSubmissionUnavailableError):
        await accept(domain_service)

    assert repository.turn.state == TurnSubmissionState.ENQUEUED
    assert repository.terminal_states == []
    assert len(task.input_stream.values) == 1
    assert task.run_calls == 1

    retried = await accept(domain_service)

    assert retried.state == TurnSubmissionState.ENQUEUED
    assert repository.terminal_states == []
    assert len(task.input_stream.values) == 1
    assert task.run_calls == 2


@pytest.mark.asyncio
async def test_known_pre_execution_task_creation_failure_is_failed():
    repository = TurnRepository()
    session_repository = SessionRepository(task_id=None)
    domain_service = service(
        session_repository,
        repository,
        task_class(None, create_error=RuntimeError("create failed")),
    )

    with pytest.raises(TurnSubmissionUnavailableError):
        await accept(domain_service)

    assert repository.turn.state == TurnSubmissionState.FAILED
    assert repository.terminal_states == [TurnSubmissionState.FAILED]
