from typing import Any, Dict, Optional, AsyncGenerator, List, Type
import asyncio
from contextlib import asynccontextmanager
import inspect
import hashlib
import io
import json
import logging
import os
import re
import uuid
import weakref
from datetime import UTC, datetime, timedelta
from glob import escape as escape_glob
from pathlib import PurePosixPath
import debugpy
from pydantic import TypeAdapter
from app.domain.models.message import Message
from app.domain.models.event import (
    BaseEvent,
    ErrorEvent,
    TitleEvent,
    MessageEvent,
    DoneEvent,
    ToolEvent,
    WaitEvent,
    FileToolContent,
    ShellToolContent,
    SearchToolContent,
    BrowserToolContent,
    PreviewToolContent,
    ToolStatus,
    AgentEvent,
    McpToolContent,
)
from app.domain.services.flows.plan_act import PlanActFlow
from app.domain.external.sandbox import Sandbox
from app.domain.external.sandbox_provisioner import (
    SandboxProvisioningRequiredError,
)
from app.domain.external.browser import Browser
from app.domain.external.search import SearchEngine
from app.domain.external.file import FileStorage
from app.domain.external.llm import LLM, LLMFactory
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.external.task import (
    RunnerCleanupCapacityError,
    TaskRunner,
    TaskRunnerFactory,
    Task,
)
from app.domain.repositories.session_repository import SessionRepository
from app.domain.repositories.turn_submission_repository import TurnSubmissionRepository
from app.domain.models.turn_submission import (
    TERMINAL_TURN_STATES,
    TurnClaimDecision,
    TurnClaimResult,
    TurnSubmissionState,
)
from app.core.config import get_settings
from app.domain.utils.error_reporting import safe_exception_summary
from app.domain.repositories.mcp_repository import MCPRepository
from app.domain.models.session import SessionStatus
from app.domain.models.file import FileInfo
from app.domain.services.tools.mcp import MCPToolkit
from app.domain.models.tool_result import ToolResult
from app.domain.models.search import SearchResults

logger = logging.getLogger(__name__)


# Automatic screenshots and generated-file discovery are response enrichment,
# not part of the durable chat result.  Keep one process-wide registry per
# event loop so a sequence of short-lived runners cannot each leave its own
# unbounded collection of timed-out work behind.  Celery deliberately reuses
# one event loop per worker process, so this also bounds work across jobs.
_ARTIFACT_TASKS_BY_LOOP: dict[
    asyncio.AbstractEventLoop,
    set[asyncio.Task[Any]],
] = {}
_DEFERRED_RUNNER_CLOSE_TASKS_BY_LOOP: dict[
    asyncio.AbstractEventLoop,
    set[asyncio.Task[Any]],
] = {}
_RUNNER_CLEANUP_LEASES_BY_LOOP: dict[
    asyncio.AbstractEventLoop,
    set[object],
] = {}
_ARTIFACT_SHUTTING_DOWN_LOOPS: weakref.WeakSet[
    asyncio.AbstractEventLoop
] = weakref.WeakSet()


class _ArtifactPathSyncEntry:
    """One loop-local path lock retained only while it has users."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


_ARTIFACT_PATH_SYNCS_BY_LOOP: dict[
    asyncio.AbstractEventLoop,
    dict[tuple[str, str], _ArtifactPathSyncEntry],
] = {}
_MAX_RUNNER_CLEANUP_BUNDLES = 64
_RUNNER_CLEANUP_ATTEMPT_TIMEOUT_SECONDS = 6.0


def _canonical_artifact_lock_path(file_path: str) -> str:
    """Return one lexical absolute POSIX identity for an artifact lock.

    Lock identity must not depend on harmless path spelling differences.  Do
    not use filesystem resolution here: following symlinks would add blocking
    I/O and a time-of-check/time-of-use race to an event-loop admission path.
    Absolute paths are clamped at ``/``; relative paths are anchored at the
    deliverable root and cannot escape it through parent components.
    """
    path = (file_path or "").strip().strip("\"'`")
    if path == "~":
        path = "/home/ubuntu"
    elif path.startswith("~/"):
        path = f"/home/ubuntu/{path[2:]}"

    is_absolute = path.startswith("/")
    components = [] if is_absolute else ["home", "ubuntu", "upload"]
    minimum_depth = 0 if is_absolute else len(components)
    for component in path.split("/"):
        if not component or component == ".":
            continue
        if component == "..":
            if len(components) > minimum_depth:
                components.pop()
            continue
        components.append(component)
    return f"/{'/'.join(components)}" if components else "/"


class _RunnerCleanupLease:
    """One loop-local admission slot held until every runner handle closes."""

    def __init__(self, loop: asyncio.AbstractEventLoop, token: object) -> None:
        self._loop = loop
        self._token = token
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        leases = _RUNNER_CLEANUP_LEASES_BY_LOOP.get(self._loop)
        if leases is None:
            return
        leases.discard(self._token)
        if not leases:
            _RUNNER_CLEANUP_LEASES_BY_LOOP.pop(self._loop, None)


def _reserve_runner_cleanup_lease() -> _RunnerCleanupLease:
    """Atomically reserve capacity on the currently running event loop."""
    loop = asyncio.get_running_loop()
    leases = _RUNNER_CLEANUP_LEASES_BY_LOOP.setdefault(loop, set())
    if len(leases) >= _MAX_RUNNER_CLEANUP_BUNDLES:
        raise RunnerCleanupCapacityError(
            "Runner cleanup capacity is temporarily exhausted"
        )
    # This function contains no await.  Event-loop execution therefore makes
    # the capacity check and reservation one atomic admission operation.
    token = object()
    leases.add(token)
    return _RunnerCleanupLease(loop, token)


def _runner_cleanup_leases_for_loop(
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> set[object]:
    """Return the live cleanup reservations for diagnostics and tests."""
    loop = loop or asyncio.get_running_loop()
    return _RUNNER_CLEANUP_LEASES_BY_LOOP.get(loop, set())


def begin_artifact_enrichment_shutdown(
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> None:
    """Close one event loop's admission gate before shutdown draining."""
    _ARTIFACT_SHUTTING_DOWN_LOOPS.add(loop or asyncio.get_running_loop())


def end_artifact_enrichment_shutdown(
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> None:
    """Open one loop's gate on process startup (and for isolated tests)."""
    _ARTIFACT_SHUTTING_DOWN_LOOPS.discard(
        loop or asyncio.get_running_loop()
    )


def _artifact_enrichment_is_shutting_down(
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> bool:
    return (loop or asyncio.get_running_loop()) in _ARTIFACT_SHUTTING_DOWN_LOOPS


@asynccontextmanager
async def _serialize_artifact_path_sync(
    session_id: str,
    file_path: str,
):
    """Serialize one session/path without permanently retaining locks/loops."""
    loop = asyncio.get_running_loop()
    key = (session_id, _canonical_artifact_lock_path(file_path))
    entries = _ARTIFACT_PATH_SYNCS_BY_LOOP.setdefault(loop, {})
    entry = entries.get(key)
    if entry is None:
        entry = _ArtifactPathSyncEntry()
        entries[key] = entry
    entry.users += 1
    acquired = False
    try:
        await entry.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        entry.users -= 1
        if entry.users == 0 and entries.get(key) is entry:
            entries.pop(key, None)
        if not entries:
            _ARTIFACT_PATH_SYNCS_BY_LOOP.pop(loop, None)


def _artifact_tasks_for_loop(
    loop: Optional[asyncio.AbstractEventLoop] = None,
    *,
    create: bool = False,
) -> set[asyncio.Task[Any]]:
    loop = loop or asyncio.get_running_loop()
    tasks = _ARTIFACT_TASKS_BY_LOOP.get(loop)
    if tasks is None:
        if not create:
            return set()
        tasks = set()
        _ARTIFACT_TASKS_BY_LOOP[loop] = tasks
        return tasks
    # Done callbacks normally remove tasks immediately.  Pruning here keeps
    # the capacity decision conservative but independent of callback timing.
    tasks.difference_update(task for task in tuple(tasks) if task.done())
    if not tasks and _ARTIFACT_TASKS_BY_LOOP.get(loop) is tasks:
        _ARTIFACT_TASKS_BY_LOOP.pop(loop, None)
    return tasks


def _retain_deferred_close_task(
    task: asyncio.Task[Any],
    *,
    agent_id: str,
) -> None:
    """Keep delayed handle cleanup alive after a bounded ``aclose`` call."""
    loop = task.get_loop()
    tasks = _DEFERRED_RUNNER_CLOSE_TASKS_BY_LOOP.setdefault(loop, set())
    if task in tasks:
        return
    tasks.add(task)

    def cleanup(completed: asyncio.Task[Any]) -> None:
        tasks.discard(completed)
        if (
            not tasks
            and _DEFERRED_RUNNER_CLOSE_TASKS_BY_LOOP.get(loop) is tasks
        ):
            _DEFERRED_RUNNER_CLOSE_TASKS_BY_LOOP.pop(loop, None)
        try:
            completed.exception()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            logger.error(
                "Deferred runner close failed: agent_id=%s error=%s",
                agent_id,
                safe_exception_summary(error),
            )

    task.add_done_callback(cleanup)


def _deferred_close_tasks_for_loop(
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> set[asyncio.Task[Any]]:
    loop = loop or asyncio.get_running_loop()
    tasks = _DEFERRED_RUNNER_CLOSE_TASKS_BY_LOOP.get(loop)
    if tasks is None:
        return set()
    tasks.difference_update(task for task in tuple(tasks) if task.done())
    if (
        not tasks
        and _DEFERRED_RUNNER_CLOSE_TASKS_BY_LOOP.get(loop) is tasks
    ):
        _DEFERRED_RUNNER_CLOSE_TASKS_BY_LOOP.pop(loop, None)
    return tasks


async def drain_artifact_enrichment_tasks(
    timeout_seconds: float,
    *,
    request_cancel: bool = True,
) -> int:
    """Bound shutdown waiting before Mongo/GridFS clients are closed.

    Returns the number of tasks whose external publish outcome is still
    unknown after the bounded wait.  Such tasks stay strongly retained and
    continue to occupy capacity; they are never silently forgotten.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_seconds)
    observed_empty = False
    while True:
        artifact_tasks = tuple(_artifact_tasks_for_loop(loop, create=False))
        close_tasks = tuple(_deferred_close_tasks_for_loop(loop))
        tasks = tuple(dict.fromkeys((*artifact_tasks, *close_tasks)))
        if not tasks:
            # One loop turn makes "empty" stable even when a completion
            # callback schedules the next bounded cleanup generation.
            if observed_empty:
                return 0
            observed_empty = True
            await asyncio.sleep(0)
            continue
        observed_empty = False
        if request_cancel:
            # Deferred close tasks own the safe ordering between enrichment
            # and browser/sandbox shutdown.  Cancel only logical enrichments;
            # once a blob has reached publish, their state machine suppresses
            # cancellation until Mongo reports a known outcome.
            for task in artifact_tasks:
                if not task.done() and task.cancelling() == 0:
                    task.cancel()
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.warning(
                "Artifact enrichment shutdown drain reached its time limit: "
                "pending=%s",
                len(tasks),
            )
            return len(tasks)
        _, pending = await asyncio.wait(
            tasks,
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if pending and loop.time() >= deadline:
            # Re-snapshot so the reported number includes tasks added by a
            # just-completed cleanup generation and excludes completed ones.
            latest = tuple(
                dict.fromkeys(
                    (
                        *_artifact_tasks_for_loop(loop, create=False),
                        *_deferred_close_tasks_for_loop(loop),
                    )
                )
            )
            logger.warning(
                "Artifact enrichment shutdown drain reached its time limit: "
                "pending=%s",
                len(latest),
            )
            return len(latest)


async def _close_resource(resource: Any, *method_names: str) -> bool:
    """Await the first supported close method on a live resource handle."""
    if resource is None:
        return False
    for method_name in method_names:
        method = getattr(resource, method_name, None)
        if not callable(method):
            continue
        result = method()
        if inspect.isawaitable(result):
            await result
        return True
    return False


async def _await_cleanup_attempt_to_finish(
    task: asyncio.Task[Any],
) -> Any:
    """Keep one close attempt alive despite cancellation of its coordinator."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                break
            continue
    return task.result()


async def _close_resource_bundle(
    resources: tuple[tuple[str, Any, tuple[str, ...]], ...],
    *,
    cleanup_lease: _RunnerCleanupLease,
    agent_id: str,
    attempt_timeout_seconds: float,
) -> None:
    """Close one runner's handles serially and release its admission lease.

    A timed-out close attempt remains the only active close coroutine for this
    bundle.  The coordinator waits for that exact attempt instead of starting
    another resource close beside it.  A completed failure is retried with
    bounded backoff; losing the lease would otherwise admit more handles while
    the failed handle's state is still unknown.
    """
    for name, resource, methods in resources:
        retry_delay = 0.1
        while True:
            close_task = asyncio.create_task(
                _close_resource(resource, *methods)
            )
            done, _ = await asyncio.wait(
                (close_task,),
                timeout=max(0.0, attempt_timeout_seconds),
            )
            if not done:
                logger.error(
                    "Timed out closing Agent %s %s handle; cleanup bundle "
                    "remains backpressured",
                    agent_id,
                    name,
                )
            try:
                await _await_cleanup_attempt_to_finish(close_task)
                break
            except asyncio.CancelledError:
                logger.error(
                    "Agent %s %s handle close was cancelled before its "
                    "outcome was known; retrying",
                    agent_id,
                    name,
                )
            except Exception as exc:
                logger.error(
                    "Failed to close Agent %s %s handle; retrying: %s",
                    agent_id,
                    name,
                    safe_exception_summary(exc),
                )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 5.0)

    cleanup_lease.release()


def _start_resource_cleanup_bundle(
    resources: tuple[tuple[str, Any, tuple[str, ...]], ...],
    *,
    cleanup_lease: _RunnerCleanupLease,
    agent_id: str,
    attempt_timeout_seconds: float,
) -> asyncio.Task[None]:
    """Start and strongly retain one cleanup bundle until it completes."""
    task = asyncio.create_task(
        _close_resource_bundle(
            resources,
            cleanup_lease=cleanup_lease,
            agent_id=agent_id,
            attempt_timeout_seconds=attempt_timeout_seconds,
        )
    )
    _retain_deferred_close_task(task, agent_id=agent_id)
    return task

class AgentTaskRunner(TaskRunner):
    """Agent task that can be cancelled"""
    _DELIVERABLE_ROOT = "/home/ubuntu/upload"
    _ARTIFACT_SYNC_TIMEOUT_SECONDS = 10.0
    _ARTIFACT_CLOSE_DRAIN_TIMEOUT_SECONDS = (
        _RUNNER_CLEANUP_ATTEMPT_TIMEOUT_SECONDS
    )
    _MAX_BACKGROUND_ARTIFACT_CLEANUPS = 64
    _MAX_AUTO_ARTIFACT_CANDIDATES = 32
    _MAX_EVENT_ATTACHMENTS = 32
    _MAX_TRACKED_ARTIFACTS = 128
    _MAX_ARTIFACT_DISCOVERY_TEXT_CHARS = 256_000
    _GENERATING_FILE_FUNCTIONS = {"file_write", "file_str_replace"}
    _ARTIFACT_EXTENSIONS = {
        ".csv", ".docx", ".html", ".htm", ".jpeg", ".jpg", ".json",
        ".log", ".md", ".pdf", ".png", ".pptx", ".py", ".tar",
        ".tgz", ".ts", ".txt", ".vue", ".xlsx", ".zip",
    }
    _INPUT_CONSUMER_GROUP = "agent-turn-workers"
    _INPUT_CLAIM_IDLE_MS = 1_000
    def __init__(
        self,
        session_id: str,
        agent_id: str,
        user_id: str,
        sandbox_id: str,
        sandbox: Sandbox,
        browser: Browser,
        agent_repository: AgentRepository,
        session_repository: SessionRepository,
        file_storage: FileStorage,
        mcp_repository: MCPRepository,
        llm: LLM,
        search_engine: Optional[SearchEngine] = None,
        turn_submission_repository: Optional[TurnSubmissionRepository] = None,
        cleanup_lease: Optional[_RunnerCleanupLease] = None,
    ):
        self._session_id = session_id
        self._agent_id = agent_id
        self._user_id = user_id
        self._sandbox_id = sandbox_id
        self._sandbox = sandbox
        self._browser = browser
        self._search_engine = search_engine
        self._repository = agent_repository
        self._session_repository = session_repository
        self._file_storage = file_storage
        self._mcp_repository = mcp_repository
        self._llm = llm
        self._turn_submission_repository = turn_submission_repository
        self._worker_id = str(uuid.uuid4())
        settings = get_settings()
        self._claim_seconds = max(30, int(settings.chat_turn_claim_seconds))
        self._claim_renew_seconds = max(
            1,
            min(
                int(settings.chat_turn_claim_renew_seconds),
                max(1, self._claim_seconds // 2),
            ),
        )
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._closing = False
        self._close_task: Optional[asyncio.Task[None]] = None
        self._cleanup_lease = cleanup_lease
        self._generated_artifacts: dict[str, FileInfo] = {}
        self._synced_artifacts: dict[str, FileInfo] = {}
        self._artifact_cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._artifact_ownership_changed = asyncio.Event()
        self._mcp_tool = MCPToolkit()
        self._flow = PlanActFlow(
            self._agent_id,
            self._repository,
            self._session_id,
            self._session_repository,
            self._sandbox,
            self._browser,
            self._mcp_tool,
            self._llm,
            self._search_engine,
        )

    async def _put_and_add_event(
        self,
        task: Task,
        event: AgentEvent,
        turn_id: Optional[str] = None,
    ) -> BaseEvent:
        event.turn_id = turn_id
        if (
            self._turn_submission_repository is not None
            and turn_id is not None
        ):
            # The per-turn outbox is independent of the bounded Session.events
            # projection. If this Mongo write fails, never publish to Redis.
            persisted = await self._turn_submission_repository.append_output(
                self._session_id, turn_id, event
            )
            if isinstance(persisted, BaseEvent):
                event = persisted
        add_once = getattr(self._session_repository, "add_event_once", None)
        persisted = (
            await add_once(self._session_id, event)
            if callable(add_once)
            else await self._session_repository.add_event(self._session_id, event)
        )
        if isinstance(persisted, BaseEvent):
            event = persisted
        if (
            self._turn_submission_repository is not None
            and turn_id is not None
        ):
            # Durable clients replay/follow the authoritative Mongo outbox.
            # There is no Redis output consumer on this path, so publishing a
            # second permanent copy would only leak retention and memory.
            return event
        try:
            transport_id = await task.output_stream.put(event.model_dump_json())
        except Exception as exc:
            # Mongo history is authoritative; a live Redis outage must not erase
            # the response or prevent terminal-state persistence.
            logger.warning(
                "Agent output live transport unavailable: agent_id=%s session_id=%s "
                "submission_id=%s error=%s",
                self._agent_id,
                self._session_id,
                turn_id,
                type(exc).__name__,
            )
            return event
        event.transport_id = transport_id
        update_cursor = getattr(
            self._session_repository, "update_event_transport_cursor", None
        )
        if callable(update_cursor):
            try:
                await update_cursor(self._session_id, event.id, transport_id)
            except Exception as exc:
                logger.warning(
                    "Could not persist output transport cursor: session_id=%s "
                    "submission_id=%s event_type=%s error=%s",
                    self._session_id,
                    turn_id,
                    event.type,
                    type(exc).__name__,
                )
        if (
            self._turn_submission_repository is not None
            and turn_id is not None
        ):
            try:
                await self._turn_submission_repository.update_output_transport_cursor(
                    self._session_id,
                    turn_id,
                    event.id,
                    transport_id,
                )
            except Exception as exc:
                logger.warning(
                    "Could not persist outbox transport cursor: session_id=%s "
                    "submission_id=%s event_type=%s error=%s",
                    self._session_id,
                    turn_id,
                    event.type,
                    type(exc).__name__,
                )
        return event
    
    async def _pop_event(self, task: Task) -> AgentEvent:
        event_id, event_str = await task.input_stream.pop()
        if event_str is None:
            logger.warning("Agent %s received an empty input entry", self._agent_id)
            return
        event = TypeAdapter(AgentEvent).validate_json(event_str)
        event.id = event_id
        return event

    @staticmethod
    async def _await_task_to_known_outcome(task: asyncio.Task[Any]) -> Any:
        """Ignore repeated caller cancellation until lifecycle I/O replies.

        Callers use this only after starting an operation whose commit outcome
        must be known before ownership can be released or compensated.
        """
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    break
                continue
        return task.result()

    async def _claim_turn_to_known_outcome(
        self,
        submission_id: str,
        *,
        task_id: str,
        owner: str,
    ) -> tuple[TurnClaimResult, Optional[asyncio.CancelledError]]:
        """Finish the Mongo claim even when task cancellation races its reply.

        Motor cancellation cannot prove that a find-and-update did not commit.
        The child task is therefore shielded and, after any number of caller
        cancellations, awaited to a known result. The saved cancellation is
        replayed only after an acquired owner has durably terminalized its
        turn in ``_process_durable_entry``.
        """

        claim_task = asyncio.create_task(
            self._turn_submission_repository.claim_for_execution(
                self._session_id,
                submission_id,
                task_id=task_id,
                owner=owner,
                claim_until=datetime.now(UTC)
                + timedelta(seconds=self._claim_seconds),
            )
        )
        try:
            return await asyncio.shield(claim_task), None
        except asyncio.CancelledError as cancellation:
            # If the repository still cannot reconcile the claim, propagate
            # that control-plane error instead of the saved explicit cancel.
            # Task backends must then keep retrying until a RUNNING owner is
            # terminalized; treating an unknown Mongo outcome as a completed
            # cancellation could pin a deleting Session forever.
            claim = await self._await_task_to_known_outcome(claim_task)
            return claim, cancellation

    async def _delete_unpublished_upload(self, file_info: FileInfo) -> None:
        """Best-effort compensation after a confirmed publish failure."""
        delete_file = getattr(self._file_storage, "delete_file", None)
        if not callable(delete_file) or not file_info.file_id:
            return
        cleanup_task = asyncio.create_task(
            delete_file(file_info.file_id, self._user_id)
        )
        try:
            await self._await_task_to_known_outcome(cleanup_task)
        except BaseException as error:
            logger.error(
                "Could not compensate unpublished upload: agent_id=%s "
                "error=%s",
                self._agent_id,
                safe_exception_summary(error),
            )

    async def _uploaded_file_reference_state(
        self,
        file_info: FileInfo,
        *,
        file_path: Optional[str] = None,
    ) -> Optional[bool]:
        """Read after an ambiguous Mongo reply without guessing commit state."""
        count_references = getattr(
            self._session_repository,
            "count_file_references",
            None,
        )
        reference_count_verified = False
        try:
            if callable(count_references):
                if await count_references(file_info.file_id):
                    return True
                reference_count_verified = True
        except Exception as error:
            logger.error(
                "Could not verify ambiguous artifact references: agent_id=%s "
                "error=%s",
                self._agent_id,
                safe_exception_summary(error),
            )
            return None

        if file_path:
            get_by_path = getattr(
                self._session_repository,
                "get_file_by_path",
                None,
            )
            if callable(get_by_path):
                try:
                    current = await get_by_path(self._session_id, file_path)
                except ValueError:
                    # ``get_file_by_path`` uses ValueError for a missing
                    # session, but do not infer absence from a broad exception
                    # type alone.  Re-read the exact session identity: only a
                    # successful zero-reference query plus a definitive
                    # identity miss proves that this newly uploaded blob can
                    # no longer be published by this session.
                    find_session = getattr(
                        self._session_repository,
                        "find_by_id",
                        None,
                    )
                    if not reference_count_verified or not callable(find_session):
                        return None
                    try:
                        session = await find_session(self._session_id)
                    except Exception as error:
                        logger.error(
                            "Could not verify missing artifact session identity: "
                            "agent_id=%s error=%s",
                            self._agent_id,
                            safe_exception_summary(error),
                        )
                        return None
                    return False if session is None else None
                except Exception as error:
                    logger.error(
                        "Could not verify ambiguous artifact path: agent_id=%s "
                        "error=%s",
                        self._agent_id,
                        safe_exception_summary(error),
                    )
                    return None
                if current and current.file_id == file_info.file_id:
                    return True

        # A zero count is authoritative only when the repository actually
        # performed the global session/outbox reference query.  Test doubles
        # or alternate repositories without that capability remain unknown.
        return False if reference_count_verified else None

    async def _resolve_publish_failure(
        self,
        file_info: FileInfo,
        error: BaseException,
        *,
        file_path: Optional[str] = None,
    ) -> bool:
        """Return true only when read-after-error confirms the new reference."""
        reference_state = await self._uploaded_file_reference_state(
            file_info,
            file_path=file_path,
        )
        if reference_state is True:
            logger.warning(
                "Artifact publish reply was lost after commit: agent_id=%s",
                self._agent_id,
            )
            return True
        if reference_state is False and isinstance(error, ValueError):
            # Repository ValueError is the explicit session-not-found path;
            # a successful read also confirmed that no reference was written.
            await self._delete_unpublished_upload(file_info)
        else:
            # Network/timeouts are unknown outcomes.  Preserve the blob and
            # quota conservatively; deleting it could break a committed Mongo
            # reference.  Auto-artifact metadata makes it auditable later.
            logger.error(
                "Artifact publish outcome remains unknown; retaining upload: "
                "agent_id=%s error=%s",
                self._agent_id,
                safe_exception_summary(error),
            )
        return False

    async def _run_retained_artifact_state_machine(
        self,
        operation: Any,
    ) -> Any:
        """Finish committed artifact work despite cancellation of its caller.

        The parent enrichment task owns the process-wide capacity slot.  Once
        an upload operation has been started, this child owns the complete
        upload/publish/read-after-error/compensation transition.  Shielding
        prevents a deadline or runner close from cancelling the child; the
        strongly retained parent does not finish (and therefore cannot release
        its slot) until the child's external outcome is known.
        """
        state_task = asyncio.create_task(operation)
        try:
            return await asyncio.shield(state_task)
        except asyncio.CancelledError:
            # No browser/sandbox operation remains after the state machine is
            # created. Transfer handle ownership to the process coordinator,
            # while the parent task continues to hold its global capacity slot.
            self._detach_artifact_cleanup_task(asyncio.current_task())
            return await self._await_task_to_known_outcome(state_task)

    async def _get_browser_screenshot(self) -> str:
        screenshot = await self._browser.screenshot()
        screenshot_data = io.BytesIO(screenshot)

        async def upload_and_publish() -> str:
            try:
                result = await self._file_storage.upload_file(
                    screenshot_data,
                    "screenshot.png",
                    self._user_id,
                    content_type="image/png",
                    metadata={
                        "manus_auto_artifact_session_id": self._session_id,
                        "manus_auto_artifact_kind": "browser_screenshot",
                    },
                )
            finally:
                await _close_resource(screenshot_data, "close", "aclose")
            try:
                await self._session_repository.add_file(
                    self._session_id,
                    result,
                )
            except Exception as error:
                published = await self._resolve_publish_failure(result, error)
                return result.file_id if published else ""
            return result.file_id

        return await self._run_retained_artifact_state_machine(
            upload_and_publish()
        )

    @staticmethod
    def _normalize_sandbox_path(file_path: str) -> str:
        path = (file_path or "").strip().strip("\"'`")
        return f"/home/ubuntu/{path[2:]}" if path.startswith("~/") else path

    def _looks_like_artifact_path(self, file_path: str) -> bool:
        path = self._normalize_sandbox_path(file_path).lower()
        return path.endswith(".tar.gz") or PurePosixPath(path).suffix in self._ARTIFACT_EXTENSIONS

    def _is_auto_deliverable_path(self, file_path: str) -> bool:
        path = self._normalize_sandbox_path(file_path)
        return path == self._DELIVERABLE_ROOT or path.startswith(
            f"{self._DELIVERABLE_ROOT}/"
        )

    def _extract_artifact_paths(
        self,
        text: str,
        max_paths: Optional[int] = None,
    ) -> List[str]:
        if not text:
            return []
        text = text[: self._MAX_ARTIFACT_DISCOVERY_TEXT_CHARS]
        path_limit = max_paths or self._MAX_AUTO_ARTIFACT_CANDIDATES
        suffixes = sorted(
            (extension.lstrip(".") for extension in self._ARTIFACT_EXTENSIONS),
            key=len,
            reverse=True,
        )
        pattern = re.compile(
            # A slash inside ``./report.md``, ``dir/report.md`` or a URL is
            # not the beginning of an absolute sandbox path.  Without this
            # boundary, ``./PLAN.md`` was parsed as ``/PLAN.md`` and the
            # missing-file fallback recursively searched the filesystem root.
            rf"(?<![\w./~+\-])(?P<path>(?:~/|/)[^\s\"'`<>|;&]*?\.(?:tar\.gz|{'|'.join(map(re.escape, suffixes))}))",
            re.IGNORECASE,
        )
        url_spans = [
            match.span()
            for match in re.finditer(
                r"[a-z][a-z0-9+.-]*://[^\s\"'`<>]+",
                text,
                re.IGNORECASE,
            )
        ]
        paths: List[str] = []
        seen_paths: set[str] = set()
        url_index = 0
        for match in pattern.finditer(text):
            match_start = match.start()
            # Both regex iterators yield spans in source order. Advance a
            # single pointer instead of rescanning every URL for every path;
            # attacker-controlled messages with thousands of URL-like paths
            # must remain O(text length), not O(paths * URLs).
            while (
                url_index < len(url_spans)
                and url_spans[url_index][1] <= match_start
            ):
                url_index += 1
            if (
                url_index < len(url_spans)
                and url_spans[url_index][0] <= match_start
                < url_spans[url_index][1]
            ):
                continue
            drive_prefix_start = match_start - 2
            if (
                drive_prefix_start >= 0
                and re.fullmatch(
                    r"[A-Za-z]:",
                    text[drive_prefix_start:match_start],
                )
                and (
                    drive_prefix_start == 0
                    or not text[drive_prefix_start - 1].isalnum()
                )
            ):
                continue
            path = self._normalize_sandbox_path(match.group("path"))
            if path and path not in seen_paths:
                paths.append(path)
                seen_paths.add(path)
                if len(paths) >= path_limit:
                    break
        return paths

    def _remember_synced_artifact(self, file_info: Optional[FileInfo]) -> None:
        if file_info and file_info.file_path and self._looks_like_artifact_path(file_info.file_path):
            self._synced_artifacts.pop(file_info.file_path, None)
            self._synced_artifacts[file_info.file_path] = file_info
            while len(self._synced_artifacts) > self._MAX_TRACKED_ARTIFACTS:
                self._synced_artifacts.pop(next(iter(self._synced_artifacts)))

    def _remember_generated_artifact(self, file_info: Optional[FileInfo]) -> None:
        if (
            file_info
            and file_info.file_path
            and self._looks_like_artifact_path(file_info.file_path)
            and self._is_auto_deliverable_path(file_info.file_path)
        ):
            self._generated_artifacts.pop(file_info.file_path, None)
            self._generated_artifacts[file_info.file_path] = file_info
            while len(self._generated_artifacts) > self._MAX_EVENT_ATTACHMENTS:
                self._generated_artifacts.pop(
                    next(iter(self._generated_artifacts))
                )

    async def _cleanup_replaced_artifact(
        self,
        file_info: Optional[FileInfo],
    ) -> None:
        """Retain superseded blobs until an age-gated offline reconciliation.

        A final MessageEvent can hold an in-memory FileInfo before its durable
        outbox write.  An eager reference-count/delete here races that event
        and can create a broken attachment.  Keeping the owned blob is the
        conservative choice; metadata and reference queries support a future
        grace-period collector without risking live data.
        """
        if not file_info or not file_info.file_id:
            return
        metadata = file_info.metadata or {}
        if metadata.get("manus_auto_artifact_session_id") != self._session_id:
            return
        logger.debug(
            "Retaining superseded auto artifact for age-gated reconciliation: "
            "agent_id=%s",
            self._agent_id,
        )

    async def _resolve_existing_sandbox_file(self, file_path: str) -> Optional[str]:
        if not file_path:
            return None
        normalized = self._normalize_sandbox_path(file_path)
        candidates = [normalized]
        if normalized and not normalized.startswith("/"):
            candidates.extend(
                [
                    f"{self._DELIVERABLE_ROOT}/{normalized}",
                    f"/home/ubuntu/{normalized}",
                    f"/tmp/{normalized}",
                ]
            )
        for candidate in candidates:
            candidate_path = PurePosixPath(candidate)
            if not candidate_path.is_absolute() or not candidate_path.name:
                continue
            try:
                result = await self._sandbox.file_find(
                    str(candidate_path.parent),
                    escape_glob(candidate_path.name),
                )
                for found_path in (result.data or {}).get("files", []):
                    if PurePosixPath(found_path).name == candidate_path.name:
                        return found_path
            except Exception:
                pass

        basename = PurePosixPath(normalized).name
        if not basename:
            return None
        parent = str(PurePosixPath(normalized).parent)
        search_dirs: List[str] = []
        for candidate in (
            parent if parent != "." else "",
            "/home/ubuntu",
            self._DELIVERABLE_ROOT,
            "/tmp",
        ):
            candidate_path = PurePosixPath(candidate) if candidate else None
            is_bounded = bool(
                candidate_path
                and ".." not in candidate_path.parts
                and (
                    str(candidate_path) == "/home/ubuntu"
                    or str(candidate_path).startswith("/home/ubuntu/")
                    or str(candidate_path) == "/tmp"
                    or str(candidate_path).startswith("/tmp/")
                )
            )
            if is_bounded and candidate not in search_dirs:
                search_dirs.append(candidate)
        for search_dir in search_dirs:
            try:
                result = await self._sandbox.file_find(
                    search_dir,
                    f"**/{escape_glob(basename)}",
                )
                for candidate in (result.data or {}).get("files", []):
                    if PurePosixPath(candidate).name == basename:
                        logger.warning(
                            "Resolved a missing attachment path in the sandbox: agent_id=%s",
                            self._agent_id,
                        )
                        return candidate
            except Exception:
                continue
        return None

    async def _sync_file_to_storage(
        self,
        file_path: str,
        fallback_content: Optional[str] = None,
        generated: bool = False,
    ) -> Optional[FileInfo]:
        """Upload or update file and return FileInfo"""
        try:
            normalized = self._normalize_sandbox_path(file_path)
            resolved = await self._resolve_existing_sandbox_file(normalized)
            if (
                not resolved
                and fallback_content
                and PurePosixPath(normalized).suffix.lower() == ".md"
            ):
                resolved = (
                    normalized
                    if normalized.startswith("/")
                    else f"{self._DELIVERABLE_ROOT}/{normalized}"
                )
                await self._sandbox.file_write(
                    file=resolved,
                    content=fallback_content,
                    trailing_newline=True,
                )
                resolved = await self._resolve_existing_sandbox_file(resolved)
            if not resolved:
                logger.warning(
                    "Attachment file not found in sandbox: agent_id=%s",
                    self._agent_id,
                )
                return None
            # Resolve before locking. Relative discovery can fall back to a
            # different absolute sandbox path (for example ``report.md`` may
            # resolve to ``/home/ubuntu/report.md``). Locking the input spelling
            # would let that request race an explicit absolute alias.
            async with _serialize_artifact_path_sync(
                str(getattr(self, "_session_id", self._agent_id)),
                resolved,
            ):
                return await self._sync_resolved_file_to_storage(
                    resolved,
                    generated=generated,
                )
        except Exception as e:
            logger.error(
                "Agent %s failed to sync file: %s",
                self._agent_id,
                safe_exception_summary(e),
            )

    async def _sync_resolved_file_to_storage(
        self,
        file_path: str,
        *,
        generated: bool,
    ) -> Optional[FileInfo]:
        """Upload and publish one already-resolved absolute sandbox path."""
        if not generated and file_path in self._synced_artifacts:
            return self._synced_artifacts[file_path]
        previous_file_info = await self._session_repository.get_file_by_path(
            self._session_id,
            file_path,
        )
        file_data = await self._sandbox.file_download(file_path)
        file_name = file_path.split("/")[-1]

        async def upload_and_publish() -> Optional[FileInfo]:
            try:
                file_info = await self._file_storage.upload_file(
                    file_data,
                    file_name,
                    self._user_id,
                    metadata={
                        "manus_auto_artifact_session_id": self._session_id,
                        "manus_auto_artifact_path": file_path,
                    },
                )
            finally:
                await _close_resource(file_data, "close", "aclose")
            file_info.file_path = file_path
            upsert_file = getattr(
                self._session_repository,
                "upsert_file_by_path",
                None,
            )
            uses_atomic_path_upsert = callable(upsert_file)
            replaced_file_info = previous_file_info
            try:
                publish_result = await (
                    upsert_file(self._session_id, file_info)
                    if uses_atomic_path_upsert
                    else self._session_repository.add_file(
                        self._session_id,
                        file_info,
                    )
                )
                if uses_atomic_path_upsert:
                    replaced_file_info = publish_result
            except Exception as error:
                published = await self._resolve_publish_failure(
                    file_info,
                    error,
                    file_path=file_path,
                )
                if not published:
                    return None
            # Production repositories replace by path atomically.  The
            # fallback preserves compatibility with simple test/in-memory
            # repositories while still publishing before removing the prior
            # reference.
            if previous_file_info and not uses_atomic_path_upsert:
                await self._session_repository.remove_file(
                    self._session_id,
                    previous_file_info.file_id,
                )
            await self._cleanup_replaced_artifact(replaced_file_info)
            self._remember_synced_artifact(file_info)
            if generated:
                self._remember_generated_artifact(file_info)
            return file_info

        return await self._run_retained_artifact_state_machine(
            upload_and_publish()
        )
    
    async def _sync_file_to_sandbox(self, file_id: str) -> Optional[FileInfo]:
        """Download file from storage to sandbox"""
        try:
            file_data, file_info = await self._file_storage.download_file(file_id, self._user_id)
            file_path = f"{self._DELIVERABLE_ROOT}/{file_info.filename}"
            try:
                result = await self._sandbox.file_upload(file_data, file_path)
            finally:
                await _close_resource(file_data, "close", "aclose")
            if result.success:
                file_info.file_path = file_path
                return file_info
        except Exception as e:
            logger.error(
                "Agent %s failed to sync file: %s",
                self._agent_id,
                safe_exception_summary(e),
            )

    async def _sync_auto_artifact_before_deadline(
        self,
        file_path: str,
        deadline: float,
        *,
        source: str,
        fallback_content: Optional[str] = None,
        generated: bool = False,
    ) -> tuple[Optional[FileInfo], bool]:
        """Sync an inferred artifact without allowing it to hold a turn open.

        Returns ``(file_info, timed_out)``.  Automatic artifact discovery is
        best-effort response enrichment; a missing or slow file must not keep
        the durable turn in RUNNING indefinitely.
        """
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None, True
        if _artifact_enrichment_is_shutting_down():
            logger.info(
                "Skipping %s artifact during process shutdown: agent_id=%s",
                source,
                self._agent_id,
            )
            return None, True
        if not self._has_artifact_task_capacity():
            logger.error(
                "Process artifact enrichment limit reached: agent_id=%s limit=%s",
                self._agent_id,
                self._MAX_BACKGROUND_ARTIFACT_CLEANUPS,
            )
            return None, True
        sync_task = asyncio.create_task(
            self._sync_file_to_storage(
                file_path,
                fallback_content=fallback_content,
                generated=generated,
            )
        )
        # Occupy the process-wide slot before the task gets an event-loop turn;
        # concurrent runners therefore cannot all pass admission first and
        # overflow the limit later at their deadlines.
        self._track_artifact_cleanup_task(sync_task)
        try:
            file_info = await asyncio.wait_for(
                asyncio.shield(sync_task),
                timeout=remaining,
            )
            return file_info, False
        except asyncio.TimeoutError:
            if not sync_task.done() and sync_task.cancelling() == 0:
                sync_task.cancel()
            logger.warning(
                "Timed out syncing %s artifact: agent_id=%s",
                source,
                self._agent_id,
            )
            return None, True
        except asyncio.CancelledError:
            if not sync_task.done() and sync_task.cancelling() == 0:
                sync_task.cancel()
            raise

    def _has_artifact_task_capacity(self) -> bool:
        if getattr(self, "_closing", False):
            return False
        if _artifact_enrichment_is_shutting_down():
            return False
        tasks = _artifact_tasks_for_loop(create=False)
        return len(tasks) < self._MAX_BACKGROUND_ARTIFACT_CLEANUPS

    def _artifact_owner_change_event(self) -> asyncio.Event:
        event = getattr(self, "_artifact_ownership_changed", None)
        if event is None:
            event = asyncio.Event()
            self._artifact_ownership_changed = event
        return event

    def _track_artifact_cleanup_task(self, task: asyncio.Task[Any]) -> None:
        """Reserve one process-wide slot and retain this runner's ownership."""
        loop = task.get_loop()
        process_tasks = _artifact_tasks_for_loop(loop, create=True)
        owned_tasks = getattr(self, "_artifact_cleanup_tasks", None)
        if owned_tasks is None:
            owned_tasks = set()
            self._artifact_cleanup_tasks = owned_tasks
        owned_tasks.add(task)
        ownership_changed = self._artifact_owner_change_event()
        ownership_changed.set()

        if task in process_tasks:
            return
        process_tasks.add(task)
        agent_id = self._agent_id

        def cleanup(completed: asyncio.Task[Any]) -> None:
            owned_tasks.discard(completed)
            ownership_changed.set()
            process_tasks.discard(completed)
            if (
                not process_tasks
                and _ARTIFACT_TASKS_BY_LOOP.get(loop) is process_tasks
            ):
                _ARTIFACT_TASKS_BY_LOOP.pop(loop, None)
            try:
                completed.exception()
            except asyncio.CancelledError:
                pass
            except Exception as error:
                logger.error(
                    "Background artifact cleanup failed: agent_id=%s error=%s",
                    agent_id,
                    safe_exception_summary(error),
                )

        task.add_done_callback(cleanup)

    def _detach_artifact_cleanup_task(
        self,
        task: Optional[asyncio.Task[Any]],
    ) -> None:
        """Transfer a post-upload publish to the process coordinator only."""
        if task is None:
            return
        owned_tasks = getattr(self, "_artifact_cleanup_tasks", None)
        if owned_tasks is not None:
            owned_tasks.discard(task)
        self._artifact_owner_change_event().set()

    async def _sync_message_attachments_to_storage(self, event: MessageEvent) -> None:
        """Sync message attachments and update event attachments"""
        attachments: List[FileInfo] = []
        seen_paths: set[str] = set()
        try:
            candidates: List[tuple[str, Optional[str]]] = []
            if event.attachments:
                for attachment in event.attachments:
                    candidates.append((attachment.file_path, event.message))
            for path in self._extract_artifact_paths(
                event.message,
                max_paths=self._MAX_AUTO_ARTIFACT_CANDIDATES + 1,
            ):
                if any(candidate_path == path for candidate_path, _ in candidates):
                    continue
                candidates.append((path, None))

            if len(candidates) > self._MAX_AUTO_ARTIFACT_CANDIDATES:
                logger.warning(
                    "Message artifact candidate limit reached: agent_id=%s count=%s limit=%s",
                    self._agent_id,
                    len(candidates),
                    self._MAX_AUTO_ARTIFACT_CANDIDATES,
                )
            deadline = (
                asyncio.get_running_loop().time()
                + self._ARTIFACT_SYNC_TIMEOUT_SECONDS
            )
            for path, fallback_content in candidates[
                : self._MAX_AUTO_ARTIFACT_CANDIDATES
            ]:
                file_info, timed_out = await self._sync_auto_artifact_before_deadline(
                    path,
                    deadline,
                    source="message",
                    fallback_content=fallback_content,
                )
                if file_info:
                    attachments.append(file_info)
                    if file_info.file_path:
                        seen_paths.add(file_info.file_path)
                if timed_out:
                    break
            # Timed-out enrichment can finish between tool completion and the
            # final message.  Snapshot to avoid concurrent-size mutation.
            for path, file_info in list(self._generated_artifacts.items()):
                if len(attachments) >= self._MAX_EVENT_ATTACHMENTS:
                    logger.warning(
                        "Message attachment limit reached: agent_id=%s limit=%s",
                        self._agent_id,
                        self._MAX_EVENT_ATTACHMENTS,
                    )
                    break
                if path not in seen_paths:
                    attachments.append(file_info)
                    seen_paths.add(path)
            event.attachments = attachments
        except Exception as e:
            logger.error(
                "Agent %s failed to sync attachments to storage: %s",
                self._agent_id,
                safe_exception_summary(e),
            )
    
    async def _sync_message_attachments_to_sandbox(self, event: MessageEvent) -> None:
        """Sync message attachments and update event attachments"""
        attachments: List[FileInfo] = []
        try:
            if event.attachments:
                for attachment in event.attachments:
                    file_info = await self._sync_file_to_sandbox(attachment.file_id)
                    if file_info:
                        attachments.append(file_info)
                        await self._session_repository.add_file(self._session_id, file_info)
            event.attachments = attachments
        except Exception as e:
            logger.error(
                "Agent %s failed to sync attachments to event: %s",
                self._agent_id,
                safe_exception_summary(e),
            )

    async def _sync_shell_artifacts(
        self,
        event: ToolEvent,
        shell_result: Optional[ToolResult],
        *,
        deadline: Optional[float] = None,
    ) -> None:
        text_parts: List[str] = []
        remaining_text_chars = self._MAX_ARTIFACT_DISCOVERY_TEXT_CHARS

        def append_bounded(value: Any) -> None:
            nonlocal remaining_text_chars
            if not isinstance(value, str) or remaining_text_chars <= 0:
                return
            chunk = value[:remaining_text_chars]
            text_parts.append(chunk)
            remaining_text_chars -= len(chunk)

        for key in ("command", "exec_dir"):
            append_bounded(event.function_args.get(key))
        if shell_result and getattr(shell_result, "data", None):
            data = shell_result.data or {}
            for key in ("command", "output"):
                append_bounded(data.get(key))
            # Prefer the newest records if a legacy sandbox returns a long
            # console history.  New sandboxes also maintain a bounded ring.
            for record in reversed(data.get("console") or []):
                if hasattr(record, "model_dump"):
                    record = record.model_dump()
                if isinstance(record, dict):
                    for key in ("command", "output"):
                        append_bounded(record.get(key))
                if remaining_text_chars <= 0:
                    break
        paths = self._extract_artifact_paths(
            "\n".join(text_parts),
            max_paths=self._MAX_AUTO_ARTIFACT_CANDIDATES + 1,
        )
        if len(paths) > self._MAX_AUTO_ARTIFACT_CANDIDATES:
            logger.warning(
                "Shell artifact candidate limit reached: agent_id=%s count=%s limit=%s",
                self._agent_id,
                len(paths),
                self._MAX_AUTO_ARTIFACT_CANDIDATES,
            )
        if deadline is None:
            deadline = (
                asyncio.get_running_loop().time()
                + self._ARTIFACT_SYNC_TIMEOUT_SECONDS
            )
        for path in paths[: self._MAX_AUTO_ARTIFACT_CANDIDATES]:
            _, timed_out = await self._sync_auto_artifact_before_deadline(
                path,
                deadline,
                source="shell",
                generated=True,
            )
            if timed_out:
                break
    

    # TODO: refactor this function
    async def _handle_tool_event(self, event: ToolEvent):
        """Generate tool content"""
        try:
            if event.status == ToolStatus.CALLED:
                if event.tool_name == "browser":
                    if not self._has_artifact_task_capacity():
                        logger.error(
                            "Skipping browser screenshot at process "
                            "enrichment limit: agent_id=%s",
                            self._agent_id,
                        )
                        event.tool_content = BrowserToolContent(screenshot="")
                        return
                    screenshot_task = asyncio.create_task(
                        self._get_browser_screenshot()
                    )
                    self._track_artifact_cleanup_task(screenshot_task)
                    try:
                        screenshot = await asyncio.wait_for(
                            asyncio.shield(screenshot_task),
                            timeout=self._ARTIFACT_SYNC_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        if (
                            not screenshot_task.done()
                            and screenshot_task.cancelling() == 0
                        ):
                            screenshot_task.cancel()
                        logger.warning(
                            "Timed out preparing browser screenshot: "
                            "agent_id=%s",
                            self._agent_id,
                        )
                        screenshot = ""
                    except asyncio.CancelledError:
                        if (
                            not screenshot_task.done()
                            and screenshot_task.cancelling() == 0
                        ):
                            screenshot_task.cancel()
                        raise
                    event.tool_content = BrowserToolContent(
                        screenshot=screenshot
                    )
                elif event.tool_name == "preview":
                    result_data = (
                        event.function_result.data
                        if event.function_result
                        and getattr(event.function_result, "data", None)
                        else {}
                    )
                    event.tool_content = PreviewToolContent(
                        url=result_data.get("url") or event.function_args.get("url", ""),
                        title=result_data.get("title") or event.function_args.get("title"),
                    )
                elif event.tool_name == "search":
                    search_results: ToolResult[SearchResults] = event.function_result
                    logger.debug(
                        "Search tool completed: agent_id=%s result_count=%s",
                        self._agent_id,
                        len(search_results.data.results),
                    )
                    event.tool_content = SearchToolContent(results=search_results.data.results)
                elif event.tool_name == "shell":
                    shell_result = None
                    deadline = (
                        asyncio.get_running_loop().time()
                        + self._ARTIFACT_SYNC_TIMEOUT_SECONDS
                    )
                    if "id" in event.function_args:
                        try:
                            shell_result = await asyncio.wait_for(
                                self._sandbox.view_shell(
                                    event.function_args["id"],
                                    console=True,
                                ),
                                timeout=max(
                                    0.001,
                                    deadline
                                    - asyncio.get_running_loop().time(),
                                ),
                            )
                            event.tool_content = ShellToolContent(
                                console=shell_result.data.get("console", [])
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "Timed out preparing shell tool content: "
                                "agent_id=%s",
                                self._agent_id,
                            )
                            event.tool_content = ShellToolContent(
                                console="(Console preview timed out)"
                            )
                    else:
                        event.tool_content = ShellToolContent(console="(No Console)")
                    await self._sync_shell_artifacts(
                        event,
                        shell_result,
                        deadline=deadline,
                    )
                elif event.tool_name == "file":
                    if "file" in event.function_args:
                        file_path = event.function_args["file"]
                        deadline = (
                            asyncio.get_running_loop().time()
                            + self._ARTIFACT_SYNC_TIMEOUT_SECONDS
                        )
                        result_data = (
                            event.function_result.data
                            if event.function_result
                            and isinstance(event.function_result.data, dict)
                            else {}
                        )
                        file_content = result_data.get("content")
                        if not isinstance(file_content, str):
                            remaining = (
                                deadline - asyncio.get_running_loop().time()
                            )
                            if remaining > 0:
                                try:
                                    file_read_result = await asyncio.wait_for(
                                        self._sandbox.file_read(file_path),
                                        timeout=remaining,
                                    )
                                    file_content = file_read_result.data.get(
                                        "content",
                                        "",
                                    )
                                except asyncio.TimeoutError:
                                    logger.warning(
                                        "Timed out preparing file tool content: "
                                        "agent_id=%s",
                                        self._agent_id,
                                    )
                                    file_content = "(Content preview timed out)"
                        if not isinstance(file_content, str):
                            file_content = "(No Content)"
                        event.tool_content = FileToolContent(content=file_content)
                        if event.function_name in self._GENERATING_FILE_FUNCTIONS:
                            await self._sync_auto_artifact_before_deadline(
                                file_path,
                                deadline,
                                source="file tool",
                                generated=True,
                            )
                    else:
                        event.tool_content = FileToolContent(content="(No Content)")
                elif event.tool_name == "mcp":
                    logger.debug(
                        "Processing MCP tool event: agent_id=%s has_result=%s",
                        self._agent_id,
                        event.function_result is not None,
                    )
                    if event.function_result:
                        if hasattr(event.function_result, 'data') and event.function_result.data:
                            event.tool_content = McpToolContent(result=event.function_result.data)
                        elif hasattr(event.function_result, 'success') and event.function_result.success:
                            result_data = event.function_result.model_dump() if hasattr(event.function_result, 'model_dump') else str(event.function_result)
                            event.tool_content = McpToolContent(result=result_data)
                        else:
                            event.tool_content = McpToolContent(result=str(event.function_result))
                    else:
                        logger.warning("MCP tool: No function_result found")
                        event.tool_content = McpToolContent(result="No result available")
                    
                    logger.debug(
                        "MCP tool content prepared: agent_id=%s has_content=%s",
                        self._agent_id,
                        event.tool_content is not None,
                    )
                else:
                    logger.warning(f"Agent {self._agent_id} received unknown tool event: {event.tool_name}")
        except Exception as e:
            logger.error(
                "Agent %s failed to generate tool content: %s",
                self._agent_id,
                safe_exception_summary(e),
            )

    async def run(self, task: Task) -> None:
        """Use durable consumer-group execution when a turn repository exists."""
        if getattr(self, "_turn_submission_repository", None) is not None:
            await self._run_durable(task)
            return
        await self._run_legacy(task)

    async def _ack_input(self, task: Task, transport_id: str) -> None:
        ack = getattr(task.input_stream, "ack", None)
        if not callable(ack):
            raise RuntimeError("Durable input stream does not support XACK")
        await ack(self._INPUT_CONSUMER_GROUP, transport_id)

    async def _try_ack_input(self, task: Task, transport_id: str) -> bool:
        """Best-effort XACK without misclassifying committed work as failed."""
        try:
            await self._ack_input(task, transport_id)
            return True
        except Exception as exc:
            logger.warning(
                "Durable input acknowledgement deferred: agent_id=%s "
                "session_id=%s transport_id=%s error=%s",
                self._agent_id,
                self._session_id,
                transport_id,
                safe_exception_summary(exc),
            )
            return False

    async def _renew_claim_loop(
        self,
        submission_id: str,
        owner: str,
        parent_task: asyncio.Task,
        renewal_lost: asyncio.Event,
    ) -> None:
        while not parent_task.done():
            await asyncio.sleep(self._claim_renew_seconds)
            try:
                renewed = await self._turn_submission_repository.renew_claim(
                    self._session_id,
                    submission_id,
                    owner=owner,
                    claim_until=datetime.now(UTC)
                    + timedelta(seconds=self._claim_seconds),
                )
            except Exception:
                renewed = False
            if not renewed:
                renewal_lost.set()
                parent_task.cancel("mongo_claim_lost")
                return

    async def _terminal_is_persisted(self, submission_id: str) -> bool:
        current = await self._turn_submission_repository.find(
            self._session_id, submission_id
        )
        return bool(current and current.state in TERMINAL_TURN_STATES)

    async def _finalize_cancelled_turn(
        self,
        task: Task,
        transport_id: str,
        submission_id: str,
        owner: str,
    ) -> bool:
        """Commit explicit cancellation and retire its transport entry."""

        # A stable ID makes a partial outbox/Session projection idempotent
        # across retries. Cancellation safety does not depend on that
        # non-authoritative projection: owner-CAS must still run if it fails.
        done_event = DoneEvent(id=f"{submission_id}:cancelled")
        output_persisted = False
        try:
            done_event = await self._put_and_add_event(
                task, done_event, turn_id=submission_id
            )
            output_persisted = True
        except Exception as exc:
            logger.warning(
                "Cancellation output projection failed before terminal CAS: "
                "agent_id=%s session_id=%s submission_id=%s error=%s",
                self._agent_id,
                self._session_id,
                submission_id,
                safe_exception_summary(exc),
            )

        terminal_state: Optional[TurnSubmissionState] = None
        retry_delay = 0.05
        while terminal_state is None:
            try:
                committed = (
                    await self._turn_submission_repository.mark_terminal(
                        self._session_id,
                        submission_id,
                        owner=owner,
                        state=TurnSubmissionState.CANCELLED,
                        terminal_event_id=(
                            done_event.id if output_persisted else None
                        ),
                        error="Execution was cancelled",
                    )
                )
            except Exception as exc:
                committed = False
                logger.warning(
                    "Cancellation terminal CAS unavailable; retaining owner "
                    "and retrying: agent_id=%s session_id=%s "
                    "submission_id=%s error=%s",
                    self._agent_id,
                    self._session_id,
                    submission_id,
                    safe_exception_summary(exc),
                )
            if committed:
                terminal_state = TurnSubmissionState.CANCELLED
                break

            try:
                current = await self._turn_submission_repository.find(
                    self._session_id, submission_id
                )
            except Exception as exc:
                logger.warning(
                    "Cancellation terminal state could not be reconciled; "
                    "retrying: agent_id=%s session_id=%s submission_id=%s "
                    "error=%s",
                    self._agent_id,
                    self._session_id,
                    submission_id,
                    safe_exception_summary(exc),
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 1.0)
                continue
            if current is not None and current.state in TERMINAL_TURN_STATES:
                terminal_state = current.state
                break
            if (
                current is None
                or current.state != TurnSubmissionState.RUNNING
                or current.claim_owner != owner
            ):
                raise RuntimeError(
                    "Lost durable ownership before cancellation commit"
                )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 1.0)

        if not output_persisted and terminal_state == TurnSubmissionState.CANCELLED:
            try:
                await self._put_and_add_event(
                    task, done_event, turn_id=submission_id
                )
            except Exception as exc:
                # Terminal turn state is authoritative. SSE replay synthesizes
                # a terminal event if this optional history repair still fails.
                logger.warning(
                    "Cancellation output repair deferred after terminal CAS: "
                    "agent_id=%s session_id=%s submission_id=%s error=%s",
                    self._agent_id,
                    self._session_id,
                    submission_id,
                    safe_exception_summary(exc),
                )
        # Terminal Mongo state commits before XACK. An ACK outage is safe: a
        # later delivery observes the terminal row and retries only the ACK.
        acknowledged = await self._try_ack_input(task, transport_id)
        await self._sync_durable_session_status()
        return acknowledged

    async def _sync_durable_session_status(
        self,
        *,
        idle_status: SessionStatus = SessionStatus.COMPLETED,
    ) -> None:
        """Project durable turn truth onto the legacy Session status field.

        More than one accepted turn can belong to a session.  A worker that
        finishes one turn must therefore never blindly mark the whole session
        completed while another turn is queued or running.  The route layer
        also derives its response from the turn repository, so this field is a
        compatibility projection rather than the concurrency authority.
        """
        try:
            active = await self._turn_submission_repository.list_active(
                self._session_id,
                user_id=self._user_id,
            )
            if any(turn.state == TurnSubmissionState.RUNNING for turn in active):
                status = SessionStatus.RUNNING
            elif active:
                status = SessionStatus.PENDING
            else:
                status = idle_status
            await self._session_repository.update_status(self._session_id, status)
        except Exception as exc:
            # The durable turn state is authoritative and the session route
            # derives its effective status from it.  A failure in this legacy
            # projection must not turn an already committed Done event into a
            # later Error event or prevent XACK.
            logger.warning(
                "Could not project durable session status: agent_id=%s "
                "session_id=%s error=%s",
                self._agent_id,
                self._session_id,
                safe_exception_summary(exc),
            )

    async def _quarantine_or_fail_input(
        self,
        task: Task,
        transport_id: str,
        event_str: str,
        *,
        reason: str,
        submission_id: Optional[str] = None,
    ) -> bool:
        """Terminalize an associated turn or dead-letter an unknown poison row."""
        if submission_id:
            current = await self._turn_submission_repository.find(
                self._session_id, submission_id
            )
            if current is not None:
                terminalized = await self._turn_submission_repository.mark_unclaimed_terminal(
                    self._session_id,
                    submission_id,
                    state=TurnSubmissionState.FAILED,
                    error=reason,
                )
                if terminalized or await self._terminal_is_persisted(submission_id):
                    await self._sync_durable_session_status()
                    return await self._try_ack_input(task, transport_id)
                # A valid worker may already own the turn. Quarantine this bad
                # duplicate without changing that running claim.
        quarantine = getattr(task.input_stream, "quarantine", None)
        if not callable(quarantine):
            raise RuntimeError(
                "Malformed input cannot be associated or quarantined"
            )
        quarantined = await quarantine(
            self._INPUT_CONSUMER_GROUP,
            transport_id,
            reason=reason,
            payload_digest=hashlib.sha256(
                (event_str or "").encode("utf-8", errors="replace")
            ).hexdigest(),
        )
        return bool(quarantined)

    async def _process_durable_entry(
        self,
        task: Task,
        transport_id: str,
        event_str: str,
    ) -> bool:
        """Claim and execute one entry; return False when it must stay pending."""
        try:
            input_event = TypeAdapter(AgentEvent).validate_json(event_str)
        except Exception:
            raw_submission_id = None
            try:
                raw = json.loads(event_str)
                if isinstance(raw, dict):
                    raw_submission_id = raw.get("turn_id") or raw.get("id")
            except Exception:
                pass
            logger.error(
                "Quarantining malformed server-authored input: agent_id=%s session_id=%s",
                self._agent_id,
                self._session_id,
            )
            return await self._quarantine_or_fail_input(
                task,
                transport_id,
                event_str,
                reason="Malformed durable input event",
                submission_id=(
                    str(raw_submission_id) if raw_submission_id else None
                ),
            )
        if not isinstance(input_event, MessageEvent):
            logger.warning(
                "Discarding non-message input: agent_id=%s session_id=%s event_type=%s",
                self._agent_id,
                self._session_id,
                type(input_event).__name__,
            )
            return await self._quarantine_or_fail_input(
                task,
                transport_id,
                event_str,
                reason=f"Unexpected durable input type {type(input_event).__name__}",
                submission_id=input_event.turn_id or input_event.id,
            )

        submission_id = input_event.turn_id or input_event.id
        if not submission_id or input_event.id != submission_id:
            logger.error(
                "Discarding input with inconsistent logical ID: agent_id=%s session_id=%s",
                self._agent_id,
                self._session_id,
            )
            return await self._quarantine_or_fail_input(
                task,
                transport_id,
                event_str,
                reason="Inconsistent durable logical turn identifier",
                submission_id=submission_id,
            )

        # Authorization and runtime ownership are re-read from Mongo before
        # claim and before any sandbox/model/tool side effect.
        session = await self._session_repository.find_by_id_and_user_id(
            self._session_id, self._user_id
        )
        if (
            session is None
            or session.agent_id != self._agent_id
            or session.deleting
            or session.sandbox_destroying
            or session.sandbox_id != self._sandbox_id
            or session.task_sandbox_id != self._sandbox_id
        ):
            terminalized = await self._turn_submission_repository.mark_unclaimed_terminal(
                self._session_id,
                submission_id,
                state=TurnSubmissionState.CANCELLED,
                error=(
                    "Session lifecycle or sandbox generation no longer "
                    "matches this queued turn"
                ),
            )
            if terminalized or await self._terminal_is_persisted(submission_id):
                return await self._try_ack_input(task, transport_id)
            return False
        if session.task_id != task.id:
            current = await self._turn_submission_repository.find(
                self._session_id, submission_id
            )
            if current is None:
                return await self._quarantine_or_fail_input(
                    task,
                    transport_id,
                    event_str,
                    reason="Stale task input has no durable turn",
                )
            if current.state in TERMINAL_TURN_STATES:
                return await self._try_ack_input(task, transport_id)
            if current.task_id and current.task_id != task.id:
                # The logical turn has been rebound to the replacement task.
                # Retire only this stale transport copy; the old worker has no
                # authority to cancel any replacement-task turns.
                return await self._try_ack_input(task, transport_id)
            terminalized = await self._turn_submission_repository.mark_unclaimed_terminal(
                self._session_id,
                submission_id,
                state=TurnSubmissionState.CANCELLED,
                error="Obsolete task stream was retired before execution",
            )
            if terminalized or await self._terminal_is_persisted(submission_id):
                await self._sync_durable_session_status()
                return await self._try_ack_input(task, transport_id)
            # A RUNNING turn remains owned until its claim expires; deleting
            # its only transport row here would prevent failed_unknown repair.
            return False

        owner = f"{self._worker_id}:{transport_id}"
        claim, pending_cancellation = await self._claim_turn_to_known_outcome(
            submission_id,
            task_id=task.id,
            owner=owner,
        )
        if claim.decision == TurnClaimDecision.ACK:
            if pending_cancellation is not None:
                ack_task = asyncio.create_task(
                    self._try_ack_input(task, transport_id)
                )
                await self._await_task_to_known_outcome(ack_task)
                raise pending_cancellation
            await self._sync_durable_session_status()
            return await self._try_ack_input(task, transport_id)
        if claim.decision == TurnClaimDecision.RETRY:
            if pending_cancellation is not None:
                raise pending_cancellation
            return False

        parent_task = asyncio.current_task()
        if parent_task is None:
            raise RuntimeError("Durable runner has no owning asyncio task")
        renewal_lost = asyncio.Event()
        renewer: Optional[asyncio.Task[None]] = None
        # Be conservative: every operation after claim can touch an external
        # provider or user artifact. A crash from this point is failed_unknown
        # and must never be automatically replayed.
        side_effects_started = False
        terminal_event: Optional[BaseEvent] = None
        try:
            # Once EXECUTE is returned, every await is inside this ownership
            # cleanup boundary. In particular, cancellation during the legacy
            # Session status projection must not strand a RUNNING turn.
            renewer = asyncio.create_task(
                self._renew_claim_loop(
                    submission_id, owner, parent_task, renewal_lost
                )
            )
            if pending_cancellation is not None:
                raise pending_cancellation
            await self._sync_durable_session_status(
                idle_status=SessionStatus.RUNNING
            )
            current_session = (
                await self._session_repository.find_by_id_and_user_id(
                    self._session_id, self._user_id
                )
            )
            if (
                current_session is None
                or current_session.agent_id != self._agent_id
                or current_session.task_id != task.id
                or current_session.deleting
                or current_session.sandbox_destroying
                or current_session.sandbox_id != self._sandbox_id
                or current_session.task_sandbox_id != self._sandbox_id
            ):
                # The Mongo turn claim is ours, but the Session lifecycle or
                # runtime generation changed while that claim was in flight.
                # Route through the cancellation finalizer so repeated task
                # cancellation cannot interrupt owner-CAS settlement, then
                # stop this stale worker instead of draining more entries.
                raise asyncio.CancelledError(
                    "session_lifecycle_changed"
                )
            # Mongo claim is already committed. An unavailable Mongo claim never
            # reaches any of these external operations.
            side_effects_started = True
            await self._sandbox.ensure_sandbox()
            await self._mcp_tool.initialized(
                await self._mcp_repository.get_mcp_config()
            )
            await self._sync_message_attachments_to_sandbox(input_event)
            logger.info(
                "Agent received durable input: agent_id=%s session_id=%s submission_id=%s "
                "attachment_count=%s",
                self._agent_id,
                self._session_id,
                submission_id,
                len(input_event.attachments or []),
            )
            message_obj = Message(
                message=input_event.message or "",
                attachments=[
                    attachment.file_path
                    for attachment in (input_event.attachments or [])
                ],
            )
            async for output_event in self._run_flow(
                message_obj,
                resumes_waiting=claim.turn.resumes_waiting,
            ):
                persisted_event = await self._put_and_add_event(
                    task, output_event, turn_id=submission_id
                )
                if isinstance(output_event, TitleEvent):
                    await self._session_repository.update_title(
                        self._session_id, output_event.title
                    )
                elif isinstance(output_event, MessageEvent):
                    await self._session_repository.update_latest_message(
                        self._session_id,
                        output_event.message,
                        output_event.timestamp,
                    )
                    await self._session_repository.increment_unread_message_count(
                        self._session_id
                    )
                if isinstance(output_event, (DoneEvent, ErrorEvent, WaitEvent)):
                    terminal_event = persisted_event or output_event
                if isinstance(output_event, WaitEvent):
                    break

            if terminal_event is None:
                terminal_event = await self._put_and_add_event(
                    task, DoneEvent(), turn_id=submission_id
                )
            committed = await self._turn_submission_repository.mark_terminal(
                self._session_id,
                submission_id,
                owner=owner,
                state=TurnSubmissionState.COMPLETED,
                terminal_event_id=terminal_event.id,
            )
            if not committed and not await self._terminal_is_persisted(
                submission_id
            ):
                raise RuntimeError("Lost durable ownership before terminal commit")
            await self._sync_durable_session_status(
                idle_status=(
                    SessionStatus.WAITING
                    if isinstance(terminal_event, WaitEvent)
                    else SessionStatus.COMPLETED
                )
            )
            # Terminal Mongo state commits before XACK. An ACK loss merely
            # causes a duplicate entry that the next claim classifies as ACK.
            return await self._try_ack_input(task, transport_id)
        except asyncio.CancelledError as exc:
            cancellation_reason = str(exc.args[0]) if exc.args else ""
            if renewal_lost.is_set() or cancellation_reason in {
                "claim_lost",
                "mongo_claim_lost",
            }:
                logger.error(
                    "Execution fencing was lost: agent_id=%s session_id=%s "
                    "submission_id=%s; leaving turn unacked for failed_unknown",
                    self._agent_id,
                    self._session_id,
                    submission_id,
                )
                # Propagate the fencing signal through _run_durable to the
                # task backend. Swallowing it would let this runner reclaim
                # and execute a later turn after its Redis/Mongo ownership was
                # already lost.
                raise

            finalizer = asyncio.create_task(
                self._finalize_cancelled_turn(
                    task,
                    transport_id,
                    submission_id,
                    owner,
                )
            )
            # A second stop/delete request must not interrupt the owner-CAS or
            # let task acknowledgement overtake terminal turn persistence.
            await self._await_task_to_known_outcome(finalizer)
            # This is an explicit stop/delete cancellation, not a claim/control
            # fencing loss. The terminal state and XACK above must commit first,
            # then cancellation propagates so the backend stops draining turns.
            raise
        except Exception as exc:
            summary = safe_exception_summary(exc)
            logger.error(
                "Durable turn failed: agent_id=%s session_id=%s submission_id=%s "
                "error=%s",
                self._agent_id,
                self._session_id,
                submission_id,
                summary,
            )
            error_event = await self._put_and_add_event(
                task,
                ErrorEvent(error=f"Task error: {summary}"),
                turn_id=submission_id,
            )
            terminal_state = (
                TurnSubmissionState.FAILED_UNKNOWN
                if side_effects_started
                else TurnSubmissionState.FAILED
            )
            committed = await self._turn_submission_repository.mark_terminal(
                self._session_id,
                submission_id,
                owner=owner,
                state=terminal_state,
                terminal_event_id=error_event.id,
                error=summary,
            )
            acknowledged = False
            if committed or await self._terminal_is_persisted(submission_id):
                acknowledged = await self._try_ack_input(task, transport_id)
            await self._sync_durable_session_status()
            return acknowledged
        finally:
            if renewer is not None:
                renewer.cancel()
                await asyncio.gather(renewer, return_exceptions=True)

    async def _run_durable(self, task: Task) -> None:
        read_group = getattr(task.input_stream, "read_group", None)
        if not callable(read_group):
            raise RuntimeError(
                "Durable input stream does not support consumer groups"
            )
        logger.info(
            "Durable agent worker started: agent_id=%s session_id=%s task_id=%s",
            self._agent_id,
            self._session_id,
            task.id,
        )
        empty_reads = 0
        while empty_reads < 2:
            transport_id, event_str = await read_group(
                self._INPUT_CONSUMER_GROUP,
                self._worker_id,
                min_idle_ms=self._INPUT_CLAIM_IDLE_MS,
                block_ms=1_000,
            )
            if event_str is None:
                empty_reads += 1
                continue
            empty_reads = 0
            acknowledged = await self._process_durable_entry(
                task, transport_id, event_str
            )
            if not acknowledged:
                # Let the entry become idle before XAUTOCLAIM. For an expired
                # running claim this loop persists failed_unknown and ACKs it;
                # it never replays external side effects.
                await asyncio.sleep(self._INPUT_CLAIM_IDLE_MS / 1000)

    async def _run_legacy(self, task: Task) -> None:
        """Process agent's message queue and run the agent's flow"""
        active_turn_id: Optional[str] = None
        try:
            logger.info(f"Agent {self._agent_id} message processing task started")
            await self._sandbox.ensure_sandbox()
            await self._mcp_tool.initialized(await self._mcp_repository.get_mcp_config())
            while not await task.input_stream.is_empty():
                event = await self._pop_event(task)
                if not isinstance(event, MessageEvent):
                    logger.warning(
                        "Agent %s ignored input event type=%s",
                        self._agent_id,
                        type(event).__name__,
                    )
                    continue
                active_turn_id = event.id
                message = event.message or ""
                await self._sync_message_attachments_to_sandbox(event)
                    
                logger.info(
                    "Agent received input: agent_id=%s session_id=%s attachment_count=%s",
                    self._agent_id,
                    self._session_id,
                    len(event.attachments or []),
                )

                message_obj = Message(
                    message=message,
                    attachments=[
                        attachment.file_path
                        for attachment in (event.attachments or [])
                    ],
                )
                
                async for event in self._run_flow(message_obj):
                    await self._put_and_add_event(
                        task, event, turn_id=active_turn_id
                    )
                    if isinstance(event, TitleEvent):
                        await self._session_repository.update_title(self._session_id, event.title)
                    elif isinstance(event, MessageEvent):
                        await self._session_repository.update_latest_message(self._session_id, event.message, event.timestamp)
                        await self._session_repository.increment_unread_message_count(self._session_id)
                    elif isinstance(event, WaitEvent):
                        await self._session_repository.update_status(self._session_id, SessionStatus.WAITING)
                        return

            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)
        except asyncio.CancelledError:
            logger.info(f"Agent {self._agent_id} task cancelled")
            await self._put_and_add_event(
                task, DoneEvent(), turn_id=active_turn_id
            )
            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)
        except Exception as e:
            logger.error(
                "Agent %s task encountered exception: %s",
                self._agent_id,
                safe_exception_summary(e),
            )
            
            # If debugger is attached, trigger breakpoint for debugging
            # You can also manually set ENABLE_DEBUG_BREAK=1 environment variable
            if debugpy.is_client_connected() or os.getenv('ENABLE_DEBUG_BREAK'):
                logger.debug("Debugger detected, triggering breakpoint")
                debugpy.breakpoint()  # This will pause execution if a debugger is attached
            
            await self._put_and_add_event(
                task,
                ErrorEvent(
                    error=f"Task error: {safe_exception_summary(e)}"
                ),
                turn_id=active_turn_id,
            )
            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)
    
    async def _run_flow(
        self,
        message: Message,
        resumes_waiting: Optional[bool] = None,
    ) -> AsyncGenerator[BaseEvent, None]:
        """Process a single message through the agent's flow and yield events"""
        if not message.message:
            logger.warning(f"Agent {self._agent_id} received empty message")
            yield ErrorEvent(error="No message")
            return

        async for event in self._flow.run(
            message,
            resumes_waiting=resumes_waiting,
        ):
            if isinstance(event, ToolEvent):
                # TODO: move to tool function
                await self._handle_tool_event(event)
            elif isinstance(event, MessageEvent):
                await self._sync_message_attachments_to_storage(event)
            yield event

        logger.info(f"Agent {self._agent_id} completed processing one message")

    
    async def on_done(self, task: Task) -> None:
        """Called when the task is done"""
        logger.info(f"Agent {self._agent_id} task done")


    async def _close_resources_after_enrichment(self) -> None:
        """Close handles only after their owned enrichment stops using them."""
        initial_owned_tasks = tuple(
            task
            for task in getattr(self, "_artifact_cleanup_tasks", set())
            if not task.done()
        )
        for task in initial_owned_tasks:
            # A first cancel moves PREPARE/upload work into its bounded
            # rollback path.  Never issue a second cancel while rollback is in
            # progress; post-upload publishing detaches itself from ownership.
            if task.cancelling() == 0:
                task.cancel()

        ownership_changed = self._artifact_owner_change_event()
        while True:
            ownership_changed.clear()
            owned_tasks = tuple(
                task
                for task in getattr(self, "_artifact_cleanup_tasks", set())
                if not task.done()
            )
            if not owned_tasks:
                break
            change_waiter = asyncio.create_task(ownership_changed.wait())
            done, _ = await asyncio.wait(
                (*owned_tasks, change_waiter),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if change_waiter not in done:
                change_waiter.cancel()
                await asyncio.gather(change_waiter, return_exceptions=True)

        resources = (
            ("browser", self._browser, ("cleanup", "aclose", "close")),
            ("MCP", self._mcp_tool, ("cleanup", "aclose", "close")),
            ("LLM", self._llm, ("aclose", "cleanup", "close")),
            ("sandbox", self._sandbox, ("aclose",)),
        )
        cleanup_lease = getattr(self, "_cleanup_lease", None)
        if cleanup_lease is None:
            raise RuntimeError("Agent runner has no cleanup lease")
        await _close_resource_bundle(
            resources,
            cleanup_lease=cleanup_lease,
            agent_id=self._agent_id,
            attempt_timeout_seconds=(
                self._ARTIFACT_CLOSE_DRAIN_TIMEOUT_SECONDS
            ),
        )
        self._closed = True

    async def aclose(self) -> None:
        """Bounded, idempotent non-destructive runner handle cleanup."""
        async with self._close_lock:
            if self._closed:
                return
            close_task = getattr(self, "_close_task", None)
            if close_task is None:
                cleanup_lease = getattr(self, "_cleanup_lease", None)
                if cleanup_lease is None:
                    # Direct test/legacy construction has no factory phase.
                    # Production factories reserve before allocating handles.
                    cleanup_lease = _reserve_runner_cleanup_lease()
                    self._cleanup_lease = cleanup_lease
                self._closing = True
                close_task = asyncio.create_task(
                    self._close_resources_after_enrichment()
                )
                self._close_task = close_task
                _retain_deferred_close_task(
                    close_task,
                    agent_id=self._agent_id,
                )

        done, _ = await asyncio.wait(
            (close_task,),
            timeout=self._ARTIFACT_CLOSE_DRAIN_TIMEOUT_SECONDS,
        )
        if not done:
            logger.warning(
                "Agent runner close deferred until enrichment finishes: "
                "agent_id=%s",
                self._agent_id,
            )
            return
        close_task.result()


    async def destroy(self) -> None:
        """Compatibility close; provider deletion belongs to the provisioner."""
        await self.aclose()


class AgentTaskRunnerFactory(TaskRunnerFactory):
    """Rebuilds an AgentTaskRunner from serializable parameters.

    Task backends only carry JSON-serializable parameters (session_id,
    agent_id, user_id, sandbox_id) between the process that creates a task
    and the process that executes it. This factory reconstructs the runner
    with live dependencies (sandbox, browser, repositories) on the execution
    side, which may be the API process (local backend) or a worker process
    (e.g. Celery backend).
    """

    def __init__(
        self,
        agent_repository: AgentRepository,
        session_repository: SessionRepository,
        sandbox_cls: Type[Sandbox],
        file_storage: FileStorage,
        mcp_repository: MCPRepository,
        llm_factory: Optional[LLMFactory] = None,
        search_engine: Optional[SearchEngine] = None,
        llm: Optional[LLM] = None,
        turn_submission_repository: Optional[TurnSubmissionRepository] = None,
    ):
        self._agent_repository = agent_repository
        self._session_repository = session_repository
        self._sandbox_cls = sandbox_cls
        self._file_storage = file_storage
        self._mcp_repository = mcp_repository
        self._llm_factory = llm_factory
        self._llm = llm
        self._search_engine = search_engine
        self._turn_submission_repository = turn_submission_repository

    @staticmethod
    def build_params(
        session_id: str,
        agent_id: str,
        user_id: str,
        sandbox_id: str,
        task_sandbox_id: Optional[str],
        sandbox_provider: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "session_id": session_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "sandbox_id": sandbox_id,
            "task_sandbox_id": task_sandbox_id,
            "sandbox_provider": sandbox_provider,
        }

    async def recover_factory_failure(
        self,
        params: Dict[str, Any],
        *,
        task_id: str,
        error: str,
    ) -> bool:
        """Durably resolve local queued work after repeated rebuild failure."""
        if self._turn_submission_repository is None:
            return False
        return await self._turn_submission_repository.recover_factory_failure_for_task(
            params["session_id"],
            user_id=params["user_id"],
            task_id=task_id,
            error=error,
        )

    async def create_runner(self, params: Dict[str, Any]) -> AgentTaskRunner:
        sandbox_id = params["sandbox_id"]
        expected_task_id = str(params.get("task_id") or "").strip()
        expected_task_sandbox_id = str(
            params.get("task_sandbox_id") or ""
        ).strip()
        # Do not turn a transient provider/network lookup error into a second
        # billable sandbox. Implementations return None only for authoritative
        # not-found and raise when the state is inconclusive.
        sandbox = None
        browser = None
        llm = None
        cleanup_lease: Optional[_RunnerCleanupLease] = None
        try:
            find_session = getattr(
                self._session_repository,
                "find_by_id_and_user_id",
                None,
            )
            if not callable(find_session):
                raise SandboxProvisioningRequiredError(
                    "Worker cannot verify persisted sandbox ownership"
                )
            session = await find_session(
                params["session_id"], params["user_id"]
            )
            configured_provider = (
                get_settings().sandbox_provider or "docker"
            ).strip().lower()
            parameter_provider = str(
                params.get("sandbox_provider") or ""
            ).strip().lower()
            persisted_provider = str(
                getattr(session, "sandbox_provider", "") or ""
            ).strip().lower()
            if (
                session is None
                or session.agent_id != params["agent_id"]
                or session.deleting
                or session.sandbox_destroying
                or session.sandbox_id != sandbox_id
                or not expected_task_id
                or session.task_id != expected_task_id
                or not expected_task_sandbox_id
                or expected_task_sandbox_id != sandbox_id
                or session.task_sandbox_id != expected_task_sandbox_id
                or not persisted_provider
                or persisted_provider != configured_provider
                or (
                    parameter_provider
                    and parameter_provider != persisted_provider
                )
            ):
                raise SandboxProvisioningRequiredError(
                    "Persisted sandbox ownership does not match this worker"
                )
            # Reserve before constructing the first live sandbox/browser/model
            # handle.  This synchronous operation is atomic on the loop and is
            # deliberately independent of the artifact shutdown admission
            # gate: cleanup capacity describes live handles, not enrichment.
            cleanup_lease = _reserve_runner_cleanup_lease()
            get_owned = getattr(self._sandbox_cls, "get_owned", None)
            if callable(get_owned):
                # Managed providers must verify that the deterministic
                # container/session belongs to the persisted chat before a
                # worker receives a live handle.  Legacy/custom providers
                # remain compatible only when they do not expose this API.
                sandbox = await get_owned(sandbox_id, session.id)
            else:
                sandbox = await self._sandbox_cls.get(sandbox_id)
            if not sandbox:
                # Worker processes do not own the session lifecycle lease and
                # therefore must never allocate a replacement. The API-side
                # provisioner will reconcile ownership and dispatch a task
                # carrying the replacement sandbox ID.
                raise SandboxProvisioningRequiredError(
                    "Persisted sandbox is missing; API provisioning is required"
                )
            if getattr(sandbox, "id", None) != sandbox_id:
                raise SandboxProvisioningRequiredError(
                    "Sandbox provider returned a different persisted runtime"
                )
            browser = await sandbox.get_browser()
            if not browser:
                raise RuntimeError(
                    f"Failed to get browser for Sandbox {sandbox_id}"
                )
            agent = await self._agent_repository.find_by_id(params["agent_id"])
            if agent is None:
                raise RuntimeError(
                    f"Agent configuration not found: {params['agent_id']}"
                )
            llm = (
                self._llm_factory.create(agent)
                if self._llm_factory
                else self._llm
            )
            if llm is None:
                raise RuntimeError(
                    "No LLMFactory configured for AgentTaskRunnerFactory"
                )
            return AgentTaskRunner(
                session_id=params["session_id"],
                agent_id=params["agent_id"],
                user_id=params["user_id"],
                sandbox_id=sandbox_id,
                sandbox=sandbox,
                browser=browser,
                agent_repository=self._agent_repository,
                session_repository=self._session_repository,
                file_storage=self._file_storage,
                mcp_repository=self._mcp_repository,
                llm=llm,
                search_engine=self._search_engine,
                turn_submission_repository=self._turn_submission_repository,
                cleanup_lease=cleanup_lease,
            )
        except BaseException:
            # A runner never took ownership, so release every constructed
            # client handle here.  The persisted sandbox itself is retained;
            # provider deletion belongs exclusively to its provisioner.
            if cleanup_lease is not None:
                cleanup_task = _start_resource_cleanup_bundle(
                    (
                        (
                            "browser",
                            browser,
                            ("cleanup", "aclose", "close"),
                        ),
                        ("LLM", llm, ("aclose", "cleanup", "close")),
                        ("sandbox", sandbox, ("aclose",)),
                    ),
                    cleanup_lease=cleanup_lease,
                    agent_id=str(params.get("agent_id") or "unknown"),
                    attempt_timeout_seconds=(
                        _RUNNER_CLEANUP_ATTEMPT_TIMEOUT_SECONDS
                    ),
                )
                done, _ = await asyncio.wait(
                    (cleanup_task,),
                    timeout=_RUNNER_CLEANUP_ATTEMPT_TIMEOUT_SECONDS,
                )
                if not done:
                    logger.error(
                        "Runner construction cleanup remains deferred: "
                        "agent_id=%s",
                        params.get("agent_id"),
                    )
            raise
