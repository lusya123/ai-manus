from typing import Any, Optional, AsyncGenerator, Awaitable, Callable, List
import asyncio
import hashlib
import json
import logging
import re
import uuid
from weakref import WeakValueDictionary
from datetime import UTC, datetime
from app.domain.models.session import Session, SessionStatus
from app.domain.external.sandbox import Sandbox
from app.domain.external.sandbox_provisioner import (
    SandboxProvisioner,
    SandboxProvisioningRequiredError,
)
from app.domain.external.agentbay_quota import AgentBayQuotaExceededError
from app.domain.external.search import SearchEngine
from app.domain.models.event import (
    AcceptedEvent,
    BaseEvent,
    ErrorEvent,
    DoneEvent,
    MessageEvent,
    WaitEvent,
    AgentEvent,
)
from pydantic import TypeAdapter
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.repositories.session_repository import SessionRepository
from app.domain.services.agent_task_runner import AgentTaskRunnerFactory
from app.domain.external.task import Task
from typing import Type
from app.domain.external.file import FileStorage
from app.domain.models.file import FileInfo
from app.domain.repositories.mcp_repository import MCPRepository
from app.domain.external.coordination import (
    SessionLifecycleLease,
    SessionLifecycleLeaseError,
)
from app.domain.models.turn_submission import (
    TERMINAL_TURN_STATES,
    TurnSubmission,
    TurnSubmissionState,
    TurnSubmissionUnavailableError,
)
from app.domain.repositories.turn_submission_repository import TurnSubmissionRepository
from app.domain.utils.error_reporting import safe_exception_summary

# Setup logging
logger = logging.getLogger(__name__)

class AgentDomainService:
    """
    Agent domain service, responsible for coordinating the work of planning agent and execution agent
    """
    
    _REDIS_STREAM_ID_PATTERN = re.compile(r"^(?:\$|\d+(?:-\d+)?)$")
    _TASK_CANCEL_TIMEOUT_SECONDS = 15.0

    def __init__(
        self,
        agent_repository: AgentRepository,
        session_repository: SessionRepository,
        sandbox_cls: Type[Sandbox],
        task_cls: Type[Task],
        file_storage: FileStorage,
        mcp_repository: MCPRepository,
        search_engine: Optional[SearchEngine] = None,
        session_lifecycle_lease: Optional[SessionLifecycleLease] = None,
        turn_submission_repository: Optional[TurnSubmissionRepository] = None,
        sandbox_provisioner: Optional[SandboxProvisioner] = None,
    ):
        self._repository = agent_repository
        self._session_repository = session_repository
        self._sandbox_cls = sandbox_cls
        self._search_engine = search_engine
        self._task_cls = task_cls
        self._file_storage = file_storage
        self._mcp_repository = mcp_repository
        self._session_lifecycle_lease = session_lifecycle_lease
        self._turn_submission_repository = turn_submission_repository
        self._sandbox_provisioner = sandbox_provisioner
        # Serialize message submission and resource allocation per session.
        # Weak values avoid retaining one lock forever for every historical
        # session while still giving all concurrent callers the same lock.
        self._session_locks: WeakValueDictionary[str, asyncio.Lock] = (
            WeakValueDictionary()
        )
        # Message submissions may enqueue quickly onto one task, but their
        # response readers must advance in submission order.  Each future is
        # the completion ticket for one turn; reconnect-only readers do not
        # participate in this chain.
        self._session_turn_tails: dict[str, asyncio.Future[None]] = {}
        logger.info("AgentDomainService initialization completed")

    @staticmethod
    def _attachment_value(attachment: Any, key: str) -> Optional[str]:
        if isinstance(attachment, dict):
            return attachment.get(key)
        if hasattr(attachment, "model_dump"):
            return attachment.model_dump().get(key)
        return getattr(attachment, key, None)

    def _to_file_infos(self, attachments: Optional[List[Any]]) -> Optional[List[FileInfo]]:
        if not attachments:
            return None
        result = []
        for attachment in attachments:
            file_id = self._attachment_value(attachment, "file_id")
            if not file_id:
                continue
            result.append(
                FileInfo(
                    file_id=file_id,
                    filename=self._attachment_value(attachment, "filename"),
                )
            )
        return result or None

    async def _canonicalize_owned_attachments(
        self,
        attachments: Optional[List[Any]],
        user_id: str,
    ) -> Optional[List[FileInfo]]:
        """Resolve attachment IDs through owner-scoped storage before accept.

        Chat attachment metadata is untrusted input. Persisting it before an
        ownership check would let a foreign file ID enter durable history and,
        when the session is shared, become a confused-deputy capability.
        """
        requested = self._to_file_infos(attachments)
        if not requested:
            return None

        canonical: List[FileInfo] = []
        for attachment in requested:
            try:
                stored = await self._file_storage.get_file_info(
                    attachment.file_id, user_id
                )
            except Exception as exc:
                logger.warning(
                    "Attachment ownership lookup failed: user_id=%s error=%s",
                    user_id,
                    safe_exception_summary(exc),
                )
                raise ValueError("Attachment not found or access denied") from exc
            if stored is None or stored.file_id != attachment.file_id:
                raise ValueError("Attachment not found or access denied")

            # Persist only storage-authored metadata needed by chat/history.
            # Never trust a request-supplied filename, owner, URL, or path.
            canonical.append(
                FileInfo(
                    file_id=stored.file_id,
                    filename=stored.filename,
                    content_type=stored.content_type,
                    size=stored.size,
                    upload_date=stored.upload_date,
                )
            )
        return canonical

    @classmethod
    def _is_redis_stream_id(cls, event_id: Optional[str]) -> bool:
        return bool(event_id and cls._REDIS_STREAM_ID_PATTERN.fullmatch(event_id))

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    async def run_session_lifecycle_exclusive(
        self,
        session_id: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run an enqueue/delete mutation under the cross-replica lease.

        Direct unit constructions may omit the distributed coordinator and
        retain the existing process-local behavior. The production composition
        root always injects the Redis implementation.
        """
        if self._session_lifecycle_lease is None:
            return await operation()
        return await self._session_lifecycle_lease.run_exclusive(
            session_id, operation
        )

    @staticmethod
    async def _get_latest_output_stream_id(task: Optional[Task]) -> Optional[str]:
        if not task:
            return None
        get_latest_id = getattr(task.output_stream, "get_latest_id", None)
        return await get_latest_id() if get_latest_id else None
            
    async def shutdown(self) -> None:
        """Clean up all Agent's resources"""
        logger.info("Starting to close all Agents")
        await self._task_cls.destroy()
        logger.info("All agents closed successfully")

    async def _persist_runtime_ownership(self, session: Session) -> None:
        """Persist runtime IDs without recreating a deleted session.

        The fallback keeps lightweight repository fakes backwards-compatible;
        the production Mongo repository always implements the non-upserting
        method from ``SessionRepository``.
        """
        update_runtime = getattr(
            self._session_repository, "update_runtime_ownership", None
        )
        if update_runtime is not None:
            await update_runtime(
                session.id,
                session.sandbox_id,
                session.task_id,
                session.sandbox_provider,
            )
            return
        await self._session_repository.save(session)

    @staticmethod
    async def _aclose_sandbox_handle(sandbox: Optional[Sandbox]) -> None:
        """Release a transient sandbox handle without deleting its resource."""
        close = getattr(sandbox, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception as exc:
                logger.warning(
                    "Failed to close transient sandbox handle: %s",
                    type(exc).__name__,
                )

    async def _ensure_sandbox_locked(self, session: Session) -> bool:
        """Ensure ownership under the caller's distributed lifecycle lease.

        Returns whether the persisted provider ID changed.  The returned live
        handle is needed only as proof that provisioning/link resolution
        succeeded; task workers reconstruct their own non-owning handle.
        """
        if self._sandbox_provisioner is None:
            raise SandboxProvisioningRequiredError(
                "No sandbox lifecycle provisioner is configured"
            )
        previous_id = session.sandbox_id
        sandbox = await self._sandbox_provisioner.ensure_locked(session)
        try:
            if not session.sandbox_id or sandbox.id != session.sandbox_id:
                raise RuntimeError(
                    "Sandbox provisioner returned inconsistent persisted ownership"
                )
            return previous_id is not None and previous_id != session.sandbox_id
        finally:
            await self._aclose_sandbox_handle(sandbox)

    async def _retire_replaced_task_stream(
        self,
        session: Session,
        *,
        preserve_submission_id: Optional[str],
    ) -> None:
        """Stop an old task before binding the session to replacement params."""
        if session.task_id:
            task = await self._get_task(session)
            if task is not None:
                await task.cancel()
                stopped = await task.wait_for_done(
                    self._TASK_CANCEL_TIMEOUT_SECONDS
                )
                if not stopped:
                    raise TimeoutError(
                        "obsolete task did not acknowledge cancellation"
                    )

        if self._turn_submission_repository is not None:
            await self._turn_submission_repository.cancel_queued(
                session.id,
                user_id=session.user_id,
                exclude_submission_id=preserve_submission_id,
            )
            if await self._turn_submission_repository.count_running(
                session.id, user_id=session.user_id
            ):
                raise RuntimeError(
                    "cannot replace a sandbox while a turn is still running"
                )

        session.task_id = None
        await self._persist_runtime_ownership(session)

    async def _create_task(
        self, session: Session, *, sandbox_ready: bool = False
    ) -> Task:
        """Create a new agent task after provisioner-owned lifecycle setup."""
        if not sandbox_ready:
            await self._ensure_sandbox_locked(session)
        task: Optional[Task] = None
        try:
            params = AgentTaskRunnerFactory.build_params(
                session_id=session.id,
                agent_id=session.agent_id,
                user_id=session.user_id,
                sandbox_id=session.sandbox_id,
                sandbox_provider=session.sandbox_provider,
            )
            task = self._task_cls.create(params)
            session.task_id = task.id
            await self._persist_runtime_ownership(session)
        except BaseException:
            # A task handle can enter an in-process registry before the
            # session update succeeds.  Cancel that unpublished handle so it
            # cannot become an orphan.  The persisted sandbox is deliberately
            # retained for a safe retry.
            if task is not None:
                try:
                    await task.cancel()
                    await task.wait_for_done(self._TASK_CANCEL_TIMEOUT_SECONDS)
                except Exception as exc:
                    logger.error(
                        "Failed to roll back unpublished task %s: %s",
                        task.id,
                        safe_exception_summary(exc),
                    )
            logger.info(
                "Retaining persisted sandbox %s after task creation failure",
                session.sandbox_id,
            )
            raise

        return task
        
    async def _get_task(self, session: Session) -> Optional[Task]:
        """Get a task for the given session"""

        task_id = session.task_id
        if not task_id:
            return None
        
        return await self._task_cls.get(task_id)

    @staticmethod
    def _canonical_attachment(attachment: FileInfo) -> dict[str, Optional[str]]:
        # The id is the stable client request field. Filename and all other
        # metadata are storage-authored and may legitimately change after a
        # turn is accepted, so they must not alter idempotency identity.
        return {"file_id": attachment.file_id}

    async def _add_event_once(
        self, session_id: str, event: BaseEvent
    ) -> BaseEvent:
        add_once = getattr(self._session_repository, "add_event_once", None)
        if callable(add_once):
            persisted = await add_once(session_id, event)
        else:
            persisted = await self._session_repository.add_event(session_id, event)
        return persisted if isinstance(persisted, BaseEvent) else event

    async def cancel_outstanding_turns(
        self, session_id: str, *, user_id: Optional[str] = None
    ) -> int:
        if self._turn_submission_repository is None:
            return 0
        return await self._turn_submission_repository.cancel_queued(
            session_id, user_id=user_id
        )

    async def _sync_durable_session_status(
        self,
        session_id: str,
        *,
        idle_status: SessionStatus = SessionStatus.COMPLETED,
    ) -> SessionStatus:
        """Project aggregate turn activity onto the legacy Session status."""
        if self._turn_submission_repository is None:
            return idle_status
        try:
            active = await self._turn_submission_repository.list_active(session_id)
            if any(turn.state == TurnSubmissionState.RUNNING for turn in active):
                status = SessionStatus.RUNNING
            elif active:
                status = SessionStatus.PENDING
            else:
                status = idle_status
            await self._session_repository.update_status(session_id, status)
            return status
        except Exception as exc:
            # Durable turn rows are authoritative and get_session derives its
            # effective status from them. Do not fail an accepted turn merely
            # because this backwards-compatible projection is unavailable.
            logger.warning(
                "Could not project durable session status: session_id=%s error=%s",
                session_id,
                safe_exception_summary(exc),
            )
            return idle_status

    async def _finalize_turns_after_task_stop(
        self, session_id: str, *, user_id: Optional[str] = None
    ) -> None:
        if self._turn_submission_repository is None:
            return
        # Running turns must reach terminal state from the runner before its
        # task acknowledgement. Only never-started queued turns are cancelled
        # here, so no quota is released while side effects can continue.
        await self._turn_submission_repository.cancel_queued(
            session_id, user_id=user_id
        )
        if await self._turn_submission_repository.count_running(
            session_id, user_id=user_id
        ):
            raise RuntimeError(
                "Task stopped without persisting a terminal running turn"
            )
        await self._sync_durable_session_status(session_id)

    def _runner_params(self, session: Session) -> dict[str, Any]:
        if not session.sandbox_id:
            raise TurnSubmissionUnavailableError(
                "Persisted task has no sandbox ownership"
            )
        return AgentTaskRunnerFactory.build_params(
            session_id=session.id,
            agent_id=session.agent_id,
            user_id=session.user_id,
            sandbox_id=session.sandbox_id,
            sandbox_provider=session.sandbox_provider,
        )

    async def _recover_task(
        self, session: Session, task_id: str
    ) -> Task:
        # Recovery is handle reconstruction, never an ownership mutation.
        # Re-read first so a stale reconnect cannot overwrite a replacement
        # task that was bound under the lifecycle lease after this caller read
        # its Session snapshot.
        current = await self._session_repository.find_by_id_and_user_id(
            session.id, session.user_id
        )
        if current is None or current.task_id != task_id:
            raise TurnSubmissionUnavailableError(
                "Persisted task no longer owns this session"
            )
        task = await self._task_cls.get(task_id)
        if task is not None:
            return task
        recover = getattr(self._task_cls, "recover", None)
        if not callable(recover):
            raise TurnSubmissionUnavailableError(
                "Task backend cannot recover a persisted input stream"
            )
        return recover(task_id, self._runner_params(current))

    async def _continue_durable_turn_locked(
        self,
        session: Session,
        turn: TurnSubmission,
    ) -> TurnSubmission:
        """Idempotently continue one accepted turn under the lifecycle lease."""
        session_id = turn.session_id
        submission_id = turn.submission_id
        if (
            turn.user_id != session.user_id
            or turn.agent_id != session.agent_id
        ):
            raise TurnSubmissionUnavailableError(
                "Accepted turn ownership no longer matches its session"
            )

        if turn.state not in TERMINAL_TURN_STATES:
            await self._sync_durable_session_status(
                session_id, idle_status=SessionStatus.PENDING
            )

        # Repair the idempotent Session projection after a crash between
        # acceptance and the legacy history/latest-message writes.
        persisted_input = TypeAdapter(AgentEvent).validate_json(turn.input_json)
        if not isinstance(persisted_input, MessageEvent):
            raise TurnSubmissionUnavailableError(
                "Accepted turn input is not a user message"
            )
        await self._add_event_once(session_id, persisted_input)
        await self._session_repository.update_latest_message(
            session_id,
            persisted_input.message,
            persisted_input.timestamp,
        )

        if turn.state in TERMINAL_TURN_STATES:
            await self._sync_durable_session_status(session_id)
            return turn

        # Pending turns may use the session's existing task. Enqueued turns
        # must recover their exact persisted stream identity.
        try:
            previous_task_id = session.task_id
            sandbox_replaced = await self._ensure_sandbox_locked(session)
            if sandbox_replaced and previous_task_id:
                preserve_submission_id = (
                    submission_id
                    if turn.state == TurnSubmissionState.PENDING
                    and not turn.task_id
                    else None
                )
                await self._retire_replaced_task_stream(
                    session,
                    preserve_submission_id=preserve_submission_id,
                )
                if preserve_submission_id is None:
                    cancelled = await self._turn_submission_repository.find(
                        session_id, submission_id
                    )
                    if cancelled is None:
                        raise TurnSubmissionUnavailableError(
                            "Replaced task turn disappeared"
                        )
                    return cancelled

            if turn.task_id:
                task = await self._recover_task(session, turn.task_id)
            else:
                task = await self._get_task(session)
                if task is None and session.task_id:
                    task = await self._recover_task(session, session.task_id)
                if task is None:
                    task = await self._create_task(
                        session, sandbox_ready=True
                    )
        except Exception as exc:
            failure_state = (
                TurnSubmissionState.FAILED_UNKNOWN
                if turn.state == TurnSubmissionState.ENQUEUED or turn.task_id
                else TurnSubmissionState.FAILED
            )
            await self._turn_submission_repository.mark_unclaimed_terminal(
                session_id,
                submission_id,
                state=failure_state,
                error=safe_exception_summary(exc),
            )
            await self._sync_durable_session_status(session_id)
            if isinstance(exc, AgentBayQuotaExceededError):
                raise
            raise TurnSubmissionUnavailableError(
                "Task provisioning or recovery failed"
            ) from exc

        if turn.state == TurnSubmissionState.PENDING:
            try:
                stream_id = await task.input_stream.put(turn.input_json)
            except Exception as exc:
                # XADD may have committed even when its response was lost.
                # Keeping Mongo pending lets the same logical UUID retry; the
                # Mongo execution claim prevents duplicate side effects.
                raise TurnSubmissionUnavailableError(
                    "Turn input transport state is unknown; retry the same submission_id"
                ) from exc
            try:
                turn = await self._turn_submission_repository.mark_enqueued(
                    session_id,
                    submission_id,
                    task_id=task.id,
                    stream_id=stream_id,
                )
            except Exception as exc:
                raise TurnSubmissionUnavailableError(
                    "Queued turn state could not be confirmed; retry the same submission_id"
                ) from exc

        if turn.state == TurnSubmissionState.ENQUEUED:
            if turn.task_id and turn.task_id != task.id:
                task = await self._recover_task(session, turn.task_id)
            try:
                await self._sync_durable_session_status(session_id)
                await task.run()
            except Exception as exc:
                await self._turn_submission_repository.mark_unclaimed_terminal(
                    session_id,
                    submission_id,
                    state=TurnSubmissionState.FAILED_UNKNOWN,
                    error=safe_exception_summary(exc),
                )
                await self._sync_durable_session_status(session_id)
                raise TurnSubmissionUnavailableError(
                    "Task dispatch state is unknown; retry the same submission_id"
                ) from exc
        await self._sync_durable_session_status(session_id)
        return turn

    async def _resume_pending_durable_turn(
        self,
        *,
        session_id: str,
        user_id: str,
        submission_id: str,
    ) -> tuple[TurnSubmission, bool]:
        """Resume accept->XADD->dispatch after an API crash on reconnect."""
        async with self._get_session_lock(session_id):
            async def resume() -> tuple[TurnSubmission, bool]:
                session = await self._session_repository.find_by_id_and_user_id(
                    session_id, user_id
                )
                if session is None:
                    raise TurnSubmissionUnavailableError("Session not found")
                current = await self._turn_submission_repository.find(
                    session_id, submission_id
                )
                if current is None or current.user_id != user_id:
                    raise TurnSubmissionUnavailableError(
                        "Accepted turn disappeared"
                    )
                if current.state != TurnSubmissionState.PENDING:
                    # Another replica may have committed ENQUEUED and then died
                    # before dispatch. Merely observing that state does not
                    # confirm the producer side effect for this reconnect.
                    return current, False
                continued = await self._continue_durable_turn_locked(
                    session, current
                )
                # A successful continuation reaches task.run() before it
                # returns an ENQUEUED turn. Terminal results need no kick, but
                # are also fully resolved by this invocation.
                return continued, True

            return await self.run_session_lifecycle_exclusive(
                session_id, resume
            )

    async def accept_chat_submission(
        self,
        *,
        session_id: str,
        user_id: str,
        submission_id: str,
        message: str,
        timestamp: Optional[datetime] = None,
        attachments: Optional[List[FileInfo]] = None,
    ) -> TurnSubmission:
        """Persist acceptance, project history once, then dispatch at least once."""
        if self._turn_submission_repository is None:
            raise TurnSubmissionUnavailableError(
                "Durable turn repository is not configured"
            )
        try:
            normalized_submission_id = str(uuid.UUID(str(submission_id)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("submission_id must be a UUID") from exc
        if not message:
            raise ValueError("A durable submission requires a non-empty message")

        async with self._get_session_lock(session_id):
            async def submit() -> TurnSubmission:
                session = await self._session_repository.find_by_id_and_user_id(
                    session_id, user_id
                )
                if not session:
                    raise RuntimeError("Session not found")

                requested_file_infos = self._to_file_infos(attachments)
                canonical = json.dumps(
                    {
                        "message": message,
                        "timestamp": (
                            timestamp.isoformat() if timestamp is not None else None
                        ),
                        "attachments": [
                            self._canonical_attachment(attachment)
                            for attachment in (requested_file_infos or [])
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                request_hash = hashlib.sha256(
                    canonical.encode("utf-8")
                ).hexdigest()

                # Idempotent retries must not depend on mutable external file
                # state. Once a turn exists, validate only its stable request
                # identity and continue the persisted input. A file may have
                # been deleted or storage may be temporarily unavailable after
                # the original acceptance response was lost.
                existing = await self._turn_submission_repository.find(
                    session_id, normalized_submission_id
                )
                if existing is not None:
                    retry_candidate = TurnSubmission(
                        session_id=session_id,
                        submission_id=normalized_submission_id,
                        user_id=user_id,
                        agent_id=session.agent_id,
                        request_hash=request_hash,
                        input_json=existing.input_json,
                        resumes_waiting=existing.resumes_waiting,
                    )
                    turn, _created = await self._turn_submission_repository.accept(
                        retry_candidate
                    )
                    return await self._continue_durable_turn_locked(session, turn)

                file_infos = await self._canonicalize_owned_attachments(
                    attachments, user_id
                )
                message_event = MessageEvent(
                    id=normalized_submission_id,
                    turn_id=normalized_submission_id,
                    message=message,
                    role="user",
                    timestamp=timestamp or datetime.now(UTC),
                    attachments=file_infos,
                )
                candidate = TurnSubmission(
                    session_id=session_id,
                    submission_id=normalized_submission_id,
                    user_id=user_id,
                    agent_id=session.agent_id,
                    request_hash=request_hash,
                    input_json=message_event.model_dump_json(),
                    # Capture this under the lifecycle lease. Subsequent
                    # projection changes the Session status to PENDING/RUNNING,
                    # but the worker still needs to resume the waiting plan.
                    # This flag intentionally is not part of request_hash: a
                    # retry of the same logical submission must return the
                    # first persisted decision.
                    resumes_waiting=session.status == SessionStatus.WAITING,
                )
                turn, _created = await self._turn_submission_repository.accept(
                    candidate
                )
                return await self._continue_durable_turn_locked(session, turn)

            return await self.run_session_lifecycle_exclusive(session_id, submit)

    async def _stream_durable_turn(
        self,
        *,
        session_id: str,
        user_id: str,
        turn: TurnSubmission,
    ) -> AsyncGenerator[BaseEvent, None]:
        """Replay Mongo history and follow it until the durable terminal state.

        Runner output is written to Mongo before Redis. Polling the bounded
        history here means a live Redis outage can delay delivery but cannot
        erase an accepted response. Stable event IDs deduplicate history/live
        overlap on the frontend.
        """
        submission_id = turn.submission_id
        yield AcceptedEvent(
            id=f"{submission_id}:accepted",
            turn_id=submission_id,
            submission_id=submission_id,
            state=turn.state.value,
        )
        seen: set[str] = set()
        terminal_seen = False
        dispatch_recovery_confirmed = False
        try:
            while True:
                session = await self._session_repository.find_by_id_and_user_id(
                    session_id, user_id
                )
                if not session:
                    return
                output_events = await self._turn_submission_repository.list_outputs(
                    session_id, submission_id
                )
                for event in output_events:
                    if event.turn_id != submission_id or event.id in seen:
                        continue
                    seen.add(event.id)
                    if isinstance(event, MessageEvent) and event.role == "user":
                        continue
                    yield event
                    if isinstance(event, (DoneEvent, ErrorEvent, WaitEvent)):
                        terminal_seen = True

                current = await self._turn_submission_repository.find(
                    session_id, submission_id
                )
                if current is None:
                    raise TurnSubmissionUnavailableError(
                        "Accepted turn state disappeared"
                    )
                if current.state == TurnSubmissionState.PENDING:
                    try:
                        current, recovered_dispatch = (
                            await self._resume_pending_durable_turn(
                                session_id=session_id,
                                user_id=user_id,
                                submission_id=submission_id,
                            )
                        )
                        # Only this invocation's successful continuation can
                        # confirm dispatch. If it merely observed a concurrent
                        # ENQUEUED transition, the producer-crash kick below is
                        # still required.
                        dispatch_recovery_confirmed = (
                            dispatch_recovery_confirmed or recovered_dispatch
                        )
                    except Exception as exc:
                        logger.warning(
                            "Pending durable turn recovery deferred: "
                            "session_id=%s submission_id=%s error=%s",
                            session_id,
                            submission_id,
                            safe_exception_summary(exc),
                        )
                if current.state in TERMINAL_TURN_STATES:
                    if not terminal_seen:
                        terminal_id = (
                            current.terminal_event_id
                            or f"{submission_id}:terminal"
                        )
                        if current.state == TurnSubmissionState.COMPLETED:
                            synthetic = DoneEvent(
                                id=terminal_id,
                                turn_id=submission_id,
                            )
                        else:
                            synthetic = ErrorEvent(
                                id=terminal_id,
                                turn_id=submission_id,
                                error=current.terminal_error
                                or f"Turn ended in state {current.state.value}",
                            )
                        synthetic = (
                            await self._turn_submission_repository.append_output(
                                session_id, submission_id, synthetic
                            )
                        )
                        await self._add_event_once(session_id, synthetic)
                        yield synthetic
                    return
                if (
                    current.state == TurnSubmissionState.ENQUEUED
                    and current.task_id
                    and not dispatch_recovery_confirmed
                ):
                    # Recover the producer crash window where Mongo/XADD and
                    # Redis DISPATCHING committed but the API process died
                    # before publishing the Celery delivery.  Reusing run()
                    # is safe: local tasks remember a tail rerun, and Celery's
                    # generation lease either observes the active delivery or
                    # takes over after its bounded deadline.
                    try:
                        task = await self._recover_task(session, current.task_id)
                        await task.run()
                        dispatch_recovery_confirmed = True
                    except Exception as exc:
                        logger.warning(
                            "Durable dispatch recovery deferred: session_id=%s "
                            "submission_id=%s error=%s",
                            session_id,
                            submission_id,
                            safe_exception_summary(exc),
                        )
                await asyncio.sleep(0.2)
        finally:
            try:
                await self._session_repository.update_unread_message_count(
                    session_id, 0
                )
            except Exception:
                # A serialized delete can remove the session immediately after
                # the final owner re-read. This cleanup is non-authoritative and
                # must not revive or fail the completed/delete-race response.
                logger.debug(
                    "Skipped unread cleanup for an absent session: session_id=%s",
                    session_id,
                )

    async def stop_session(self, session_id: str) -> None:
        """Stop a session"""
        session = await self._session_repository.find_by_id(session_id)
        if not session:
            logger.error(f"Attempted to stop non-existent Session {session_id}")
            raise RuntimeError("Session not found")
        task = await self._get_task(session)
        if task:
            await task.cancel()
            stopped = await task.wait_for_done(self._TASK_CANCEL_TIMEOUT_SECONDS)
            if not stopped:
                raise TimeoutError(
                    "task did not acknowledge cancellation within "
                    f"{self._TASK_CANCEL_TIMEOUT_SECONDS:g}s"
                )
        await self._finalize_turns_after_task_stop(
            session_id, user_id=session.user_id
        )
        await self._session_repository.update_status(session_id, SessionStatus.COMPLETED)

    async def cleanup_session_resources(self, session: Session) -> None:
        """Stop a session task and release its sandbox before record deletion.

        A cancellation request is not an acknowledgement for remote task
        backends.  The sandbox is released only after the execution side has
        reported the task done.  The caller must retain the session record on
        any failure so cleanup can be retried without losing provider IDs.
        """
        errors: list[str] = []
        task_stopped = True

        if session.task_id:
            try:
                task = await self._get_task(session)
                if task:
                    await task.cancel()
                    task_stopped = await task.wait_for_done(
                        self._TASK_CANCEL_TIMEOUT_SECONDS
                    )
                    if not task_stopped:
                        raise TimeoutError(
                            "task did not acknowledge cancellation within "
                            f"{self._TASK_CANCEL_TIMEOUT_SECONDS:g}s"
                        )
            except Exception as exc:
                task_stopped = False
                summary = safe_exception_summary(exc)
                logger.error(
                    "Failed to cancel task %s for session %s: %s",
                    session.task_id,
                    session.id,
                    summary,
                )
                errors.append(f"task cancellation failed: {summary}")

        if task_stopped:
            try:
                await self._finalize_turns_after_task_stop(
                    session.id, user_id=session.user_id
                )
            except Exception as exc:
                task_stopped = False
                errors.append(
                    "turn cancellation finalization failed: "
                    f"{safe_exception_summary(exc)}"
                )

        if task_stopped:
            try:
                if self._sandbox_provisioner is None:
                    if session.sandbox_id:
                        raise SandboxProvisioningRequiredError(
                            "No sandbox lifecycle provisioner is configured"
                        )
                else:
                    # AgentBay cleanup must consult its ledger even when the
                    # Session projection lost sandbox_id.  The provisioner is
                    # the only component allowed to delete provider resources.
                    await self._sandbox_provisioner.destroy_locked(session)
            except Exception as exc:
                summary = safe_exception_summary(exc)
                logger.error(
                    "Failed to release sandbox ownership for session %s: %s",
                    session.id,
                    summary,
                )
                errors.append(f"sandbox destruction failed: {summary}")

        if errors:
            raise RuntimeError("; ".join(errors))

    async def chat(
        self,
        session_id: str,
        user_id: str,
        message: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        latest_event_id: Optional[str] = None,
        attachments: Optional[List[FileInfo]] = None,
        *,
        submission_id: Optional[str] = None,
        accepted_submission: Optional[TurnSubmission] = None,
    ) -> AsyncGenerator[BaseEvent, None]:
        """
        Chat with an agent
        """

        if self._turn_submission_repository is not None:
            if message:
                turn = accepted_submission or await self.accept_chat_submission(
                    session_id=session_id,
                    user_id=user_id,
                    submission_id=submission_id or "",
                    message=message,
                    timestamp=timestamp,
                    attachments=attachments,
                )
                async for event in self._stream_durable_turn(
                    session_id=session_id,
                    user_id=user_id,
                    turn=turn,
                ):
                    yield event
                return

            # A browser refresh reconnects with an empty message. Replay the
            # Mongo outbox for every active durable turn in submission order;
            # falling back to the old Redis output stream here would lose
            # events after stream expiry/outage despite Mongo having them.
            if submission_id:
                requested = await self._turn_submission_repository.find(
                    session_id, submission_id
                )
                if requested is None or requested.user_id != user_id:
                    raise RuntimeError("Durable turn not found")
                durable_turns = [requested]
            else:
                durable_turns = await self._turn_submission_repository.list_active(
                    session_id, user_id=user_id
                )
            if durable_turns:
                for active_turn in durable_turns:
                    async for event in self._stream_durable_turn(
                        session_id=session_id,
                        user_id=user_id,
                        turn=active_turn,
                    ):
                        yield event
                return

        owner_verified = False
        turn_id: Optional[str] = None
        previous_turn_done: Optional[asyncio.Future[None]] = None
        turn_done: Optional[asyncio.Future[None]] = None
        try:
            if message:
                # Re-read and enqueue under a short submission lock.  Multiple
                # messages can then sit in one task's input stream while their
                # response readers are ordered by the turn-future chain below.
                async with self._get_session_lock(session_id):
                    async def submit_message():
                        nonlocal owner_verified
                        session = (
                            await self._session_repository.find_by_id_and_user_id(
                                session_id, user_id
                            )
                        )
                        if not session:
                            logger.error(
                                "Attempted to chat with non-existent Session %s for user %s",
                                session_id,
                                user_id,
                            )
                            raise RuntimeError("Session not found")
                        owner_verified = True

                        previous_task_id = session.task_id
                        sandbox_replaced = await self._ensure_sandbox_locked(
                            session
                        )
                        if sandbox_replaced and previous_task_id:
                            await self._retire_replaced_task_stream(
                                session, preserve_submission_id=None
                            )
                        task = await self._get_task(session)
                        if task and await task.is_done():
                            # Local task backends keep a registry entry until
                            # the terminal handle is explicitly cleaned.
                            await task.cancel()
                            task = None
                        if not task:
                            task = await self._create_task(
                                session, sandbox_ready=True
                            )
                            if not task:
                                raise RuntimeError("Failed to create task")

                        # New turns always start after the output that existed
                        # at submission time. turn_id filtering is the second
                        # guard while a preceding turn is still producing.
                        turn_start_event_id = (
                            await self._get_latest_output_stream_id(task)
                        )
                        await self._session_repository.update_latest_message(
                            session_id, message, timestamp or datetime.now(UTC)
                        )

                        message_event = MessageEvent(
                            message=message,
                            role="user",
                            attachments=self._to_file_infos(attachments),
                        )
                        event_id = await task.input_stream.put(
                            message_event.model_dump_json()
                        )
                        message_event.id = event_id
                        message_event.turn_id = event_id
                        await self._session_repository.add_event(
                            session_id, message_event
                        )
                        await task.run()
                        logger.debug(
                            "Put message into Session queue: session_id=%s submission_transport=%s",
                            session_id,
                            event_id,
                        )
                        return session, task, turn_start_event_id, event_id

                    session, task, turn_start_event_id, event_id = (
                        await self.run_session_lifecycle_exclusive(
                            session_id, submit_message
                        )
                    )
                    previous_turn_done = self._session_turn_tails.get(session_id)
                    turn_done = asyncio.get_running_loop().create_future()
                    self._session_turn_tails[session_id] = turn_done
                    turn_id = event_id

                # Submission is complete, so later turns can enqueue on the
                # same live task.  Only output delivery is serialized.
                if previous_turn_done is not None:
                    await asyncio.shield(previous_turn_done)
                latest_event_id = turn_start_event_id
            else:
                session = await self._session_repository.find_by_id_and_user_id(
                    session_id, user_id
                )
                if not session:
                    logger.error(
                        "Attempted to chat with non-existent Session %s for user %s",
                        session_id,
                        user_id,
                    )
                    raise RuntimeError("Session not found")
                owner_verified = True
                task = await self._get_task(session)

            if not message and not task and session.status in (
                SessionStatus.PENDING,
                SessionStatus.RUNNING,
            ):
                logger.warning(
                    "Session %s is %s but has no live task; marking it completed",
                    session_id,
                    session.status,
                )
                await self._session_repository.update_status(
                    session_id, SessionStatus.COMPLETED
                )
                return

            logger.info(f"Session {session_id} started")
            logger.debug(f"Session {session_id} task: {task}")

            if (
                not message
                and latest_event_id
                and not self._is_redis_stream_id(latest_event_id)
            ):
                logger.warning(
                    "Ignoring non-Redis stream event id for session %s: %s",
                    session_id,
                    latest_event_id,
                )
                # Browser/client event UUIDs are not Redis cursor IDs.  Start
                # from the beginning instead of jumping to the stream tail;
                # jumping silently loses every event produced while the
                # client was disconnected.
                latest_event_id = None

            terminal_seen = False
            restarted_after_drained_task = False
            while task:
                # Check done state before reading so buffered events are
                # fully drained even if the task finished in the meantime
                task_done = await task.is_done()
                event_id, event_str = await task.output_stream.get(
                    start_id=latest_event_id, block_ms=1000
                )
                if event_str is None:
                    # The worker may have crossed from running to done while
                    # the blocking stream read was waiting.
                    if not task_done:
                        task_done = await task.is_done()
                    if task_done:
                        if message and not restarted_after_drained_task:
                            # Cover the narrow hand-off race where a second
                            # input arrives after a runner's final empty-queue
                            # check but before its task backend records DONE.
                            # Re-running the same task is idempotent for the
                            # stream and gives that queued turn one guaranteed
                            # execution opportunity (including Celery mode).
                            restarted_after_drained_task = True
                            await task.run()
                            continue
                        logger.debug(
                            "Session %s's task is done and event queue is drained",
                            session_id,
                        )
                        break
                    logger.debug(f"No event found in Session {session_id}'s event queue")
                    continue
                latest_event_id = event_id
                event = TypeAdapter(AgentEvent).validate_json(event_str)
                event.transport_id = event_id
                if turn_id is not None and event.turn_id != turn_id:
                    logger.debug(
                        "Skipping output event %s for turn %s while reading turn %s",
                        event_id,
                        event.turn_id,
                        turn_id,
                    )
                    continue
                logger.debug(
                    "Got event from Session %s's event queue: %s",
                    session_id,
                    type(event).__name__,
                )
                await self._session_repository.update_unread_message_count(
                    session_id, 0
                )
                yield event
                if isinstance(event, (DoneEvent, ErrorEvent, WaitEvent)):
                    terminal_seen = True
                    break

            if message and not terminal_seen:
                raise RuntimeError(
                    f"Task completed before producing a terminal event for turn {turn_id}"
                )
            
            logger.info(f"Session {session_id} completed")

        except Exception as e:
            summary = safe_exception_summary(e)
            logger.error(
                "Session processing failed: session_id=%s error=%s",
                session_id,
                summary,
            )
            # Preserve the small set of domain-authored, user-actionable
            # messages while never echoing arbitrary provider/network bodies.
            public_error = summary
            if isinstance(e, SessionLifecycleLeaseError) or (
                isinstance(e, RuntimeError)
                and e.args == ("Session not found",)
            ):
                public_error = str(e)
            event = ErrorEvent(error=public_error, turn_id=turn_id)
            # A caller that does not own this session must never be able to
            # append an ErrorEvent to it or mutate its unread state.
            if owner_verified:
                await self._add_event_once(session_id, event)
            yield event # TODO: raise api exception
        finally:
            if turn_done is not None:
                if not turn_done.done():
                    turn_done.set_result(None)
                if self._session_turn_tails.get(session_id) is turn_done:
                    self._session_turn_tails.pop(session_id, None)
            if owner_verified:
                await self._session_repository.update_unread_message_count(
                    session_id, 0
                )
