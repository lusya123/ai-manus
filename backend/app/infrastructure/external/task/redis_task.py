import asyncio
import uuid
import logging
from typing import Any, Dict, Optional

from app.domain.external.task import (
    RunnerCleanupCapacityError,
    Task,
    TaskRunner,
    TaskRunnerFactory,
)
from app.domain.utils.error_reporting import safe_exception_summary
from app.infrastructure.external.message_queue.redis_stream_queue import RedisStreamQueue, MessageQueue

logger = logging.getLogger(__name__)


class RedisStreamTask(Task):
    """In-process task implementation backed by Redis Streams for I/O.

    The task runs as an asyncio task inside the current process; only the
    input/output streams live in Redis. The task registry is process-local.
    """
    
    _task_registry: Dict[str, 'RedisStreamTask'] = {}
    _runner_factory: Optional[TaskRunnerFactory] = None
    _DESTROY_CANCEL_TIMEOUT_SECONDS = 15.0
    _RUNNER_CAPACITY_RETRY_INITIAL_SECONDS = 0.05
    _RUNNER_CAPACITY_RETRY_MAX_SECONDS = 1.0
    _GENERIC_FACTORY_RETRIES_BEFORE_RECOVERY = 3
    _RETRYABLE_CONTROL_CANCELLATIONS = frozenset(
        {"claim_lost", "mongo_claim_lost"}
    )
    
    def __init__(self, params: Dict[str, Any], *, task_id: Optional[str] = None):
        """Initialize Redis Stream task with serializable runner parameters.
        
        Args:
            params: JSON-serializable parameters used by the registered
                TaskRunnerFactory to rebuild the runner when the task runs
        """
        self._params = params
        self._runner: Optional[TaskRunner] = None
        self._runner_closed = False
        self._id = task_id or str(uuid.uuid4())
        # Runner construction is part of the task lifecycle too.  Keep it in
        # a separate, shared task so concurrent cold-start run() calls await
        # one factory operation and cancel()/wait_for_done() can observe it.
        self._startup_task: Optional[asyncio.Task] = None
        self._execution_task: Optional[asyncio.Task] = None
        # A second message can be submitted while the current runner is just
        # about to observe an empty input stream and return.  Remember that
        # run request so the execution coroutine rechecks the queue instead
        # of leaving the new message stranded until another HTTP request.
        self._rerun_requested = False
        self._cancel_requested = False
        
        # Create input/output streams based on task ID
        input_stream_name = f"task:input:{self._id}"
        output_stream_name = f"task:output:{self._id}"
        self._input_stream = RedisStreamQueue(input_stream_name)
        self._output_stream = RedisStreamQueue(output_stream_name)
        
        # Register task instance
        RedisStreamTask._task_registry[self._id] = self
        
    @property
    def id(self) -> str:
        """Task ID."""
        return self._id

    def refresh_runner_params(self, params: Dict[str, Any]) -> None:
        """Fill legacy metadata without moving this task to another runtime."""
        for field in ("session_id", "agent_id", "user_id", "sandbox_id"):
            previous = self._params.get(field)
            incoming = params.get(field)
            if previous is not None and previous != incoming:
                raise RuntimeError(
                    f"Task {self._id} cannot change bound {field}"
                )
        previous_generation = self._params.get("task_sandbox_id")
        incoming_generation = params.get("task_sandbox_id")
        if (
            previous_generation is not None
            and previous_generation != incoming_generation
        ):
            raise RuntimeError(
                f"Task {self._id} cannot change sandbox generation"
            )
        self._params = dict(params)

    def _bound_runner_params(self) -> Dict[str, Any]:
        return {**self._params, "task_id": self._id}
    
    @property
    def _done(self) -> bool:
        startup_task = getattr(self, "_startup_task", None)
        if startup_task is not None and not startup_task.done():
            return False
        execution_task = getattr(self, "_execution_task", None)
        return execution_task is None or execution_task.done()
    
    async def is_done(self) -> bool:
        """Check if the task is done.

        Returns:
            bool: True if the task is done, False otherwise
        """
        return self._done
    
    async def run(self) -> None:
        """Run the task using the runner built by the registered factory."""
        # A caller may retain an object just before its completed execution
        # removes it from the registry.  If recover() has since installed a
        # replacement for the same durable stream ID, that incumbent is the
        # sole lifecycle owner.  Delegate before inspecting or publishing any
        # local startup state so the stale object cannot overwrite the new
        # registry entry and create a second runner (registry ABA).
        incumbent = RedisStreamTask._task_registry.get(self._id)
        if incumbent is not None and incumbent is not self:
            await incumbent.run()
            return

        # There is no await before a new startup task is assigned.  Event-loop
        # scheduling therefore makes the check-and-publish single-flight:
        # every concurrent caller that arrives during construction observes
        # and awaits this exact task instead of invoking the factory again.
        startup_task = getattr(self, "_startup_task", None)
        if startup_task is not None and not startup_task.done():
            await asyncio.shield(startup_task)
            return

        if not self._done:
            if not self._cancel_requested:
                self._rerun_requested = True
            return

        self._rerun_requested = False
        self._cancel_requested = False
        # Completed executions remove themselves from the registry.  A new
        # turn can legitimately reuse the same task object obtained just
        # before that cleanup, so register it again before restarting.
        RedisStreamTask._task_registry[self._id] = self
        startup_task = asyncio.create_task(self._start_execution())
        self._startup_task = startup_task
        # Shield the shared startup from cancellation of one HTTP caller.  An
        # explicit task.cancel() still cancels it directly below.
        await asyncio.shield(startup_task)

    async def _close_unstarted_runner(self) -> None:
        """Close a runner built after cancellation but never executed."""
        runner = self._runner
        close = getattr(runner, "aclose", None) if runner else None
        try:
            if callable(close):
                await close()
        except Exception as exc:
            logger.error(
                "Task %s unstarted runner cleanup failed: %s",
                self._id,
                safe_exception_summary(exc),
            )
        finally:
            self._runner_closed = True

    async def _start_execution(self) -> None:
        """Build one runner and publish its execution task atomically."""
        try:
            if self._runner is None or getattr(self, "_runner_closed", False):
                factory = RedisStreamTask._runner_factory
                if factory is None:
                    raise RuntimeError(
                        "No TaskRunnerFactory registered for RedisStreamTask"
                    )
                self._runner = await factory.create_runner(
                    self._bound_runner_params()
                )
                self._runner_closed = False

            # A defensive factory may finish constructing and return a runner
            # while suppressing CancelledError in order to clean up.  Honour
            # the explicit request before exposing an execution coroutine and
            # release every handle/cleanup lease owned by that runner.
            if getattr(self, "_cancel_requested", False):
                await self._close_unstarted_runner()
                self._finish_explicit_cancellation()
                return

            self._execution_task = asyncio.create_task(self._execute_task())
            logger.info(f"Task {self._id} execution started")
        except asyncio.CancelledError:
            # AgentTaskRunnerFactory owns cleanup for cancellation raised
            # during construction.  If a custom factory returned a runner,
            # the branch above closes it before reaching this handler.
            if getattr(self, "_cancel_requested", False):
                self._finish_explicit_cancellation()
            else:
                self._cleanup_registry()
            raise
    
    async def cancel(self) -> bool:
        """Cancel the task.

        Returns:
            bool: True if the task is cancelled, False otherwise
        """
        startup_task = getattr(self, "_startup_task", None)
        if startup_task is not None and not startup_task.done():
            if getattr(self, "_cancel_requested", False):
                # Do not inject a second CancelledError while the factory is
                # releasing partially constructed handles/leases.
                return True
            self._cancel_requested = True
            self._rerun_requested = False
            startup_task.cancel()
            logger.info(f"Task {self._id} startup cancelled")
            return True

        if not self._done:
            if getattr(self, "_cancel_requested", False):
                return True
            self._cancel_requested = True
            self._rerun_requested = False
            self._execution_task.cancel()
            logger.info(f"Task {self._id} cancelled")
            return True
        
        self._cleanup_registry()
        return False

    async def wait_for_done(self, timeout_seconds: float) -> bool:
        """Wait until the in-process coroutine has finished cancelling.

        ``cancel()`` only schedules ``CancelledError`` delivery.  Keeping the
        task registered until this coroutine exits also prevents another API
        request from mistaking a cancelling task for a missing one.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_seconds)
        while True:
            startup_task = getattr(self, "_startup_task", None)
            execution_task = getattr(self, "_execution_task", None)
            pending_task = (
                startup_task
                if startup_task is not None and not startup_task.done()
                else execution_task
                if execution_task is not None and not execution_task.done()
                else None
            )
            if pending_task is None:
                return True
            try:
                await asyncio.wait_for(
                    asyncio.shield(pending_task),
                    timeout=max(0.0, deadline - loop.time()),
                )
            except asyncio.CancelledError:
                # A normally acknowledged cancellation leaves startup or
                # execution in a cancelled state.  Preserve cancellation of
                # this waiter itself, then loop because successful startup may
                # have published a still-live execution task.
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
                if not pending_task.done():
                    raise
            except TimeoutError:
                return False
    
    @property
    def input_stream(self) -> MessageQueue:
        """Input stream."""
        return self._input_stream
    
    @property
    def output_stream(self) -> MessageQueue:
        """Output stream."""
        return self._output_stream
    
    async def _finalize_runner(self) -> None:
        """Await runner callbacks and non-destructive handle cleanup."""
        if not self._runner:
            return
        try:
            await self._runner.on_done(self)
        except Exception as exc:
            logger.error(
                "Task %s runner on_done callback failed: %s",
                self._id,
                safe_exception_summary(exc),
            )
        finally:
            close = getattr(self._runner, "aclose", None)
            try:
                if callable(close):
                    await close()
            except Exception as exc:
                logger.error(
                    "Task %s runner handle cleanup failed: %s",
                    self._id,
                    safe_exception_summary(exc),
                )
            finally:
                self._runner_closed = True
    
    def _cleanup_registry(self) -> None:
        """Remove this task from the registry."""
        if RedisStreamTask._task_registry.get(self._id) is self:
            RedisStreamTask._task_registry.pop(self._id, None)
            logger.info(f"Task {self._id} removed from registry")

    @staticmethod
    def _consume_internal_cancellation() -> None:
        """Clear cancellation used as an infrastructure retry signal."""
        current = asyncio.current_task()
        uncancel = getattr(current, "uncancel", None)
        if current is None or not callable(uncancel):
            return
        while current.cancelling():
            uncancel()

    @classmethod
    def _is_retryable_control_cancellation(
        cls,
        error: asyncio.CancelledError,
    ) -> bool:
        reason = str(error.args[0]) if error.args else ""
        return reason in cls._RETRYABLE_CONTROL_CANCELLATIONS

    def _finish_explicit_cancellation(self) -> None:
        self._cancel_requested = False
        self._rerun_requested = False
        self._cleanup_registry()

    async def _wait_before_runner_retry(
        self,
        delay: float,
        *,
        reason: str,
    ) -> bool:
        """Wait without letting a user cancellation strand the task."""
        try:
            await asyncio.sleep(max(0.0, delay))
        except asyncio.CancelledError as exc:
            if self._cancel_requested:
                logger.info(
                    "Task %s %s wait cancelled by user request",
                    self._id,
                    reason,
                )
                self._finish_explicit_cancellation()
                return False
            cancellation_reason = str(exc.args[0]) if exc.args else "unknown"
            if not self._is_retryable_control_cancellation(exc):
                logger.info(
                    "Task %s %s wait cancelled by runtime shutdown "
                    "(reason=%s)",
                    self._id,
                    reason,
                    cancellation_reason,
                )
                self._finish_explicit_cancellation()
                return False
            self._consume_internal_cancellation()
            logger.warning(
                "Task %s %s wait received an infrastructure cancellation; "
                "continuing durable recovery (reason=%s)",
                self._id,
                reason,
                cancellation_reason,
            )
        return True

    async def _rebuild_runner_until_resolved(self) -> bool:
        """Rebuild a local runner or durably resolve work before returning.

        A rerun request has already been committed to Redis and Mongo before
        this method is reached.  Returning ``False`` is therefore permitted
        only after explicit cancellation or after the factory's durable
        recovery hook confirms that no live turn remains.
        """
        retry_delay = self._RUNNER_CAPACITY_RETRY_INITIAL_SECONDS
        capacity_retries = 0
        generic_failures = 0
        stable_recovery_error: Optional[str] = None
        while True:
            factory = RedisStreamTask._runner_factory
            if factory is None:
                generic_failures += 1
                if generic_failures & (generic_failures - 1) == 0:
                    logger.error(
                        "Task %s has no runner factory while durable work "
                        "remains queued (attempt=%s)",
                        self._id,
                        generic_failures,
                    )
                if not await self._wait_before_runner_retry(
                    retry_delay,
                    reason="missing runner factory",
                ):
                    return False
                retry_delay = min(
                    retry_delay * 2,
                    self._RUNNER_CAPACITY_RETRY_MAX_SECONDS,
                )
                continue

            try:
                runner = await factory.create_runner(
                    self._bound_runner_params()
                )
            except RunnerCleanupCapacityError:
                # Cleanup admission fails before any new handles are created.
                # The queued turn must retain this execution task as its
                # worker until an existing cleanup lease is released.
                capacity_retries += 1
                if capacity_retries & (capacity_retries - 1) == 0:
                    logger.warning(
                        "Task %s runner cleanup capacity is exhausted; "
                        "waiting to rebuild durable work (attempt=%s)",
                        self._id,
                        capacity_retries,
                    )
                if not await self._wait_before_runner_retry(
                    retry_delay,
                    reason="cleanup-capacity recovery",
                ):
                    return False
                retry_delay = min(
                    retry_delay * 2,
                    self._RUNNER_CAPACITY_RETRY_MAX_SECONDS,
                )
                continue
            except asyncio.CancelledError as exc:
                if self._cancel_requested:
                    logger.info(
                        "Task %s runner rebuild cancelled by user request",
                        self._id,
                    )
                    self._finish_explicit_cancellation()
                    return False
                cancellation_reason = str(exc.args[0]) if exc.args else "unknown"
                if not self._is_retryable_control_cancellation(exc):
                    logger.info(
                        "Task %s runner rebuild cancelled by runtime "
                        "shutdown (reason=%s)",
                        self._id,
                        cancellation_reason,
                    )
                    self._finish_explicit_cancellation()
                    return False
                self._consume_internal_cancellation()
                logger.warning(
                    "Task %s runner rebuild received an infrastructure "
                    "cancellation; retrying (reason=%s)",
                    self._id,
                    cancellation_reason,
                )
                if not await self._wait_before_runner_retry(
                    retry_delay,
                    reason="runner rebuild",
                ):
                    return False
                retry_delay = min(
                    retry_delay * 2,
                    self._RUNNER_CAPACITY_RETRY_MAX_SECONDS,
                )
                continue
            except Exception as exc:
                generic_failures += 1
                summary = safe_exception_summary(exc)
                if stable_recovery_error is None:
                    # Mongo recovery may commit FAILED and then lose its reply
                    # while repairing quota/outbox postconditions. Reuse the
                    # first exact error marker so the retry selects and
                    # repairs that same terminal row even if later factory
                    # exceptions have a different HTTP status/errno.
                    stable_recovery_error = (
                        f"Runner factory failed: {summary}"
                    )
                if generic_failures & (generic_failures - 1) == 0:
                    logger.error(
                        "Task %s runner rebuild failed; retrying without "
                        "orphaning durable work (attempt=%s error=%s)",
                        self._id,
                        generic_failures,
                        summary,
                    )

                recover = getattr(factory, "recover_factory_failure", None)
                if (
                    generic_failures
                    >= self._GENERIC_FACTORY_RETRIES_BEFORE_RECOVERY
                    and callable(recover)
                ):
                    try:
                        resolved = await recover(
                            self._params,
                            task_id=self._id,
                            error=stable_recovery_error,
                        )
                    except asyncio.CancelledError as recovery_cancel:
                        if self._cancel_requested:
                            self._finish_explicit_cancellation()
                            return False
                        if not self._is_retryable_control_cancellation(
                            recovery_cancel
                        ):
                            self._finish_explicit_cancellation()
                            return False
                        self._consume_internal_cancellation()
                        resolved = False
                    except Exception as recovery_exc:
                        logger.error(
                            "Task %s durable factory-failure recovery failed; "
                            "will retry (error=%s)",
                            self._id,
                            safe_exception_summary(recovery_exc),
                        )
                        resolved = False
                    if resolved:
                        # A concurrent run() may have queued work after the
                        # recovery query. In that case rebuild once more so it
                        # cannot fall into the completion boundary.
                        if self._rerun_requested:
                            self._rerun_requested = False
                            generic_failures = 0
                            stable_recovery_error = None
                            retry_delay = (
                                self._RUNNER_CAPACITY_RETRY_INITIAL_SECONDS
                            )
                            continue
                        self._cleanup_registry()
                        return False

                if not await self._wait_before_runner_retry(
                    retry_delay,
                    reason="runner-factory recovery",
                ):
                    return False
                retry_delay = min(
                    retry_delay * 2,
                    self._RUNNER_CAPACITY_RETRY_MAX_SECONDS,
                )
                continue

            self._runner = runner
            self._runner_closed = False
            return True
    
    async def _execute_task(self):
        """Execute every requested runner generation without a tail gap.

        ``run()`` can arrive while ``on_done``/``aclose`` is awaiting.  The
        final rerun check therefore happens after those awaits, and registry
        removal immediately follows a negative check with no intervening await.
        """
        execution_retry_delay = self._RUNNER_CAPACITY_RETRY_INITIAL_SECONDS
        while True:
            execution_error: Optional[BaseException] = None
            try:
                while True:
                    await self._runner.run(self)
                    execution_retry_delay = (
                        self._RUNNER_CAPACITY_RETRY_INITIAL_SECONDS
                    )
                    if self._cancel_requested or not self._rerun_requested:
                        break
                    self._rerun_requested = False
                    logger.debug(
                        "Task %s received another run request while active; "
                        "rechecking its input stream",
                        self._id,
                    )
            except asyncio.CancelledError as exc:
                if self._cancel_requested:
                    logger.info(
                        "Task %s execution cancelled by user request",
                        self._id,
                    )
                elif self._is_retryable_control_cancellation(exc):
                    # AgentTaskRunner uses cancellation as a fencing signal
                    # when its Mongo claim renewal is lost. Celery retries that
                    # exact state; local execution must do the same instead of
                    # confusing it with an explicit stop request.
                    self._consume_internal_cancellation()
                    execution_error = exc
                    cancellation_reason = (
                        str(exc.args[0]) if exc.args else "unknown"
                    )
                    logger.error(
                        "Task %s execution lost infrastructure ownership; "
                        "rebuilding durable worker (reason=%s)",
                        self._id,
                        cancellation_reason,
                    )
                else:
                    cancellation_reason = (
                        str(exc.args[0]) if exc.args else "runtime shutdown"
                    )
                    logger.info(
                        "Task %s execution cancelled outside durable control; "
                        "stopping local worker (reason=%s)",
                        self._id,
                        cancellation_reason,
                    )
                    self._cancel_requested = True
            except Exception as exc:
                execution_error = exc
                logger.error(
                    "Task %s execution failed before durable completion; "
                    "rebuilding worker: %s",
                    self._id,
                    safe_exception_summary(exc),
                )

            try:
                await self._finalize_runner()
            except asyncio.CancelledError as exc:
                if self._cancel_requested:
                    logger.info(
                        "Task %s runner finalization cancelled by user request",
                        self._id,
                    )
                elif self._is_retryable_control_cancellation(exc):
                    self._consume_internal_cancellation()
                    execution_error = execution_error or exc
                    logger.error(
                        "Task %s runner finalization was interrupted; "
                        "rebuilding durable worker",
                        self._id,
                    )
                else:
                    logger.info(
                        "Task %s runner finalization cancelled by runtime "
                        "shutdown",
                        self._id,
                    )
                    self._cancel_requested = True

            if self._cancel_requested:
                self._finish_explicit_cancellation()
                return

            if execution_error is None and not self._rerun_requested:
                # No await is allowed between this final check and removal:
                # otherwise run() could set a marker that nobody consumes.
                self._cleanup_registry()
                return

            if execution_error is not None:
                if not await self._wait_before_runner_retry(
                    execution_retry_delay,
                    reason="execution recovery",
                ):
                    return
                execution_retry_delay = min(
                    execution_retry_delay * 2,
                    self._RUNNER_CAPACITY_RETRY_MAX_SECONDS,
                )

            self._rerun_requested = False
            if not await self._rebuild_runner_until_resolved():
                return
    
    @classmethod
    def set_runner_factory(cls, factory: TaskRunnerFactory) -> None:
        """Register the factory used to rebuild task runners."""
        cls._runner_factory = factory
    
    @classmethod
    async def get(cls, task_id: str) -> Optional['RedisStreamTask']:
        """Get a task by its ID.

        Returns:
            Optional[RedisStreamTask]: Task instance if found, None otherwise
        """
        return cls._task_registry.get(task_id)
    
    @classmethod
    def create(cls, params: Dict[str, Any]) -> "RedisStreamTask":
        """Create a new task instance from serializable runner parameters.

        Args:
            params: JSON-serializable runner parameters

        Returns:
            RedisStreamTask: New task instance
        """
        return cls(params)

    @classmethod
    def recover(
        cls, task_id: str, params: Dict[str, Any]
    ) -> "RedisStreamTask":
        """Recover the exact Redis stream identity after an API restart."""
        existing = cls._task_registry.get(task_id)
        return existing or cls(params, task_id=task_id)

    @classmethod
    async def destroy(cls) -> None:
        """Destroy all task instances."""
        # Never release a runner handle while its coroutine may still be using
        # it. Broadcast cancellation to the whole snapshot before waiting;
        # sequential 15-second waits let later tasks miss the application's
        # 30-second shutdown window entirely. All acknowledgements share one
        # deadline, and only confirmed-stopped runners are closed.
        tasks = list({id(task): task for task in cls._task_registry.values()}.values())
        if not tasks:
            return

        cancel_results = await asyncio.gather(
            *(task.cancel() for task in tasks),
            return_exceptions=True,
        )
        for task, result in zip(tasks, cancel_results):
            if isinstance(result, BaseException):
                logger.error(
                    "Task %s cancellation request failed during shutdown: %s",
                    task.id,
                    safe_exception_summary(result),
                )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + cls._DESTROY_CANCEL_TIMEOUT_SECONDS

        async def wait_for_stop(task: "RedisStreamTask") -> bool:
            return await task.wait_for_done(max(0.0, deadline - loop.time()))

        wait_results = await asyncio.gather(
            *(wait_for_stop(task) for task in tasks),
            return_exceptions=True,
        )
        stopped_tasks: list[RedisStreamTask] = []
        for task, result in zip(tasks, wait_results):
            if result is True:
                stopped_tasks.append(task)
                continue
            detail = (
                safe_exception_summary(result)
                if isinstance(result, BaseException)
                else "timeout"
            )
            logger.error(
                "Task %s did not stop during shutdown (%s); retaining its resources",
                task.id,
                detail,
            )

        async def close_stopped_runner(task: "RedisStreamTask") -> None:
            if getattr(task, "_runner_closed", False):
                return
            runner = task._runner
            close = getattr(runner, "aclose", None) if runner else None
            try:
                if callable(close):
                    await close()
            finally:
                task._runner_closed = True

        close_results = await asyncio.gather(
            *(close_stopped_runner(task) for task in stopped_tasks),
            return_exceptions=True,
        )
        for task, result in zip(stopped_tasks, close_results):
            if isinstance(result, BaseException):
                logger.error(
                    "Task %s runner close failed during shutdown: %s",
                    task.id,
                    safe_exception_summary(result),
                )
    
    def __repr__(self) -> str:
        """String representation of the task."""
        return f"RedisStreamTask(id={self._id}, done={self._done})"
