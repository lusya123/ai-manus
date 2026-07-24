"""Celery-backed Task implementation.

The API process only enqueues tasks and reads/writes Redis: agent execution
happens in Celery worker processes (see celery_worker.py). Cross-process
state lives entirely in Redis:

- ``task:input:{id}`` / ``task:output:{id}``  — Redis Streams for messages/events
- ``task:meta:{id}``                          — task status + runner params (JSON)
- ``task:cancel:{id}``                        — cancellation flag polled by the worker

This makes tasks visible from any API replica and survives API restarts,
unlike the in-process registry of RedisStreamTask.
"""
import asyncio
import json
import uuid
import logging
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional

from app.domain.external.task import Task, TaskRunnerFactory
from app.infrastructure.external.message_queue.redis_stream_queue import RedisStreamQueue, MessageQueue
from app.infrastructure.storage.redis import get_redis
from app.infrastructure.external.task.celery_app import celery_app, AGENT_TASK_NAME

logger = logging.getLogger(__name__)

META_TTL_SECONDS = 7 * 24 * 3600
CANCEL_TTL_SECONDS = 3600
RERUN_TTL_SECONDS = META_TTL_SECONDS
DISPATCH_LEASE_SECONDS = 30
WORKER_CLAIM_LEASE_SECONDS = 300
EXECUTION_LEASE_SECONDS = 60

STATUS_DISPATCHING = "dispatching"
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"

DispatchDecision = Literal["dispatch", "rerun", "wait"]
FinishDecision = Literal["continue", "done", "stale"]
ClaimDecision = Literal["claimed", "busy", "stale"]


@dataclass(frozen=True)
class DispatchRequest:
    decision: DispatchDecision
    token: str


# All state transitions that can race with another API replica or worker are
# performed by Redis, not by a read followed by a write in Python.  A
# generation token prevents a delayed delivery from an older dispatch from
# claiming or completing the current generation.
_REQUEST_RUN_SCRIPT = r"""
local raw = redis.call('GET', KEYS[1])
local meta = nil
if raw then
  local ok, decoded = pcall(cjson.decode, raw)
  if ok then meta = decoded end
end

local status = meta and meta['status'] or nil
if status == 'pending' or status == 'running' then
  redis.call('SET', KEYS[2], '1', 'EX', tonumber(ARGV[4]))
  return 'rerun'
end

if status == 'dispatching' then
  local clock = redis.call('TIME')
  local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
  local deadline = tonumber(meta['dispatch_deadline_ms'] or 0)
  if deadline > now_ms then
    return 'wait'
  end
end

local clock = redis.call('TIME')
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local replacement = {
  status = 'dispatching',
  params = cjson.decode(ARGV[1]),
  dispatch_token = ARGV[2],
  dispatch_deadline_ms = now_ms + tonumber(ARGV[5])
}
redis.call('SET', KEYS[1], cjson.encode(replacement), 'EX', tonumber(ARGV[3]))
redis.call('DEL', KEYS[2], KEYS[3])
return 'dispatch'
"""

_PUBLISH_DISPATCH_SCRIPT = r"""
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local ok, meta = pcall(cjson.decode, raw)
if not ok then return 0 end
if meta['status'] ~= 'dispatching' or meta['dispatch_token'] ~= ARGV[1] then
  return 0
end
meta['status'] = 'pending'
meta['dispatch_deadline_ms'] = nil
redis.call('SET', KEYS[1], cjson.encode(meta), 'EX', tonumber(ARGV[2]))
return 1
"""

_ABORT_DISPATCH_SCRIPT = r"""
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local ok, meta = pcall(cjson.decode, raw)
if not ok then return 0 end
if meta['status'] ~= 'dispatching' or meta['dispatch_token'] ~= ARGV[1] then
  return 0
end
meta['status'] = 'done'
meta['dispatch_deadline_ms'] = nil
redis.call('SET', KEYS[1], cjson.encode(meta), 'EX', tonumber(ARGV[2]))
redis.call('DEL', KEYS[2], KEYS[3])
return 1
"""

_CLAIM_DISPATCH_SCRIPT = r"""
local raw = redis.call('GET', KEYS[1])
if not raw then return 'stale' end
local ok, meta = pcall(cjson.decode, raw)
if not ok then return 'stale' end
local expected = meta['dispatch_token']
if expected and expected ~= ARGV[1] then return 'stale' end
local clock = redis.call('TIME')
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if meta['status'] == 'running' then
  if meta['worker_claim_id'] == ARGV[2] then
    meta['worker_claim_deadline_ms'] = now_ms + tonumber(ARGV[4])
    redis.call('SET', KEYS[1], cjson.encode(meta), 'EX', tonumber(ARGV[3]))
    return 'claimed'
  end
  local deadline = tonumber(meta['worker_claim_deadline_ms'] or 0)
  if deadline > now_ms then return 'busy' end
elseif meta['status'] ~= 'dispatching' and meta['status'] ~= 'pending' then
  return 'stale'
end
if not expected then
  meta['dispatch_token'] = ARGV[1]
end
meta['status'] = 'running'
meta['dispatch_deadline_ms'] = nil
meta['worker_claim_id'] = ARGV[2]
meta['worker_claim_deadline_ms'] = now_ms + tonumber(ARGV[4])
redis.call('SET', KEYS[1], cjson.encode(meta), 'EX', tonumber(ARGV[3]))
return 'claimed'
"""

_RENEW_CLAIM_SCRIPT = r"""
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local ok, meta = pcall(cjson.decode, raw)
if not ok then return 0 end
if meta['status'] ~= 'running'
  or meta['dispatch_token'] ~= ARGV[1]
  or meta['worker_claim_id'] ~= ARGV[2] then
  return 0
end
local clock = redis.call('TIME')
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
meta['worker_claim_deadline_ms'] = now_ms + tonumber(ARGV[4])
redis.call('SET', KEYS[1], cjson.encode(meta), 'EX', tonumber(ARGV[3]))
return 1
"""

_FINISH_CYCLE_SCRIPT = r"""
local raw = redis.call('GET', KEYS[1])
if not raw then return 'stale' end
local ok, meta = pcall(cjson.decode, raw)
if not ok then return 'stale' end
local expected = meta['dispatch_token']
if expected and expected ~= ARGV[1] then return 'stale' end
local claim = meta['worker_claim_id']
if claim and claim ~= ARGV[2] then return 'stale' end
if meta['last_finish_cycle'] == ARGV[5] then
  return meta['last_finish_decision'] or 'stale'
end
if meta['status'] ~= 'running' then return 'stale' end

if ARGV[4] == '1' or redis.call('EXISTS', KEYS[3]) == 1 then
  redis.call('DEL', KEYS[2], KEYS[3])
  meta['status'] = 'done'
  meta['worker_claim_deadline_ms'] = nil
  meta['last_finish_cycle'] = ARGV[5]
  meta['last_finish_decision'] = 'done'
  redis.call('SET', KEYS[1], cjson.encode(meta), 'EX', tonumber(ARGV[3]))
  return 'done'
end

if redis.call('GETDEL', KEYS[2]) then
  meta['last_finish_cycle'] = ARGV[5]
  meta['last_finish_decision'] = 'continue'
  redis.call('SET', KEYS[1], cjson.encode(meta), 'EX', tonumber(ARGV[3]))
  return 'continue'
end

redis.call('DEL', KEYS[3])
meta['status'] = 'done'
meta['worker_claim_deadline_ms'] = nil
meta['last_finish_cycle'] = ARGV[5]
meta['last_finish_decision'] = 'done'
redis.call('SET', KEYS[1], cjson.encode(meta), 'EX', tonumber(ARGV[3]))
return 'done'
"""

_CANCEL_TASK_SCRIPT = r"""
local raw = redis.call('GET', KEYS[1])
if not raw then return 'done' end
local ok, meta = pcall(cjson.decode, raw)
if not ok then return 'done' end
local status = meta['status']
if status == 'done' then return 'done' end
if status == 'dispatching' or status == 'pending' then
  meta['status'] = 'done'
  meta['dispatch_deadline_ms'] = nil
  redis.call('SET', KEYS[1], cjson.encode(meta), 'EX', tonumber(ARGV[1]))
  redis.call('DEL', KEYS[2], KEYS[3])
  return 'cancelled'
end
if status == 'running' then
  redis.call('SET', KEYS[3], '1', 'EX', tonumber(ARGV[2]))
  return 'requested'
end
return 'done'
"""

_ACQUIRE_EXECUTION_SCRIPT = r"""
local owner = redis.call('GET', KEYS[1])
if owner == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
  return 1
end
if owner then return 0 end
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', tonumber(ARGV[2])) then
  return 1
end
return 0
"""

_RENEW_EXECUTION_SCRIPT = r"""
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
return 1
"""

_RELEASE_EXECUTION_SCRIPT = r"""
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""

def meta_key(task_id: str) -> str:
    return f"task:meta:{task_id}"


def cancel_key(task_id: str) -> str:
    return f"task:cancel:{task_id}"


def rerun_key(task_id: str) -> str:
    return f"task:rerun:{task_id}"


def execution_key(task_id: str) -> str:
    return f"task:execution:{task_id}"


async def acquire_execution_lease(task_id: str, owner_id: str) -> bool:
    """Serialize physical Celery deliveries, even with one logical claim ID."""
    if not owner_id:
        raise ValueError("execution owner_id is required")
    return bool(
        await get_redis().client.eval(
            _ACQUIRE_EXECUTION_SCRIPT,
            1,
            execution_key(task_id),
            owner_id,
            EXECUTION_LEASE_SECONDS * 1000,
        )
    )


async def renew_execution_lease(task_id: str, owner_id: str) -> bool:
    return bool(
        await get_redis().client.eval(
            _RENEW_EXECUTION_SCRIPT,
            1,
            execution_key(task_id),
            owner_id,
            EXECUTION_LEASE_SECONDS * 1000,
        )
    )


async def release_execution_lease(task_id: str, owner_id: str) -> bool:
    return bool(
        await get_redis().client.eval(
            _RELEASE_EXECUTION_SCRIPT,
            1,
            execution_key(task_id),
            owner_id,
        )
    )


async def read_meta(task_id: str) -> Optional[Dict[str, Any]]:
    raw = await get_redis().client.get(meta_key(task_id))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Invalid task metadata for task %s", task_id)
        return None


async def request_cancel(task_id: str) -> bool:
    """Atomically retire unclaimed work or signal its owning worker."""
    decision = await get_redis().client.eval(
        _CANCEL_TASK_SCRIPT,
        3,
        meta_key(task_id),
        rerun_key(task_id),
        cancel_key(task_id),
        META_TTL_SECONDS,
        CANCEL_TTL_SECONDS,
    )
    if decision not in {"cancelled", "requested", "done"}:
        raise RuntimeError("Redis returned an invalid task cancellation decision")
    return decision != "done"


async def is_cancel_requested(task_id: str) -> bool:
    return bool(await get_redis().client.exists(cancel_key(task_id)))


async def request_dispatch(
    task_id: str, params: Dict[str, Any], *, token: Optional[str] = None
) -> DispatchRequest:
    dispatch_token = token or str(uuid.uuid4())
    decision = await get_redis().client.eval(
        _REQUEST_RUN_SCRIPT,
        3,
        meta_key(task_id),
        rerun_key(task_id),
        cancel_key(task_id),
        json.dumps(params, separators=(",", ":"), sort_keys=True),
        dispatch_token,
        META_TTL_SECONDS,
        RERUN_TTL_SECONDS,
        DISPATCH_LEASE_SECONDS * 1000,
    )
    if decision not in {"dispatch", "rerun", "wait"}:
        raise RuntimeError("Redis returned an invalid task dispatch decision")
    return DispatchRequest(decision=decision, token=dispatch_token)


async def publish_dispatch(task_id: str, token: str) -> bool:
    return bool(
        await get_redis().client.eval(
            _PUBLISH_DISPATCH_SCRIPT,
            1,
            meta_key(task_id),
            token,
            META_TTL_SECONDS,
        )
    )


async def abort_dispatch(task_id: str, token: str) -> bool:
    return bool(
        await get_redis().client.eval(
            _ABORT_DISPATCH_SCRIPT,
            3,
            meta_key(task_id),
            rerun_key(task_id),
            cancel_key(task_id),
            token,
            META_TTL_SECONDS,
        )
    )


async def claim_dispatch(
    task_id: str, token: str, claim_id: str
) -> ClaimDecision:
    if not claim_id:
        raise ValueError("claim_id is required for idempotent worker claims")
    result = await get_redis().client.eval(
        _CLAIM_DISPATCH_SCRIPT,
        1,
        meta_key(task_id),
        token,
        claim_id,
        META_TTL_SECONDS,
        WORKER_CLAIM_LEASE_SECONDS * 1000,
    )
    if result not in {"claimed", "busy", "stale"}:
        raise RuntimeError("Redis returned an invalid worker claim decision")
    return result


async def renew_dispatch_claim(
    task_id: str, token: str, claim_id: str
) -> bool:
    return bool(
        await get_redis().client.eval(
            _RENEW_CLAIM_SCRIPT,
            1,
            meta_key(task_id),
            token,
            claim_id,
            META_TTL_SECONDS,
            WORKER_CLAIM_LEASE_SECONDS * 1000,
        )
    )


async def finish_cycle(
    task_id: str,
    token: str,
    claim_id: str,
    cycle_id: str,
    *,
    force_done: bool = False,
) -> FinishDecision:
    if not cycle_id:
        raise ValueError("cycle_id is required for idempotent task completion")
    result = await get_redis().client.eval(
        _FINISH_CYCLE_SCRIPT,
        3,
        meta_key(task_id),
        rerun_key(task_id),
        cancel_key(task_id),
        token,
        claim_id,
        META_TTL_SECONDS,
        "1" if force_done else "0",
        cycle_id,
    )
    if result not in {"continue", "done", "stale"}:
        raise RuntimeError("Redis returned an invalid task completion decision")
    return result


class CeleryTask(Task):
    """Task handle that enqueues agent execution onto Celery workers."""

    _runner_factory: Optional[TaskRunnerFactory] = None

    def __init__(self, task_id: str, params: Optional[Dict[str, Any]] = None):
        self._id = task_id
        self._params = params
        self._input_stream = RedisStreamQueue(f"task:input:{task_id}")
        self._output_stream = RedisStreamQueue(f"task:output:{task_id}")

    @property
    def id(self) -> str:
        """Task ID."""
        return self._id

    def refresh_runner_params(self, params: Dict[str, Any]) -> None:
        """Fill legacy metadata without moving this task to another runtime."""
        previous_params = self._params or {}
        for field in ("session_id", "agent_id", "user_id", "sandbox_id"):
            previous = previous_params.get(field)
            incoming = params.get(field)
            if previous is not None and previous != incoming:
                raise RuntimeError(
                    f"Task {self._id} cannot change bound {field}"
                )
        previous_generation = previous_params.get("task_sandbox_id")
        incoming_generation = params.get("task_sandbox_id")
        if (
            previous_generation is not None
            and previous_generation != incoming_generation
        ):
            raise RuntimeError(
                f"Task {self._id} cannot change sandbox generation"
            )
        self._params = dict(params)

    @property
    def input_stream(self) -> MessageQueue:
        """Input stream."""
        return self._input_stream

    @property
    def output_stream(self) -> MessageQueue:
        """Output stream."""
        return self._output_stream

    async def is_done(self) -> bool:
        """Check if the task is done (from Redis metadata)."""
        meta = await read_meta(self._id)
        if meta is None:
            return True
        return meta.get("status") == STATUS_DONE

    async def run(self) -> None:
        """Enqueue the task onto a Celery worker if it is not already running."""
        if self._params is None:
            meta = await read_meta(self._id)
            if meta:
                self._params = meta.get("params")
        if self._params is None:
            raise RuntimeError(f"Task {self._id} has no runner params to run with")

        # A sender that dies during broker publication leaves a short-lived
        # DISPATCHING lease. Other replicas wait, then safely take over with a
        # new generation token instead of stranding the accepted turn.
        while True:
            request = await request_dispatch(self._id, self._params)
            if request.decision == "rerun":
                return
            if request.decision == "wait":
                await asyncio.sleep(0.05)
                continue
            try:
                celery_app.send_task(
                    AGENT_TASK_NAME,
                    args=[self._id, self._params, request.token],
                    task_id=f"{self._id}:{request.token}",
                )
            except BaseException:
                await abort_dispatch(self._id, request.token)
                raise
            await publish_dispatch(self._id, request.token)
            logger.info("Task %s enqueued to Celery", self._id)
            return

    async def cancel(self) -> bool:
        """Request cancellation of the task.

        The worker that owns the task polls the cancel flag and cancels the
        agent coroutine, which emits a DoneEvent and completes the session.

        Returns:
            bool: True if cancellation was requested, False if already done
        """
        if not await request_cancel(self._id):
            return False
        logger.info(f"Task {self._id} cancellation requested")
        return True

    async def wait_for_done(self, timeout_seconds: float) -> bool:
        """Poll Redis until the worker acknowledges task completion."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_seconds)
        while True:
            if await self.is_done():
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.1, remaining))

    @classmethod
    def set_runner_factory(cls, factory: TaskRunnerFactory) -> None:
        """Register the factory used by workers to rebuild task runners."""
        cls._runner_factory = factory

    @classmethod
    def get_runner_factory(cls) -> TaskRunnerFactory:
        if cls._runner_factory is None:
            raise RuntimeError("No TaskRunnerFactory registered for CeleryTask")
        return cls._runner_factory

    @classmethod
    async def get(cls, task_id: str) -> Optional["CeleryTask"]:
        """Get a task handle by its ID (from Redis metadata).

        Returns:
            Optional[CeleryTask]: Task handle if the task exists, None otherwise
        """
        meta = await read_meta(task_id)
        if meta is None:
            return None
        return cls(task_id, params=meta.get("params"))

    @classmethod
    def create(cls, params: Dict[str, Any]) -> "CeleryTask":
        """Create a new task handle from serializable runner parameters."""
        return cls(str(uuid.uuid4()), params=params)

    @classmethod
    def recover(cls, task_id: str, params: Dict[str, Any]) -> "CeleryTask":
        """Recover a handle even if volatile Redis task metadata expired."""
        return cls(task_id, params=params)

    @classmethod
    async def destroy(cls) -> None:
        """Destroy all task instances.

        Tasks are owned by Celery workers, not the API process, so API
        shutdown intentionally leaves them running.
        """
        logger.info("CeleryTask.destroy: tasks are owned by workers, nothing to do")

    def __repr__(self) -> str:
        return f"CeleryTask(id={self._id})"
