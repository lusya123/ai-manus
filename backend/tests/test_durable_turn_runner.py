"""Unit checks for the durable turn worker's crash/duplicate boundaries."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domain.models.event import DoneEvent, MessageEvent
from app.domain.models.turn_submission import (
    TERMINAL_TURN_STATES,
    TurnClaimDecision,
    TurnClaimResult,
    TurnSubmission,
    TurnSubmissionState,
)
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.infrastructure.models.documents import TurnSubmissionDocument
from app.infrastructure.repositories.mongo_turn_submission_repository import (
    MongoTurnSubmissionRepository,
)


SESSION_ID = "session-1"
USER_ID = "user-1"
AGENT_ID = "agent-1"
TASK_ID = "task-1"
SANDBOX_ID = "sandbox-1"
TURN_ID = "11111111-1111-4111-8111-111111111111"


class FakeTurnRepository:
    def __init__(
        self,
        state=TurnSubmissionState.ENQUEUED,
        *,
        resumes_waiting: bool = False,
    ):
        self.turn = TurnSubmission(
            session_id=SESSION_ID,
            submission_id=TURN_ID,
            user_id=USER_ID,
            agent_id=AGENT_ID,
            task_id=TASK_ID,
            request_hash="hash",
            input_json="{}",
            resumes_waiting=resumes_waiting,
            state=state,
            stream_id="1-0",
        )
        self.claim_error = None
        self.outbox = []
        self.terminal_calls = []

    async def find(self, session_id, submission_id):
        if session_id == SESSION_ID and submission_id == TURN_ID:
            return self.turn
        return None

    async def list_active(self, session_id, *, user_id=None):
        if (
            session_id == SESSION_ID
            and (user_id is None or user_id == USER_ID)
            and self.turn.state not in TERMINAL_TURN_STATES
        ):
            return [self.turn]
        return []

    async def claim_for_execution(
        self, session_id, submission_id, *, task_id, owner, claim_until
    ):
        if self.claim_error is not None:
            raise self.claim_error
        if self.turn.state == TurnSubmissionState.ENQUEUED:
            self.turn.state = TurnSubmissionState.RUNNING
            self.turn.claim_owner = owner
            self.turn.claim_until = claim_until
            self.turn.attempt += 1
            return TurnClaimResult(
                decision=TurnClaimDecision.EXECUTE, turn=self.turn
            )
        if self.turn.state in TERMINAL_TURN_STATES:
            return TurnClaimResult(
                decision=TurnClaimDecision.ACK, turn=self.turn
            )
        return TurnClaimResult(
            decision=TurnClaimDecision.RETRY, turn=self.turn
        )

    async def renew_claim(
        self, session_id, submission_id, *, owner, claim_until
    ):
        return (
            self.turn.state == TurnSubmissionState.RUNNING
            and self.turn.claim_owner == owner
        )

    async def mark_terminal(
        self,
        session_id,
        submission_id,
        *,
        owner,
        state,
        terminal_event_id=None,
        error=None,
    ):
        if self.turn.state in TERMINAL_TURN_STATES:
            return True
        if (
            self.turn.state != TurnSubmissionState.RUNNING
            or self.turn.claim_owner != owner
        ):
            return False
        self.turn.state = state
        self.turn.terminal_event_id = terminal_event_id
        self.turn.terminal_error = error
        self.terminal_calls.append(state)
        return True

    async def mark_unclaimed_terminal(
        self, session_id, submission_id, *, state, error
    ):
        if self.turn.state in {
            TurnSubmissionState.PENDING,
            TurnSubmissionState.ENQUEUED,
        }:
            self.turn.state = state
            self.turn.terminal_error = error
            self.terminal_calls.append(state)
            return True
        return self.turn.state in TERMINAL_TURN_STATES

    async def cancel_queued(self, session_id, *, user_id=None):
        if self.turn.state in {
            TurnSubmissionState.PENDING,
            TurnSubmissionState.ENQUEUED,
        }:
            self.turn.state = TurnSubmissionState.CANCELLED
            return 1
        return 0

    async def append_output(self, session_id, submission_id, event):
        if all(existing.id != event.id for existing in self.outbox):
            self.outbox.append(event.model_copy(deep=True))
        return next(existing for existing in self.outbox if existing.id == event.id)

    async def update_output_transport_cursor(
        self, session_id, submission_id, event_id, transport_id
    ):
        for event in self.outbox:
            if event.id == event_id:
                event.transport_id = transport_id


class FakeSessionRepository:
    def __init__(self):
        self.events = []
        self.statuses = []

    async def find_by_id_and_user_id(self, session_id, user_id):
        return SimpleNamespace(
            agent_id=AGENT_ID,
            task_id=TASK_ID,
            deleting=False,
            sandbox_destroying=False,
            sandbox_id=SANDBOX_ID,
            task_sandbox_id=SANDBOX_ID,
        )

    async def add_event_once(self, session_id, event):
        if all(existing.id != event.id for existing in self.events):
            self.events.append(event.model_copy(deep=True))
        return next(existing for existing in self.events if existing.id == event.id)

    async def update_event_transport_cursor(
        self, session_id, event_id, transport_id
    ):
        return None

    async def update_title(self, session_id, title):
        return None

    async def update_latest_message(self, session_id, message, timestamp):
        return None

    async def increment_unread_message_count(self, session_id):
        return None

    async def update_status(self, session_id, status):
        self.statuses.append(status)


class FakeInputStream:
    def __init__(self, *, fail_ack_once=False):
        self.fail_ack_once = fail_ack_once
        self.ack_attempts = 0
        self.acked = []
        self.quarantined = []

    async def ack(self, group, transport_id):
        self.ack_attempts += 1
        if self.fail_ack_once and self.ack_attempts == 1:
            raise ConnectionError("redis unavailable")
        self.acked.append((group, transport_id))
        return True

    async def quarantine(
        self, group, transport_id, *, reason, payload_digest
    ):
        self.quarantined.append(
            (group, transport_id, reason, payload_digest)
        )
        return True


class FakeOutputStream:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.items = []

    async def put(self, value):
        if self.fail:
            raise ConnectionError("redis unavailable")
        transport_id = f"{len(self.items) + 1}-0"
        self.items.append((transport_id, value))
        return transport_id


class FakeSandbox:
    def __init__(self):
        self.ensure_calls = 0

    async def ensure_sandbox(self):
        self.ensure_calls += 1


class FakeMCPTool:
    async def initialized(self, config):
        return None


class FakeMCPRepository:
    async def get_mcp_config(self):
        return {}


def _input_json():
    return MessageEvent(
        id=TURN_ID,
        turn_id=TURN_ID,
        role="user",
        message="hello",
    ).model_dump_json()


def _build_runner(
    turn_repository,
    *,
    input_stream=None,
    output_stream=None,
    run_flow=None,
):
    runner = object.__new__(AgentTaskRunner)
    runner._session_id = SESSION_ID
    runner._agent_id = AGENT_ID
    runner._user_id = USER_ID
    runner._sandbox_id = SANDBOX_ID
    runner._worker_id = "worker-1"
    runner._claim_seconds = 60
    runner._claim_renew_seconds = 3_600
    runner._turn_submission_repository = turn_repository
    runner._session_repository = FakeSessionRepository()
    runner._sandbox = FakeSandbox()
    runner._mcp_tool = FakeMCPTool()
    runner._mcp_repository = FakeMCPRepository()

    async def sync_attachments(event):
        event.attachments = []

    runner._sync_message_attachments_to_sandbox = sync_attachments
    flow_calls = {"count": 0, "resumes_waiting": []}

    async def default_flow(message, resumes_waiting=None):
        flow_calls["count"] += 1
        flow_calls["resumes_waiting"].append(resumes_waiting)
        yield MessageEvent(role="assistant", message="answer")
        yield DoneEvent()

    runner._run_flow = run_flow or default_flow
    task = SimpleNamespace(
        id=TASK_ID,
        input_stream=input_stream or FakeInputStream(),
        output_stream=output_stream or FakeOutputStream(),
    )
    return runner, task, flow_calls


def test_turn_submission_mapping_and_retry_keep_waiting_resume_decision():
    original = FakeTurnRepository(resumes_waiting=True).turn

    # ``model_construct`` exercises the document/domain field mapping without
    # requiring a live Beanie collection for this unit regression.
    restored = TurnSubmissionDocument.model_construct(
        **original.model_dump()
    ).to_domain()

    assert restored.resumes_waiting is True
    retry_candidate = restored.model_copy(update={"resumes_waiting": False})
    validated = MongoTurnSubmissionRepository._validate_existing(
        restored,
        retry_candidate,
    )
    assert validated.resumes_waiting is True

    # Rolling upgrades must read pre-field documents as ordinary new turns.
    legacy_payload = original.model_dump(exclude={"resumes_waiting"})
    legacy = TurnSubmissionDocument.model_construct(**legacy_payload).to_domain()
    assert legacy.resumes_waiting is False


@pytest.mark.asyncio
async def test_mongo_claim_response_loss_reconciles_exact_owner(monkeypatch):
    owner = "worker-1:1-0"
    running = FakeTurnRepository().turn
    running.state = TurnSubmissionState.RUNNING
    running.claim_owner = owner
    running.claim_until = datetime.now(UTC) + timedelta(minutes=1)
    running.attempt = 1

    class ResponseLossCollection:
        async def find_one_and_update(self, *args, **kwargs):
            raise ConnectionError("claim response lost after commit")

        async def find_one(self, query):
            return running.model_dump()

    repository = MongoTurnSubmissionRepository(
        max_active_per_session=1,
        max_active_per_user=1,
        max_payload_bytes=1024,
        terminal_retention_days=1,
    )
    collection = ResponseLossCollection()
    monkeypatch.setattr(repository, "_turn_collection", lambda: collection)

    claim = await repository.claim_for_execution(
        SESSION_ID,
        TURN_ID,
        task_id=TASK_ID,
        owner=owner,
        claim_until=running.claim_until,
    )

    assert claim.decision == TurnClaimDecision.EXECUTE
    assert claim.turn.state == TurnSubmissionState.RUNNING
    assert claim.turn.claim_owner == owner


@pytest.mark.asyncio
async def test_mongo_terminal_response_loss_confirms_terminal_postcondition(
    monkeypatch,
):
    terminal = FakeTurnRepository().turn
    terminal.state = TurnSubmissionState.CANCELLED
    terminal.claim_owner = None
    terminal.claim_until = None
    terminal.terminal_error = "Execution was cancelled"
    terminal.expires_at = datetime.now(UTC) + timedelta(days=1)

    class ResponseLossCollection:
        async def find_one_and_update(self, *args, **kwargs):
            raise ConnectionError("terminal response lost after commit")

        async def find_one(self, query):
            return terminal.model_dump()

    repository = MongoTurnSubmissionRepository(
        max_active_per_session=1,
        max_active_per_user=1,
        max_payload_bytes=1024,
        terminal_retention_days=1,
    )
    collection = ResponseLossCollection()
    repaired = []

    async def repair(turn):
        repaired.append(turn.state)

    monkeypatch.setattr(repository, "_turn_collection", lambda: collection)
    monkeypatch.setattr(repository, "_repair_terminal_postconditions", repair)

    assert await repository.mark_terminal(
        SESSION_ID,
        TURN_ID,
        owner="worker-1:1-0",
        state=TurnSubmissionState.CANCELLED,
        error="Execution was cancelled",
    )
    assert repaired == [TurnSubmissionState.CANCELLED]


@pytest.mark.asyncio
async def test_duplicate_transport_entries_execute_one_logical_turn_once():
    repository = FakeTurnRepository()
    runner, task, flow_calls = _build_runner(repository)

    assert await runner._process_durable_entry(task, "1-0", _input_json())
    assert await runner._process_durable_entry(task, "2-0", _input_json())

    assert flow_calls["count"] == 1
    assert repository.turn.attempt == 1
    assert repository.turn.state == TurnSubmissionState.COMPLETED
    assert [transport_id for _, transport_id in task.input_stream.acked] == [
        "1-0",
        "2-0",
    ]


@pytest.mark.asyncio
async def test_runner_keeps_waiting_resume_after_running_status_projection():
    repository = FakeTurnRepository(resumes_waiting=True)
    runner, task, flow_calls = _build_runner(repository)

    assert await runner._process_durable_entry(task, "1-0", _input_json())

    # Claiming projects the aggregate Session status to RUNNING before the
    # flow starts. The acceptance-time flag must nevertheless reach the flow.
    assert runner._session_repository.statuses[0] == "running"
    assert flow_calls["resumes_waiting"] == [True]


@pytest.mark.asyncio
async def test_terminal_commit_then_ack_loss_retries_ack_without_error_output():
    repository = FakeTurnRepository()
    input_stream = FakeInputStream(fail_ack_once=True)
    runner, task, flow_calls = _build_runner(
        repository, input_stream=input_stream
    )

    assert not await runner._process_durable_entry(
        task, "1-0", _input_json()
    )
    assert repository.turn.state == TurnSubmissionState.COMPLETED
    assert [event.type for event in repository.outbox] == ["message", "done"]

    assert await runner._process_durable_entry(task, "1-0", _input_json())
    assert flow_calls["count"] == 1
    assert [event.type for event in repository.outbox] == ["message", "done"]
    assert input_stream.ack_attempts == 2


@pytest.mark.asyncio
async def test_claim_failure_has_no_sandbox_flow_output_or_ack_side_effects():
    repository = FakeTurnRepository()
    repository.claim_error = ConnectionError("mongo unavailable")
    runner, task, flow_calls = _build_runner(repository)

    with pytest.raises(ConnectionError):
        await runner._process_durable_entry(task, "1-0", _input_json())

    assert runner._sandbox.ensure_calls == 0
    assert flow_calls["count"] == 0
    assert repository.outbox == []
    assert task.input_stream.acked == []


@pytest.mark.asyncio
async def test_redis_output_failure_keeps_mongo_outbox_and_terminal_state():
    repository = FakeTurnRepository()
    runner, task, flow_calls = _build_runner(
        repository, output_stream=FakeOutputStream(fail=True)
    )

    assert await runner._process_durable_entry(task, "1-0", _input_json())

    assert flow_calls["count"] == 1
    assert repository.turn.state == TurnSubmissionState.COMPLETED
    assert [event.type for event in repository.outbox] == ["message", "done"]
    assert task.input_stream.acked


@pytest.mark.asyncio
async def test_cancelled_execution_persists_cancelled_before_ack():
    repository = FakeTurnRepository()
    flow_started = asyncio.Event()
    wait_forever = asyncio.Event()

    async def blocking_flow(message, resumes_waiting=None):
        flow_started.set()
        await wait_forever.wait()
        yield DoneEvent()

    runner, task, _ = _build_runner(repository, run_flow=blocking_flow)
    execution = asyncio.create_task(
        runner._process_durable_entry(task, "1-0", _input_json())
    )
    await asyncio.wait_for(flow_started.wait(), timeout=1)
    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution
    assert repository.turn.state == TurnSubmissionState.CANCELLED
    assert repository.terminal_calls[-1] == TurnSubmissionState.CANCELLED
    assert task.input_stream.acked


@pytest.mark.asyncio
async def test_cancel_event_failure_cannot_block_terminal_cas_and_ack():
    class TransientTerminalRepository(FakeTurnRepository):
        def __init__(self):
            super().__init__()
            self.mark_calls = 0

        async def mark_terminal(self, *args, **kwargs):
            self.mark_calls += 1
            if self.mark_calls == 1:
                raise ConnectionError("terminal CAS temporarily unavailable")
            return await super().mark_terminal(*args, **kwargs)

    class FailingEventSessionRepository(FakeSessionRepository):
        async def add_event_once(self, session_id, event):
            raise ConnectionError("session event projection unavailable")

    repository = TransientTerminalRepository()
    flow_started = asyncio.Event()

    async def blocking_flow(message, resumes_waiting=None):
        flow_started.set()
        await asyncio.Event().wait()
        yield DoneEvent()

    runner, task, _ = _build_runner(repository, run_flow=blocking_flow)
    runner._session_repository = FailingEventSessionRepository()
    execution = asyncio.create_task(
        runner._process_durable_entry(task, "1-0", _input_json())
    )
    await asyncio.wait_for(flow_started.wait(), timeout=1)
    execution.cancel("cancel_requested")

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=1)

    assert repository.mark_calls == 2
    assert repository.turn.state == TurnSubmissionState.CANCELLED
    assert repository.turn.terminal_event_id is None
    assert repository.terminal_calls == [TurnSubmissionState.CANCELLED]
    assert [event.id for event in repository.outbox] == [
        f"{TURN_ID}:cancelled"
    ]
    assert task.input_stream.acked == [
        (runner._INPUT_CONSUMER_GROUP, "1-0")
    ]


@pytest.mark.asyncio
async def test_cancelled_terminal_ack_loss_duplicate_only_retries_ack():
    repository = FakeTurnRepository()
    input_stream = FakeInputStream(fail_ack_once=True)
    flow_started = asyncio.Event()
    flow_invocations = 0

    async def blocking_flow(message, resumes_waiting=None):
        nonlocal flow_invocations
        flow_invocations += 1
        flow_started.set()
        await asyncio.Event().wait()
        yield DoneEvent()

    runner, task, flow_calls = _build_runner(
        repository,
        input_stream=input_stream,
        run_flow=blocking_flow,
    )
    execution = asyncio.create_task(
        runner._process_durable_entry(task, "1-0", _input_json())
    )
    await asyncio.wait_for(flow_started.wait(), timeout=1)
    execution.cancel("cancel_requested")

    with pytest.raises(asyncio.CancelledError):
        await execution
    assert repository.turn.state == TurnSubmissionState.CANCELLED
    assert input_stream.ack_attempts == 1
    assert input_stream.acked == []

    assert await runner._process_durable_entry(task, "1-0", _input_json())
    assert repository.terminal_calls == [TurnSubmissionState.CANCELLED]
    assert input_stream.ack_attempts == 2
    assert input_stream.acked == [
        (runner._INPUT_CONSUMER_GROUP, "1-0")
    ]
    assert flow_invocations == 1
    assert runner._sandbox.ensure_calls == 1


@pytest.mark.asyncio
async def test_terminal_claim_cancel_waits_for_ack_before_propagating():
    class TerminalReplyRepository(FakeTurnRepository):
        def __init__(self):
            super().__init__(state=TurnSubmissionState.COMPLETED)
            self.claim_replied = asyncio.Event()
            self.release_reply = asyncio.Event()

        async def claim_for_execution(self, *args, **kwargs):
            result = await super().claim_for_execution(*args, **kwargs)
            self.claim_replied.set()
            await self.release_reply.wait()
            return result

    class BlockingAckInput(FakeInputStream):
        def __init__(self):
            super().__init__()
            self.ack_started = asyncio.Event()
            self.release_ack = asyncio.Event()

        async def ack(self, group, transport_id):
            self.ack_started.set()
            await self.release_ack.wait()
            return await super().ack(group, transport_id)

    repository = TerminalReplyRepository()
    input_stream = BlockingAckInput()
    runner, task, flow_calls = _build_runner(
        repository, input_stream=input_stream
    )
    execution = asyncio.create_task(
        runner._process_durable_entry(task, "1-0", _input_json())
    )
    await asyncio.wait_for(repository.claim_replied.wait(), timeout=1)
    execution.cancel("cancel_requested")
    repository.release_reply.set()
    await asyncio.wait_for(input_stream.ack_started.wait(), timeout=1)

    execution.cancel("cancel_requested")
    await asyncio.sleep(0)
    assert not execution.done()
    input_stream.release_ack.set()

    with pytest.raises(asyncio.CancelledError) as cancellation:
        await execution
    assert cancellation.value.args == ("cancel_requested",)
    assert input_stream.acked == [
        (runner._INPUT_CONSUMER_GROUP, "1-0")
    ]
    assert flow_calls["count"] == 0
    assert runner._sandbox.ensure_calls == 0


@pytest.mark.asyncio
async def test_repeated_cancel_during_committed_claim_waits_then_terminalizes():
    class CommitBeforeReplyRepository(FakeTurnRepository):
        def __init__(self):
            super().__init__()
            self.claim_committed = asyncio.Event()
            self.release_reply = asyncio.Event()

        async def claim_for_execution(
            self, session_id, submission_id, *, task_id, owner, claim_until
        ):
            result = await super().claim_for_execution(
                session_id,
                submission_id,
                task_id=task_id,
                owner=owner,
                claim_until=claim_until,
            )
            self.claim_committed.set()
            await self.release_reply.wait()
            return result

    repository = CommitBeforeReplyRepository()
    runner, task, flow_calls = _build_runner(repository)
    execution = asyncio.create_task(
        runner._process_durable_entry(task, "1-0", _input_json())
    )
    await asyncio.wait_for(repository.claim_committed.wait(), timeout=1)

    execution.cancel("cancel_requested")
    await asyncio.sleep(0)
    execution.cancel("cancel_requested")
    await asyncio.sleep(0)

    assert not execution.done()
    repository.release_reply.set()
    with pytest.raises(asyncio.CancelledError) as cancellation:
        await execution

    assert cancellation.value.args == ("cancel_requested",)
    assert repository.turn.state == TurnSubmissionState.CANCELLED
    assert repository.terminal_calls == [TurnSubmissionState.CANCELLED]
    assert task.input_stream.acked
    assert runner._sandbox.ensure_calls == 0
    assert flow_calls["count"] == 0


@pytest.mark.asyncio
async def test_cancel_after_claim_during_status_projection_terminalizes_owner():
    class BlockingTerminalRepository(FakeTurnRepository):
        def __init__(self):
            super().__init__()
            self.terminal_started = asyncio.Event()
            self.release_terminal = asyncio.Event()

        async def mark_terminal(self, *args, **kwargs):
            self.terminal_started.set()
            await self.release_terminal.wait()
            return await super().mark_terminal(*args, **kwargs)

    repository = BlockingTerminalRepository()
    runner, task, flow_calls = _build_runner(repository)
    projection_started = asyncio.Event()
    projection_calls = 0

    async def blocking_first_projection(*, idle_status="completed"):
        nonlocal projection_calls
        projection_calls += 1
        if projection_calls == 1:
            projection_started.set()
            await asyncio.Event().wait()

    runner._sync_durable_session_status = blocking_first_projection
    execution = asyncio.create_task(
        runner._process_durable_entry(task, "1-0", _input_json())
    )
    await asyncio.wait_for(projection_started.wait(), timeout=1)
    assert repository.turn.state == TurnSubmissionState.RUNNING

    execution.cancel("cancel_requested")
    await asyncio.wait_for(repository.terminal_started.wait(), timeout=1)
    execution.cancel("cancel_requested")
    await asyncio.sleep(0)

    assert not execution.done()
    assert repository.turn.state == TurnSubmissionState.RUNNING
    repository.release_terminal.set()
    with pytest.raises(asyncio.CancelledError) as cancellation:
        await execution

    assert cancellation.value.args == ("cancel_requested",)
    assert repository.turn.state == TurnSubmissionState.CANCELLED
    assert repository.terminal_calls == [TurnSubmissionState.CANCELLED]
    assert task.input_stream.acked
    assert runner._sandbox.ensure_calls == 0
    assert flow_calls["count"] == 0


@pytest.mark.asyncio
async def test_post_claim_session_recheck_settles_changed_lifecycle_without_runtime():
    class BlockingTerminalRepository(FakeTurnRepository):
        def __init__(self):
            super().__init__()
            self.terminal_started = asyncio.Event()
            self.release_terminal = asyncio.Event()

        async def mark_terminal(self, *args, **kwargs):
            self.terminal_started.set()
            await self.release_terminal.wait()
            return await super().mark_terminal(*args, **kwargs)

    repository = BlockingTerminalRepository()
    runner, task, flow_calls = _build_runner(repository)
    reads = 0

    async def lifecycle_changes_after_claim(session_id, user_id):
        nonlocal reads
        reads += 1
        return SimpleNamespace(
            agent_id=AGENT_ID,
            task_id=TASK_ID,
            deleting=reads > 1,
            sandbox_destroying=False,
            sandbox_id=SANDBOX_ID,
            task_sandbox_id=SANDBOX_ID,
        )

    runner._session_repository.find_by_id_and_user_id = (
        lifecycle_changes_after_claim
    )

    execution = asyncio.create_task(
        runner._process_durable_entry(task, "1-0", _input_json())
    )
    await asyncio.wait_for(repository.terminal_started.wait(), timeout=1)
    execution.cancel("cancel_requested")
    await asyncio.sleep(0)

    assert not execution.done()
    assert repository.turn.state == TurnSubmissionState.RUNNING
    repository.release_terminal.set()
    with pytest.raises(asyncio.CancelledError) as cancellation:
        await execution

    assert cancellation.value.args == ("session_lifecycle_changed",)
    assert reads == 2
    assert repository.turn.state == TurnSubmissionState.CANCELLED
    assert repository.terminal_calls == [TurnSubmissionState.CANCELLED]
    assert task.input_stream.acked
    assert runner._sandbox.ensure_calls == 0
    assert flow_calls["count"] == 0


@pytest.mark.asyncio
async def test_control_claim_loss_leaves_running_turn_unacked_for_expiry():
    repository = FakeTurnRepository()
    flow_started = asyncio.Event()

    async def blocking_flow(message, resumes_waiting=None):
        flow_started.set()
        await asyncio.Event().wait()
        yield DoneEvent()

    runner, task, _ = _build_runner(repository, run_flow=blocking_flow)
    execution = asyncio.create_task(
        runner._process_durable_entry(task, "1-0", _input_json())
    )
    await asyncio.wait_for(flow_started.wait(), timeout=1)
    execution.cancel("claim_lost")

    with pytest.raises(asyncio.CancelledError) as cancellation:
        await execution
    assert cancellation.value.args == ("claim_lost",)
    assert repository.turn.state == TurnSubmissionState.RUNNING
    assert repository.terminal_calls == []
    assert task.input_stream.acked == []


@pytest.mark.asyncio
async def test_stale_worker_only_acks_transport_rebound_to_replacement_task():
    repository = FakeTurnRepository()
    repository.turn.task_id = "replacement-task"
    runner, task, flow_calls = _build_runner(repository)

    async def replacement_session(session_id, user_id):
        return SimpleNamespace(
            agent_id=AGENT_ID,
            task_id="replacement-task",
            deleting=False,
            sandbox_destroying=False,
            sandbox_id=SANDBOX_ID,
            task_sandbox_id=SANDBOX_ID,
        )

    runner._session_repository.find_by_id_and_user_id = replacement_session

    assert await runner._process_durable_entry(task, "old-1", _input_json())
    assert repository.turn.state == TurnSubmissionState.ENQUEUED
    assert repository.turn.task_id == "replacement-task"
    assert repository.terminal_calls == []
    assert flow_calls["count"] == 0
    assert task.input_stream.acked == [
        (runner._INPUT_CONSUMER_GROUP, "old-1")
    ]


@pytest.mark.asyncio
async def test_malformed_associated_input_terminalizes_and_unknown_is_quarantined():
    associated_repository = FakeTurnRepository()
    runner, task, _ = _build_runner(associated_repository)
    malformed_associated = json.dumps(
        {"id": TURN_ID, "turn_id": TURN_ID, "type": "not-an-event"}
    )

    assert await runner._process_durable_entry(
        task, "1-0", malformed_associated
    )
    assert associated_repository.turn.state == TurnSubmissionState.FAILED
    assert task.input_stream.acked
    assert task.input_stream.quarantined == []

    unknown_repository = FakeTurnRepository()
    unknown_runner, unknown_task, _ = _build_runner(unknown_repository)
    assert await unknown_runner._process_durable_entry(
        unknown_task, "2-0", "{not-json"
    )
    assert unknown_repository.turn.state == TurnSubmissionState.ENQUEUED
    assert len(unknown_task.input_stream.quarantined) == 1
    _, _, reason, digest = unknown_task.input_stream.quarantined[0]
    assert reason == "Malformed durable input event"
    assert len(digest) == 64
