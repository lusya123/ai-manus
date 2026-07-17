import asyncio
import uuid
import logging
from typing import Any, Dict, Optional

from app.domain.external.task import Task, TaskRunner, TaskRunnerFactory
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
    
    @property
    def _done(self) -> bool:
        if self._execution_task is None:
            return True
        return self._execution_task.done()
    
    async def is_done(self) -> bool:
        """Check if the task is done.

        Returns:
            bool: True if the task is done, False otherwise
        """
        return self._done
    
    async def run(self) -> None:
        """Run the task using the runner built by the registered factory."""
        if not self._done:
            if not self._cancel_requested:
                self._rerun_requested = True
            return

        if self._runner is None or getattr(self, "_runner_closed", False):
            if RedisStreamTask._runner_factory is None:
                raise RuntimeError("No TaskRunnerFactory registered for RedisStreamTask")
            self._runner = await RedisStreamTask._runner_factory.create_runner(self._params)
            self._runner_closed = False

        self._rerun_requested = False
        self._cancel_requested = False
        # Completed executions remove themselves from the registry.  A new
        # turn can legitimately reuse the same task object obtained just
        # before that cleanup, so register it again before restarting.
        RedisStreamTask._task_registry[self._id] = self
        self._execution_task = asyncio.create_task(self._execute_task())
        logger.info(f"Task {self._id} execution started")
    
    async def cancel(self) -> bool:
        """Cancel the task.

        Returns:
            bool: True if the task is cancelled, False otherwise
        """
        if not self._done:
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
        execution_task = self._execution_task
        if execution_task is None or execution_task.done():
            return True
        try:
            await asyncio.wait_for(
                asyncio.shield(execution_task), timeout=max(0.0, timeout_seconds)
            )
        except asyncio.CancelledError:
            # A normally acknowledged cancellation leaves the execution task
            # in the cancelled state, which is still terminal/done.  Preserve
            # cancellation of the caller itself when the execution is live.
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
            if execution_task.done():
                return True
            raise
        except TimeoutError:
            return False
        return execution_task.done()
    
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
        if self._id in RedisStreamTask._task_registry:
            del RedisStreamTask._task_registry[self._id]
            logger.info(f"Task {self._id} removed from registry")
    
    async def _execute_task(self):
        """Execute every requested runner generation without a tail gap.

        ``run()`` can arrive while ``on_done``/``aclose`` is awaiting.  The
        final rerun check therefore happens after those awaits, and registry
        removal immediately follows a negative check with no intervening await.
        """
        while True:
            try:
                while True:
                    await self._runner.run(self)
                    if self._cancel_requested or not self._rerun_requested:
                        break
                    self._rerun_requested = False
                    logger.debug(
                        "Task %s received another run request while active; "
                        "rechecking its input stream",
                        self._id,
                    )
            except asyncio.CancelledError:
                logger.info(f"Task {self._id} execution cancelled")
                self._cancel_requested = True
            except Exception as e:
                logger.error(
                    "Task %s execution failed: %s",
                    self._id,
                    safe_exception_summary(e),
                )

            await self._finalize_runner()

            if self._cancel_requested:
                self._cancel_requested = False
                self._rerun_requested = False
                self._cleanup_registry()
                return

            if not self._rerun_requested:
                # No await is allowed between this final check and removal:
                # otherwise run() could set a marker that nobody consumes.
                self._cleanup_registry()
                return

            self._rerun_requested = False
            if RedisStreamTask._runner_factory is None:
                logger.error(
                    "Task %s cannot rebuild its runner for a requested rerun",
                    self._id,
                )
                self._cleanup_registry()
                return
            try:
                self._runner = await RedisStreamTask._runner_factory.create_runner(
                    self._params
                )
                self._runner_closed = False
            except Exception as exc:
                logger.error(
                    "Task %s runner rebuild failed: %s",
                    self._id,
                    safe_exception_summary(exc),
                )
                self._cleanup_registry()
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
        # it. cancel() only schedules cancellation; wait for acknowledgement,
        # then close clients non-destructively. Sandbox deletion belongs to the
        # session cleanup path, not global task-backend shutdown.
        for task in list(cls._task_registry.values()):
            await task.cancel()
            stopped = await task.wait_for_done(
                cls._DESTROY_CANCEL_TIMEOUT_SECONDS
            )
            if not stopped:
                logger.error(
                    "Task %s did not stop during shutdown; retaining its resources",
                    task.id,
                )
                continue
            if task._runner:
                close = getattr(task._runner, "aclose", None)
                if callable(close):
                    await close()
    
    def __repr__(self) -> str:
        """String representation of the task."""
        return f"RedisStreamTask(id={self._id}, done={self._done})"
