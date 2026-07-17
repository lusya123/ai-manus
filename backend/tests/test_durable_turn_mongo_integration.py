"""Real-Mongo checks for cross-replica durable turn invariants.

The module skips on developer/CI hosts without ``mongod``. It deliberately
uses standalone Mongo (no transactions) because production safety must not
depend on replica-set-only transaction support.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import shutil
import socket
import subprocess
import tempfile
import time
from types import MethodType

import pytest
from pymongo import ASCENDING, MongoClient
from pymongo.asynchronous.mongo_client import AsyncMongoClient

from app.domain.models.event import DoneEvent, MessageEvent
from app.domain.models.turn_submission import (
    TurnClaimDecision,
    TurnSubmission,
    TurnSubmissionCapacityError,
    TurnSubmissionConflictError,
    TurnSubmissionState,
    TurnSubmissionUnavailableError,
)
from app.infrastructure.repositories.mongo_turn_submission_repository import (
    MongoTurnSubmissionRepository,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def standalone_mongo_uri():
    executable = shutil.which("mongod")
    if not executable:
        pytest.skip("mongod is not installed")
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="manus-turn-mongo-") as data_dir:
        process = subprocess.Popen(
            [
                executable,
                "--dbpath",
                data_dir,
                "--port",
                str(port),
                "--bind_ip",
                "127.0.0.1",
                "--nounixsocket",
                "--quiet",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        uri = f"mongodb://127.0.0.1:{port}"
        deadline = time.monotonic() + 10
        while True:
            try:
                probe = MongoClient(uri, serverSelectionTimeoutMS=200)
                probe.admin.command("ping")
                probe.close()
                break
            except Exception:
                if process.poll() is not None or time.monotonic() >= deadline:
                    process.terminate()
                    pytest.skip("temporary standalone mongod did not start")
                time.sleep(0.05)
        try:
            yield uri
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


@pytest.fixture
async def mongo_repositories(standalone_mongo_uri):
    client = AsyncMongoClient(standalone_mongo_uri, tz_aware=True)
    database = client[f"turn_test_{time.time_ns()}"]
    turns = database.turn_submissions
    quotas = database.turn_quotas
    outputs = database.turn_output_events
    await turns.create_index(
        [("session_id", ASCENDING), ("submission_id", ASCENDING)],
        unique=True,
    )
    await quotas.create_index("scope_key", unique=True)
    await outputs.create_index(
        [("session_id", 1), ("submission_id", 1), ("event_id", 1)],
        unique=True,
    )

    def repository(**kwargs):
        repo = MongoTurnSubmissionRepository(**kwargs)
        repo._turn_collection = MethodType(lambda self: turns, repo)
        repo._quota_collection = MethodType(lambda self: quotas, repo)
        repo._output_collection = MethodType(lambda self: outputs, repo)
        return repo

    try:
        yield repository, turns, quotas, outputs, client
    finally:
        await client.drop_database(database.name)
        await client.close()


def _turn(
    submission_id: str,
    *,
    session_id: str = "session-a",
    user_id: str = "user-1",
    request_hash: str = "hash-a",
) -> TurnSubmission:
    event = MessageEvent(
        id=submission_id,
        turn_id=submission_id,
        role="user",
        message="hello",
    )
    return TurnSubmission(
        session_id=session_id,
        submission_id=submission_id,
        user_id=user_id,
        agent_id="agent-1",
        request_hash=request_hash,
        input_json=event.model_dump_json(),
    )


async def test_same_id_two_replicas_is_one_accept_and_payload_mismatch_is_409(
    mongo_repositories,
):
    factory, turns, _quotas, _outputs, _client = mongo_repositories
    first = factory()
    second = factory()
    candidate = _turn("11111111-1111-4111-8111-111111111111")

    results = await asyncio.gather(
        first.accept(candidate.model_copy(deep=True)),
        second.accept(candidate.model_copy(deep=True)),
    )

    assert sorted(created for _turn_value, created in results) == [False, True]
    assert await turns.count_documents({}) == 1
    with pytest.raises(TurnSubmissionConflictError):
        await second.accept(
            candidate.model_copy(update={"request_hash": "different"})
        )


async def test_waiting_resume_round_trips_and_same_id_retry_keeps_first_decision(
    mongo_repositories,
):
    factory, turns, _quotas, _outputs, _client = mongo_repositories
    first = factory()
    second = factory()
    candidate = _turn(
        "12121212-1212-4212-8212-121212121212"
    ).model_copy(update={"resumes_waiting": True})

    accepted, created = await first.accept(candidate)
    retried, retry_created = await second.accept(
        candidate.model_copy(update={"resumes_waiting": False})
    )
    raw = await turns.find_one(
        {
            "session_id": candidate.session_id,
            "submission_id": candidate.submission_id,
        }
    )

    assert created is True
    assert retry_created is False
    assert accepted.resumes_waiting is True
    assert retried.resumes_waiting is True
    assert raw["resumes_waiting"] is True


async def test_one_user_quota_document_atomically_enforces_both_limits_and_releases(
    mongo_repositories,
):
    factory, _turns, quotas, _outputs, _client = mongo_repositories
    first = factory(max_active_per_session=2, max_active_per_user=3)
    second = factory(max_active_per_session=2, max_active_per_user=3)
    turns = [
        _turn(f"00000000-0000-4000-8000-{index:012d}")
        for index in range(4)
    ]

    accepted = await asyncio.gather(
        first.accept(turns[0]),
        second.accept(turns[1]),
    )
    assert all(created for _value, created in accepted)
    with pytest.raises(TurnSubmissionCapacityError, match="session"):
        await first.accept(turns[2])

    other_session = _turn(
        "00000000-0000-4000-8000-000000000010",
        session_id="session-b",
    )
    await second.accept(other_session)
    with pytest.raises(TurnSubmissionCapacityError, match="user"):
        await first.accept(
            _turn(
                "00000000-0000-4000-8000-000000000011",
                session_id="session-c",
            )
        )

    quota = await quotas.find_one({"scope_key": "user:user-1"})
    assert len(quota["active_turns"]) == 3
    assert await first.cancel_queued("session-a", user_id="user-1") == 2
    # cancelled is terminal and releases capacity rather than pinning a user
    # until the 30-day idempotency TTL expires.
    await first.accept(turns[2])


async def test_claim_is_single_execution_terminal_precedes_ack_and_expiry_is_unknown(
    mongo_repositories,
):
    factory, turns, _quotas, outputs, _client = mongo_repositories
    repo = factory(terminal_retention_days=30)
    submission_id = "22222222-2222-4222-8222-222222222222"
    accepted, _ = await repo.accept(_turn(submission_id))
    assert accepted.expires_at is None
    enqueued = await repo.mark_enqueued(
        accepted.session_id,
        submission_id,
        task_id="task-1",
        stream_id="1-0",
    )
    assert enqueued.expires_at is None

    first_claim = await repo.claim_for_execution(
        accepted.session_id,
        submission_id,
        task_id="task-1",
        owner="worker-1",
        claim_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    duplicate_claim = await repo.claim_for_execution(
        accepted.session_id,
        submission_id,
        task_id="task-1",
        owner="worker-2",
        claim_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert first_claim.decision == TurnClaimDecision.EXECUTE
    assert duplicate_claim.decision == TurnClaimDecision.RETRY

    done_output = DoneEvent(
        id="done-event",
        turn_id=submission_id,
    )
    await repo.append_output(accepted.session_id, submission_id, done_output)
    await repo.append_output(accepted.session_id, submission_id, done_output)
    assert await outputs.count_documents(
        {"session_id": accepted.session_id, "submission_id": submission_id}
    ) == 1

    assert await repo.mark_terminal(
        accepted.session_id,
        submission_id,
        owner="worker-1",
        state=TurnSubmissionState.COMPLETED,
        terminal_event_id="done-event",
    )
    terminal = await repo.find(accepted.session_id, submission_id)
    assert terminal.state == TurnSubmissionState.COMPLETED
    assert terminal.expires_at.tzinfo is not None
    assert timedelta(days=29) < terminal.expires_at - datetime.now(UTC) < timedelta(days=31)
    after_ack_loss = await repo.claim_for_execution(
        accepted.session_id,
        submission_id,
        task_id="task-1",
        owner="worker-3",
        claim_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert after_ack_loss.decision == TurnClaimDecision.ACK
    output_document = await outputs.find_one(
        {"session_id": accepted.session_id, "submission_id": submission_id}
    )
    assert output_document["expires_at"] == terminal.expires_at

    crashed_id = "33333333-3333-4333-8333-333333333333"
    crashed, _ = await repo.accept(_turn(crashed_id))
    await repo.mark_enqueued(
        crashed.session_id,
        crashed_id,
        task_id="task-1",
        stream_id="2-0",
    )
    await repo.claim_for_execution(
        crashed.session_id,
        crashed_id,
        task_id="task-1",
        owner="crashed-worker",
        claim_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    await turns.update_one(
        {"session_id": crashed.session_id, "submission_id": crashed_id},
        {"$set": {"claim_until": datetime.now(UTC) - timedelta(seconds=1)}},
    )
    expired = await repo.claim_for_execution(
        crashed.session_id,
        crashed_id,
        task_id="task-1",
        owner="recovery-worker",
        claim_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert expired.decision == TurnClaimDecision.ACK
    assert expired.turn.state == TurnSubmissionState.FAILED_UNKNOWN
    assert expired.turn.expires_at is not None


async def test_turn_outbox_replays_beyond_bounded_session_history(
    mongo_repositories,
):
    factory, _turns, _quotas, outputs, _client = mongo_repositories
    repo = factory()
    submission_id = "55555555-5555-4555-8555-555555555555"
    turn, _ = await repo.accept(_turn(submission_id))
    events = [
        MessageEvent(
            id=f"event-{index:04d}",
            turn_id=submission_id,
            message=f"chunk-{index}",
            # Deliberately reverse event clocks: replay order must come from
            # Mongo insertion sequence, not timestamps supplied by events.
            timestamp=datetime.now(UTC) - timedelta(seconds=index),
        )
        for index in range(520)
    ]
    for event in events:
        await repo.append_output(turn.session_id, submission_id, event)

    replayed = await repo.list_outputs(turn.session_id, submission_id)
    assert len(replayed) == 520
    assert [event.id for event in replayed] == [event.id for event in events]
    assert await outputs.count_documents(
        {"session_id": turn.session_id, "submission_id": submission_id}
    ) == 520


async def test_terminal_ack_path_repairs_a_previous_quota_release_failure(
    mongo_repositories,
):
    factory, _turns, quotas, _outputs, _client = mongo_repositories
    repo = factory()
    submission_id = "66666666-6666-4666-8666-666666666666"
    turn, _ = await repo.accept(_turn(submission_id))
    await repo.mark_enqueued(
        turn.session_id,
        submission_id,
        task_id="task-1",
        stream_id="1-0",
    )
    await repo.claim_for_execution(
        turn.session_id,
        submission_id,
        task_id="task-1",
        owner="worker-1",
        claim_until=datetime.now(UTC) + timedelta(minutes=5),
    )

    original_release = repo._release_turn
    failed_once = False

    async def fail_release_once(current):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise ConnectionError("quota collection unavailable")
        await original_release(current)

    repo._release_turn = fail_release_once
    with pytest.raises(TurnSubmissionUnavailableError):
        await repo.mark_terminal(
            turn.session_id,
            submission_id,
            owner="worker-1",
            state=TurnSubmissionState.COMPLETED,
            terminal_event_id="done",
        )

    # The terminal write committed, but the reservation is still present.
    quota = await quotas.find_one({"scope_key": "user:user-1"})
    assert len(quota["active_turns"]) == 1
    repaired = await repo.claim_for_execution(
        turn.session_id,
        submission_id,
        task_id="task-1",
        owner="worker-2",
        claim_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert repaired.decision == TurnClaimDecision.ACK
    quota = await quotas.find_one({"scope_key": "user:user-1"})
    assert quota["active_turns"] == []


async def test_factory_failure_only_terminalizes_exact_enqueued_task_turns(
    mongo_repositories,
):
    factory, _turns, quotas, outputs, _client = mongo_repositories
    repo = factory(max_active_per_session=8, max_active_per_user=8)
    identifiers = [
        "77777777-7777-4777-8777-777777777771",
        "77777777-7777-4777-8777-777777777772",
        "77777777-7777-4777-8777-777777777773",
        "77777777-7777-4777-8777-777777777774",
    ]
    for submission_id in identifiers:
        turn, _ = await repo.accept(_turn(submission_id))
        await repo.mark_enqueued(
            turn.session_id,
            submission_id,
            task_id="task-1" if submission_id != identifiers[2] else "task-2",
            stream_id=f"{len(submission_id)}-0",
        )

    await repo.claim_for_execution(
        "session-a",
        identifiers[3],
        task_id="task-1",
        owner="live-worker",
        claim_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    await repo.append_output(
        "session-a",
        identifiers[0],
        MessageEvent(
            id="pre-failure-output",
            turn_id=identifiers[0],
            role="assistant",
            message="diagnostic",
        ),
    )

    repaired = await repo.fail_enqueued_for_task(
        "session-a",
        user_id="user-1",
        task_id="task-1",
        error="Agent worker could not initialize before execution",
    )

    assert repaired == 2
    states = {
        submission_id: (await repo.find("session-a", submission_id)).state
        for submission_id in identifiers
    }
    assert states == {
        identifiers[0]: TurnSubmissionState.FAILED,
        identifiers[1]: TurnSubmissionState.FAILED,
        identifiers[2]: TurnSubmissionState.ENQUEUED,
        identifiers[3]: TurnSubmissionState.RUNNING,
    }
    quota = await quotas.find_one({"scope_key": "user:user-1"})
    assert {entry["submission_id"] for entry in quota["active_turns"]} == {
        identifiers[2],
        identifiers[3],
    }
    output = await outputs.find_one({"event_id": "pre-failure-output"})
    terminal = await repo.find("session-a", identifiers[0])
    assert output["expires_at"] == terminal.expires_at

    # Retrying after an acknowledgement failure repairs postconditions and
    # never broadens the task/user/state predicate.
    assert await repo.fail_enqueued_for_task(
        "session-a",
        user_id="user-1",
        task_id="task-1",
        error="Agent worker could not initialize before execution",
    ) == 2


async def test_factory_recovery_waits_for_live_turn_then_fences_expired_claim(
    mongo_repositories,
):
    factory, turns, quotas, outputs, _client = mongo_repositories
    repo = factory(terminal_retention_days=30)
    submission_id = "88888888-8888-4888-8888-888888888888"
    turn, _ = await repo.accept(_turn(submission_id))
    await repo.mark_enqueued(
        turn.session_id,
        submission_id,
        task_id="task-factory",
        stream_id="1-0",
    )
    await repo.claim_for_execution(
        turn.session_id,
        submission_id,
        task_id="task-factory",
        owner="previous-worker",
        claim_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    await repo.append_output(
        turn.session_id,
        submission_id,
        MessageEvent(
            id="possible-side-effect",
            turn_id=submission_id,
            role="assistant",
            message="partial",
        ),
    )

    error = "Agent worker could not initialize before execution"
    assert not await repo.recover_factory_failure_for_task(
        turn.session_id,
        user_id=turn.user_id,
        task_id="task-factory",
        error=error,
    )
    current = await repo.find(turn.session_id, submission_id)
    assert current.state == TurnSubmissionState.RUNNING
    quota = await quotas.find_one({"scope_key": "user:user-1"})
    assert {item["submission_id"] for item in quota["active_turns"]} == {
        submission_id
    }

    await turns.update_one(
        {"session_id": turn.session_id, "submission_id": submission_id},
        {"$set": {"claim_until": datetime.now(UTC) - timedelta(seconds=1)}},
    )
    assert await repo.recover_factory_failure_for_task(
        turn.session_id,
        user_id=turn.user_id,
        task_id="task-factory",
        error=error,
    )
    terminal = await repo.find(turn.session_id, submission_id)
    assert terminal.state == TurnSubmissionState.FAILED_UNKNOWN
    assert "side-effect status is unknown" in terminal.terminal_error
    quota = await quotas.find_one({"scope_key": "user:user-1"})
    assert quota["active_turns"] == []
    output = await outputs.find_one({"event_id": "possible-side-effect"})
    assert output["expires_at"] == terminal.expires_at


async def test_mongo_unavailable_fails_closed(mongo_repositories):
    bad_client = AsyncMongoClient(
        f"mongodb://127.0.0.1:{_free_port()}",
        serverSelectionTimeoutMS=50,
    )
    repo = MongoTurnSubmissionRepository()
    repo._turn_collection = MethodType(
        lambda self: bad_client.unavailable.turns, repo
    )
    repo._quota_collection = MethodType(
        lambda self: bad_client.unavailable.quotas, repo
    )
    repo._output_collection = MethodType(
        lambda self: bad_client.unavailable.outputs, repo
    )
    try:
        with pytest.raises(TurnSubmissionUnavailableError):
            await repo.accept(_turn("44444444-4444-4444-8444-444444444444"))
    finally:
        await bad_client.close()
