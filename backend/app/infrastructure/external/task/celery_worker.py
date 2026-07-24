"""Celery worker-side execution of agent tasks.

Each worker process lazily initializes its own event loop, MongoDB/Beanie and
Redis connections, and the AgentTaskRunner factory. The agent coroutine is
supervised by a cancel watcher that polls the Redis cancellation flag set by
``CeleryTask.cancel()`` from any API replica.
"""
import asyncio
import logging
import uuid
from typing import Any, Dict, Optional

from beanie import init_beanie
from celery.exceptions import Reject
from celery.signals import worker_process_shutdown

from app.core.config import get_settings
from app.domain.utils.error_reporting import safe_exception_summary
from app.domain.repositories.turn_submission_repository import TurnSubmissionRepository
from app.infrastructure.external.task.celery_app import celery_app, AGENT_TASK_NAME
from app.infrastructure.external.task.celery_task import (
    CeleryTask,
    is_cancel_requested,
    claim_dispatch,
    finish_cycle,
    renew_dispatch_claim,
    acquire_execution_lease,
    renew_execution_lease,
    release_execution_lease,
)

logger = logging.getLogger(__name__)

CANCEL_POLL_INTERVAL_SECONDS = 1.0
CLAIM_RENEW_INTERVAL_SECONDS = 10.0

_loop: Optional[asyncio.AbstractEventLoop] = None
_initialized = False
_turn_submission_repository: Optional[TurnSubmissionRepository] = None

_FACTORY_FAILURE_MESSAGE = "Agent worker could not initialize before execution"


@worker_process_shutdown.connect
def _drain_artifact_tasks_before_worker_exit(**_: Any) -> None:
    """Give committed uploads a bounded publish/compensation grace period."""
    loop = _loop
    if loop is None or loop.is_closed():
        return
    from app.domain.services.agent_task_runner import (
        begin_artifact_enrichment_shutdown,
        drain_artifact_enrichment_tasks,
    )
    # Worker-process shutdown is terminal. Do not reopen this loop's gate;
    # tests that deliberately reuse the loop can call the explicit end hook.
    begin_artifact_enrichment_shutdown(loop)
    if loop.is_running():
        logger.warning(
            "Celery event loop is still running during worker shutdown; "
            "artifact admission is closed but synchronous drain is skipped"
        )
        return
    try:
        loop.run_until_complete(drain_artifact_enrichment_tasks(10.0))
    except Exception as exc:
        logger.error(
            "Celery artifact enrichment shutdown drain failed: %s",
            safe_exception_summary(exc),
        )


class WorkerStateRetry(RuntimeError):
    """Ask Celery to retry one explicit, idempotent worker phase."""

    def __init__(
        self,
        mode: str,
        cycle_id: str = "",
        claim_id: str = "",
    ):
        super().__init__("Agent worker state transition is temporarily unavailable")
        self.mode = mode
        self.cycle_id = cycle_id
        self.claim_id = claim_id


def _get_loop() -> asyncio.AbstractEventLoop:
    """Get the per-worker-process event loop, creating it lazily after fork."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def _build_runner_factory(
    turn_submission_repository: Optional[TurnSubmissionRepository] = None,
):
    """Composition root for the worker process (mirrors interfaces/dependencies.py)."""
    from app.domain.services.agent_task_runner import AgentTaskRunnerFactory
    from app.infrastructure.external.sandbox import get_sandbox_provider
    from app.infrastructure.external.file.gridfsfile import get_file_storage
    from app.infrastructure.external.search import get_search_engine
    from app.infrastructure.external.llm import get_llm_factory
    from app.infrastructure.repositories.mongo_agent_repository import MongoAgentRepository
    from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
    from app.infrastructure.repositories.mongo_turn_submission_repository import MongoTurnSubmissionRepository
    from app.infrastructure.repositories.file_mcp_repository import FileMCPRepository

    repository = turn_submission_repository or MongoTurnSubmissionRepository()
    return AgentTaskRunnerFactory(
        agent_repository=MongoAgentRepository(),
        session_repository=MongoSessionRepository(),
        turn_submission_repository=repository,
        sandbox_cls=get_sandbox_provider(),
        file_storage=get_file_storage(),
        mcp_repository=FileMCPRepository(),
        llm_factory=get_llm_factory(),
        search_engine=get_search_engine(),
    )


async def _ensure_initialized() -> None:
    """Initialize MongoDB/Beanie, Redis and the runner factory once per process."""
    global _initialized, _turn_submission_repository
    if _initialized:
        return

    from app.infrastructure.storage.mongodb import get_mongodb
    from app.infrastructure.storage.redis import get_redis
    from app.infrastructure.models.documents import (
        AgentDocument,
        SessionDocument,
        UserDocument,
        ClawDocument,
        TurnSubmissionDocument,
        TurnQuotaDocument,
        TurnOutputEventDocument,
    )

    settings = get_settings()
    await get_mongodb().initialize()
    await init_beanie(
        database=get_mongodb().client[settings.mongodb_database],
        document_models=[
            AgentDocument,
            SessionDocument,
            UserDocument,
            ClawDocument,
            TurnSubmissionDocument,
            TurnQuotaDocument,
            TurnOutputEventDocument,
        ],
    )
    await get_redis().initialize()
    from app.infrastructure.repositories.mongo_turn_submission_repository import (
        MongoTurnSubmissionRepository,
    )

    _turn_submission_repository = MongoTurnSubmissionRepository()
    CeleryTask.set_runner_factory(
        _build_runner_factory(_turn_submission_repository)
    )
    _initialized = True
    logger.info("Celery worker process initialized")


async def _watch_cancel(
    task_id: str,
    claim_lost: asyncio.Event,
    agent_task: asyncio.Task,
) -> str | None:
    """Cancel active execution when lifecycle or claim control is lost."""
    while not agent_task.done():
        if claim_lost.is_set():
            agent_task.cancel("claim_lost")
            return "claim_lost"
        try:
            cancel_requested = await is_cancel_requested(task_id)
        except Exception as exc:
            logger.error(
                "Task %s cancellation state is unavailable: %s",
                task_id,
                safe_exception_summary(exc),
            )
            claim_lost.set()
            agent_task.cancel("claim_lost")
            return "claim_lost"
        if cancel_requested:
            logger.info(f"Task {task_id} cancel flag detected, cancelling agent coroutine")
            agent_task.cancel("cancel_requested")
            return "cancel"
        await asyncio.sleep(CANCEL_POLL_INTERVAL_SECONDS)
    return None


async def _heartbeat_claim(
    task_id: str,
    dispatch_token: str,
    claim_id: str,
    execution_owner_id: str,
    claim_lost: asyncio.Event,
) -> None:
    """Renew ownership across factory, runner, cleanup and Mongo phases."""
    while not claim_lost.is_set():
        await asyncio.sleep(CLAIM_RENEW_INTERVAL_SECONDS)
        try:
            execution_renewed = await renew_execution_lease(
                task_id, execution_owner_id
            )
        except Exception as exc:
            logger.error(
                "Task %s execution lease renewal is unavailable: %s",
                task_id,
                safe_exception_summary(exc),
            )
            execution_renewed = False
        if not execution_renewed:
            claim_lost.set()
            return
        try:
            claim_renewed = await renew_dispatch_claim(
                task_id, dispatch_token, claim_id
            )
        except Exception as exc:
            logger.error(
                "Task %s worker claim renewal is unavailable: %s",
                task_id,
                safe_exception_summary(exc),
            )
            claim_renewed = False
        if not claim_renewed:
            claim_lost.set()
            return


async def _close_runner(task_id: str, runner, task_handle: CeleryTask) -> None:
    """Finalize one runner generation before exposing task completion."""
    try:
        await runner.on_done(task_handle)
    except Exception as exc:
        logger.error(
            "Task %s on_done callback failed: %s",
            task_id,
            safe_exception_summary(exc),
        )
    finally:
        close = getattr(runner, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception as exc:
                logger.error(
                    "Task %s runner handle cleanup failed: %s",
                    task_id,
                    safe_exception_summary(exc),
                )


async def _terminalize_factory_failure(
    task_id: str,
    params: Dict[str, Any],
    dispatch_token: str,
    claim_id: str,
    cycle_id: str,
) -> bool:
    repository = _turn_submission_repository
    session_id = str(params.get("session_id") or "")
    user_id = str(params.get("user_id") or "")
    if repository is None or not session_id or not user_id:
        raise WorkerStateRetry("factory", cycle_id, claim_id)
    try:
        safe_to_finish = await repository.recover_factory_failure_for_task(
            session_id,
            user_id=user_id,
            task_id=task_id,
            error=_FACTORY_FAILURE_MESSAGE,
        )
    except Exception as exc:
        logger.error(
            "Task %s could not terminalize unstarted turns: %s",
            task_id,
            safe_exception_summary(exc),
        )
        raise WorkerStateRetry("factory", cycle_id, claim_id) from None
    if not safe_to_finish:
        # A previous runner still owns a Mongo execution lease.  Reuse this
        # Redis claim and retry construction until that owner finishes or its
        # lease can be fenced as failed_unknown; never expose DONE early.
        raise WorkerStateRetry("factory", cycle_id, claim_id)
    # Do not force completion here. A newer turn may have been enqueued after
    # the Mongo scan and advertised itself through the atomic rerun marker. In
    # that case this worker must take another generation (or fail that turn on
    # the next exact scan), never erase the marker and leave it stranded.
    return await _resume_completion(
        task_id,
        dispatch_token,
        claim_id,
        cycle_id,
        force_done=False,
    )


async def _resume_completion(
    task_id: str,
    dispatch_token: str,
    claim_id: str,
    cycle_id: str,
    *,
    force_done: bool,
) -> bool:
    try:
        decision = await finish_cycle(
            task_id,
            dispatch_token,
            claim_id,
            cycle_id,
            force_done=force_done,
        )
    except Exception as exc:
        logger.error(
            "Task %s completion acknowledgement is unavailable: %s",
            task_id,
            safe_exception_summary(exc),
        )
        raise WorkerStateRetry(
            "finish_force" if force_done else "finish",
            cycle_id,
            claim_id,
        ) from None
    return decision == "continue"


async def _run_agent(
    task_id: str,
    params: Dict[str, Any],
    dispatch_token: str = "",
    claim_id: str = "",
    retry_mode: str = "claim",
    retry_cycle_id: str = "",
) -> None:
    effective_claim_id = claim_id or str(uuid.uuid4())
    try:
        await _ensure_initialized()
    except Exception as exc:
        logger.error(
            "Task %s worker initialization is unavailable: %s",
            task_id,
            safe_exception_summary(exc),
        )
        raise WorkerStateRetry(
            retry_mode, retry_cycle_id, effective_claim_id
        ) from None

    try:
        claim_decision = await claim_dispatch(
            task_id, dispatch_token, effective_claim_id
        )
    except Exception as exc:
        logger.error(
            "Task %s dispatch claim is unavailable: %s",
            task_id,
            safe_exception_summary(exc),
        )
        raise WorkerStateRetry(
            "claim", claim_id=effective_claim_id
        ) from None
    if claim_decision == "busy":
        raise WorkerStateRetry("claim", claim_id=effective_claim_id)
    if claim_decision == "stale":
        logger.info("Ignoring stale delivery for task %s", task_id)
        return

    execution_owner_id = str(uuid.uuid4())
    try:
        execution_acquired = await acquire_execution_lease(
            task_id, execution_owner_id
        )
    except Exception as exc:
        logger.error(
            "Task %s execution lease is unavailable: %s",
            task_id,
            safe_exception_summary(exc),
        )
        raise WorkerStateRetry(
            "claim", claim_id=effective_claim_id
        ) from None
    if not execution_acquired:
        raise WorkerStateRetry("claim", claim_id=effective_claim_id)

    claim_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat_claim(
            task_id,
            dispatch_token,
            effective_claim_id,
            execution_owner_id,
            claim_lost,
        )
    )
    task_handle = CeleryTask(task_id, params=params)
    try:
        resume_cycle_id = retry_cycle_id or str(uuid.uuid4())
        if retry_mode == "factory":
            should_continue = await _terminalize_factory_failure(
                task_id,
                params,
                dispatch_token,
                effective_claim_id,
                resume_cycle_id,
            )
            if not should_continue:
                return
        elif retry_mode in {"finish", "finish_force"}:
            should_continue = await _resume_completion(
                task_id,
                dispatch_token,
                effective_claim_id,
                resume_cycle_id,
                force_done=retry_mode == "finish_force",
            )
            if not should_continue:
                return

        while True:
            if claim_lost.is_set():
                raise WorkerStateRetry(
                    "claim", claim_id=effective_claim_id
                )
            cycle_id = str(uuid.uuid4())
            runner = None
            try:
                runner = await CeleryTask.get_runner_factory().create_runner(
                    {**params, "task_id": task_id}
                )
            except Exception as exc:
                if claim_lost.is_set():
                    raise WorkerStateRetry(
                        "claim", claim_id=effective_claim_id
                    ) from None
                from app.domain.services.agent_task_runner import (
                    RunnerCleanupCapacityError,
                )
                if isinstance(exc, RunnerCleanupCapacityError):
                    logger.warning(
                        "Task %s runner cleanup capacity is exhausted; "
                        "retrying without terminalizing its durable turn",
                        task_id,
                    )
                    raise WorkerStateRetry(
                        "run", claim_id=effective_claim_id
                    ) from None
                logger.error(
                    "Task %s runner construction failed: %s",
                    task_id,
                    safe_exception_summary(exc),
                )
                should_continue = await _terminalize_factory_failure(
                    task_id,
                    params,
                    dispatch_token,
                    effective_claim_id,
                    cycle_id,
                )
                if should_continue:
                    continue
                return

            if claim_lost.is_set():
                await _close_runner(task_id, runner, task_handle)
                raise WorkerStateRetry(
                    "claim", claim_id=effective_claim_id
                )

            watcher = None
            run_error: Optional[BaseException] = None
            cancelled = False
            cancellation_reason = ""
            control_reason: str | None = None
            try:
                agent_task = asyncio.ensure_future(runner.run(task_handle))
                watcher = asyncio.ensure_future(
                    _watch_cancel(task_id, claim_lost, agent_task)
                )
                await agent_task
            except asyncio.CancelledError as exc:
                cancelled = True
                cancellation_reason = str(exc.args[0]) if exc.args else ""
                logger.info("Task %s agent coroutine cancelled", task_id)
            except BaseException as exc:
                run_error = exc
            finally:
                if watcher is not None:
                    if not watcher.done():
                        watcher.cancel()
                    watcher_result = await asyncio.gather(
                        watcher, return_exceptions=True
                    )
                    if watcher_result and isinstance(watcher_result[0], str):
                        control_reason = watcher_result[0]
                await _close_runner(task_id, runner, task_handle)

            if control_reason == "claim_lost" or claim_lost.is_set():
                raise WorkerStateRetry(
                    "claim", claim_id=effective_claim_id
                )
            if cancellation_reason == "mongo_claim_lost":
                raise WorkerStateRetry(
                    "run", claim_id=effective_claim_id
                )

            if run_error is not None:
                # AgentTaskRunner persists and ACKs ordinary generation
                # failures itself.  Anything escaping runner.run is therefore
                # an infrastructure/unknown-state failure (for example Redis
                # read or Mongo terminal-write loss).  Completing the Redis
                # lifecycle here would orphan ENQUEUED/RUNNING durable work.
                logger.error(
                    "Task %s runner failed before durable completion; "
                    "rebuilding with the same claim: %s",
                    task_id,
                    safe_exception_summary(run_error),
                )
                raise WorkerStateRetry(
                    "run", claim_id=effective_claim_id
                ) from None

            # Cancellation deliberately retires the task and lifecycle cleanup
            # cancels any queued turns. An unexpected generation error must still
            # preserve a concurrently requested rerun, otherwise a newer durable
            # turn can be left ENQUEUED with no worker.
            force_done = cancelled
            should_continue = await _resume_completion(
                task_id,
                dispatch_token,
                effective_claim_id,
                cycle_id,
                force_done=force_done,
            )
            if cancelled or not should_continue:
                return
    finally:
        claim_lost.set()
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        try:
            await release_execution_lease(task_id, execution_owner_id)
        except Exception as exc:
            # The owner-tagged lease expires on its own; never let cleanup
            # mask the durable retry/completion decision.
            logger.error(
                "Task %s execution lease release is unavailable: %s",
                task_id,
                safe_exception_summary(exc),
            )


@celery_app.task(bind=True, name=AGENT_TASK_NAME, max_retries=None)
def run_agent_task(
    self,
    task_id: str,
    params: Dict[str, Any],
    dispatch_token: str = "",
    claim_id: str = "",
    retry_mode: str = "claim",
    retry_cycle_id: str = "",
) -> None:
    """Celery entry point: run one agent task to completion in this process."""
    logger.info(f"Worker picked up agent task {task_id}")
    loop = _get_loop()
    try:
        loop.run_until_complete(
            _run_agent(
                task_id,
                params,
                dispatch_token,
                claim_id,
                retry_mode,
                retry_cycle_id,
            )
        )
    except WorkerStateRetry as exc:
        try:
            raise self.retry(
                args=[
                    task_id,
                    params,
                    dispatch_token,
                    exc.claim_id,
                    exc.mode,
                    exc.cycle_id,
                ],
                exc=exc,
                countdown=1,
                max_retries=None,
            )
        except Reject as publish_failure:
            # Task.retry publishes a replacement before acknowledging this
            # late-acked delivery. If broker publication itself fails, Celery
            # normally Rejects with requeue=False, which would strand the
            # durable Redis/Mongo state. Requeue the original delivery; its
            # renewable claim protocol will safely wait or take over.
            raise Reject(publish_failure, requeue=True) from None
    logger.info(f"Worker finished agent task {task_id}")
