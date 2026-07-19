from typing import Any, Dict, Optional, AsyncGenerator, List, Type
import asyncio
import inspect
import hashlib
import io
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
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
from app.domain.external.task import TaskRunner, TaskRunnerFactory, Task
from app.domain.repositories.session_repository import SessionRepository
from app.domain.repositories.turn_submission_repository import TurnSubmissionRepository
from app.domain.models.turn_submission import (
    TERMINAL_TURN_STATES,
    TurnClaimDecision,
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

class AgentTaskRunner(TaskRunner):
    """Agent task that can be cancelled"""
    _DELIVERABLE_ROOT = "/home/ubuntu/upload"
    _ARTIFACT_SYNC_TIMEOUT_SECONDS = 10.0
    _MAX_SHELL_ARTIFACT_CANDIDATES = 32
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
        sandbox: Sandbox,
        browser: Browser,
        agent_repository: AgentRepository,
        session_repository: SessionRepository,
        file_storage: FileStorage,
        mcp_repository: MCPRepository,
        llm: LLM,
        search_engine: Optional[SearchEngine] = None,
        turn_submission_repository: Optional[TurnSubmissionRepository] = None,
    ):
        self._session_id = session_id
        self._agent_id = agent_id
        self._user_id = user_id
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
        self._generated_artifacts: dict[str, FileInfo] = {}
        self._synced_artifacts: dict[str, FileInfo] = {}
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
    
    async def _get_browser_screenshot(self) -> str:
        screenshot = await self._browser.screenshot()
        result = await self._file_storage.upload_file(
            io.BytesIO(screenshot),
            "screenshot.png",
            self._user_id,
            content_type="image/png",
        )
        # Public sharing authorizes only files canonically bound to the
        # session. Replace by file_id before appending so a retried screenshot
        # upload cannot leave duplicate session-file entries.
        await self._session_repository.remove_file(
            self._session_id, result.file_id
        )
        await self._session_repository.add_file(self._session_id, result)
        return result.file_id

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

    def _extract_artifact_paths(self, text: str) -> List[str]:
        if not text:
            return []
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
            rf"(?<![\w./:~+\-])(?P<path>(?:~/|/)[^\s\"'`<>|;&]*?\.(?:tar\.gz|{'|'.join(map(re.escape, suffixes))}))",
            re.IGNORECASE,
        )
        paths: List[str] = []
        for match in pattern.finditer(text):
            path = self._normalize_sandbox_path(match.group("path"))
            if path and path not in paths:
                paths.append(path)
        return paths

    def _remember_synced_artifact(self, file_info: Optional[FileInfo]) -> None:
        if file_info and file_info.file_path and self._looks_like_artifact_path(file_info.file_path):
            self._synced_artifacts[file_info.file_path] = file_info

    def _remember_generated_artifact(self, file_info: Optional[FileInfo]) -> None:
        if (
            file_info
            and file_info.file_path
            and self._looks_like_artifact_path(file_info.file_path)
            and self._is_auto_deliverable_path(file_info.file_path)
        ):
            self._generated_artifacts[file_info.file_path] = file_info

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
            try:
                await self._sandbox.file_download(candidate)
                return candidate
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
                result = await self._sandbox.file_find(search_dir, f"**/{basename}")
                for candidate in (result.data or {}).get("files", []):
                    try:
                        await self._sandbox.file_download(candidate)
                        logger.warning(
                            "Resolved a missing attachment path in the sandbox: agent_id=%s",
                            self._agent_id,
                        )
                        return candidate
                    except Exception:
                        continue
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
            file_path = resolved
            if not generated and file_path in self._synced_artifacts:
                return self._synced_artifacts[file_path]
            file_info = await self._session_repository.get_file_by_path(self._session_id, file_path)
            file_data = await self._sandbox.file_download(file_path)
            if file_info:
                await self._session_repository.remove_file(self._session_id, file_info.file_id)
            file_name = file_path.split("/")[-1]
            file_info = await self._file_storage.upload_file(file_data, file_name, self._user_id)
            file_info.file_path = file_path
            await self._session_repository.add_file(self._session_id, file_info)
            self._remember_synced_artifact(file_info)
            if generated:
                self._remember_generated_artifact(file_info)
            return file_info
        except Exception as e:
            logger.error(
                "Agent %s failed to sync file: %s",
                self._agent_id,
                safe_exception_summary(e),
            )
    
    async def _sync_file_to_sandbox(self, file_id: str) -> Optional[FileInfo]:
        """Download file from storage to sandbox"""
        try:
            file_data, file_info = await self._file_storage.download_file(file_id, self._user_id)
            file_path = f"{self._DELIVERABLE_ROOT}/{file_info.filename}"
            result = await self._sandbox.file_upload(file_data, file_path)
            if result.success:
                file_info.file_path = file_path
                return file_info
        except Exception as e:
            logger.error(
                "Agent %s failed to sync file: %s",
                self._agent_id,
                safe_exception_summary(e),
            )

    async def _sync_message_attachments_to_storage(self, event: MessageEvent) -> None:
        """Sync message attachments and update event attachments"""
        attachments: List[FileInfo] = []
        seen_paths: set[str] = set()
        try:
            if event.attachments:
                for attachment in event.attachments:
                    file_info = await self._sync_file_to_storage(
                        attachment.file_path, fallback_content=event.message
                    )
                    if file_info:
                        attachments.append(file_info)
                        if file_info.file_path:
                            seen_paths.add(file_info.file_path)
            for path in self._extract_artifact_paths(event.message):
                if path in seen_paths:
                    continue
                file_info = await self._sync_file_to_storage(path)
                if file_info:
                    attachments.append(file_info)
                    if file_info.file_path:
                        seen_paths.add(file_info.file_path)
            for path, file_info in self._generated_artifacts.items():
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
        self, event: ToolEvent, shell_result: Optional[ToolResult]
    ) -> None:
        text_parts = [
            value
            for key in ("command", "exec_dir")
            if isinstance((value := event.function_args.get(key)), str)
        ]
        if shell_result and getattr(shell_result, "data", None):
            data = shell_result.data or {}
            for key in ("command", "output"):
                if isinstance(data.get(key), str):
                    text_parts.append(data[key])
            for record in data.get("console") or []:
                if hasattr(record, "model_dump"):
                    record = record.model_dump()
                if isinstance(record, dict):
                    for key in ("command", "output"):
                        if isinstance(record.get(key), str):
                            text_parts.append(record[key])
        paths = self._extract_artifact_paths("\n".join(text_parts))
        if len(paths) > self._MAX_SHELL_ARTIFACT_CANDIDATES:
            logger.warning(
                "Shell artifact candidate limit reached: agent_id=%s count=%s limit=%s",
                self._agent_id,
                len(paths),
                self._MAX_SHELL_ARTIFACT_CANDIDATES,
            )
        deadline = (
            asyncio.get_running_loop().time()
            + self._ARTIFACT_SYNC_TIMEOUT_SECONDS
        )
        for path in paths[: self._MAX_SHELL_ARTIFACT_CANDIDATES]:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(
                    self._sync_file_to_storage(path, generated=True),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                # Artifact discovery enriches the response but is not part of
                # the shell tool result.  It must never hold a turn open.
                logger.warning(
                    "Timed out syncing shell artifact: agent_id=%s path=%s",
                    self._agent_id,
                    path,
                )
                break
    

    # TODO: refactor this function
    async def _handle_tool_event(self, event: ToolEvent):
        """Generate tool content"""
        try:
            if event.status == ToolStatus.CALLED:
                if event.tool_name == "browser":
                    event.tool_content = BrowserToolContent(screenshot=await self._get_browser_screenshot())
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
                    if "id" in event.function_args:
                        shell_result = await self._sandbox.view_shell(event.function_args["id"], console=True)
                        event.tool_content = ShellToolContent(console=shell_result.data.get("console", []))
                    else:
                        event.tool_content = ShellToolContent(console="(No Console)")
                    await self._sync_shell_artifacts(event, shell_result)
                elif event.tool_name == "file":
                    if "file" in event.function_args:
                        file_path = event.function_args["file"]
                        file_read_result = await self._sandbox.file_read(file_path)
                        file_content: str = file_read_result.data.get("content", "")
                        event.tool_content = FileToolContent(content=file_content)
                        await self._sync_file_to_storage(
                            file_path,
                            generated=event.function_name in self._GENERATING_FILE_FUNCTIONS,
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
        if session is None or session.agent_id != self._agent_id:
            terminalized = await self._turn_submission_repository.mark_unclaimed_terminal(
                self._session_id,
                submission_id,
                state=TurnSubmissionState.CANCELLED,
                error="Session ownership no longer matches this queued turn",
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
        claim = await self._turn_submission_repository.claim_for_execution(
            self._session_id,
            submission_id,
            task_id=task.id,
            owner=owner,
            claim_until=datetime.now(UTC)
            + timedelta(seconds=self._claim_seconds),
        )
        if claim.decision == TurnClaimDecision.ACK:
            await self._sync_durable_session_status()
            return await self._try_ack_input(task, transport_id)
        if claim.decision == TurnClaimDecision.RETRY:
            return False

        await self._sync_durable_session_status(
            idle_status=SessionStatus.RUNNING
        )

        parent_task = asyncio.current_task()
        if parent_task is None:
            raise RuntimeError("Durable runner has no owning asyncio task")
        renewal_lost = asyncio.Event()
        renewer = asyncio.create_task(
            self._renew_claim_loop(
                submission_id, owner, parent_task, renewal_lost
            )
        )
        # Be conservative: every operation after claim can touch an external
        # provider or user artifact. A crash from this point is failed_unknown
        # and must never be automatically replayed.
        side_effects_started = True
        terminal_event: Optional[BaseEvent] = None
        try:
            # Mongo claim is already committed. An unavailable Mongo claim never
            # reaches any of these external operations.
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

            done_event = await self._put_and_add_event(
                task, DoneEvent(), turn_id=submission_id
            )
            committed = await self._turn_submission_repository.mark_terminal(
                self._session_id,
                submission_id,
                owner=owner,
                state=TurnSubmissionState.CANCELLED,
                terminal_event_id=done_event.id,
                error="Execution was cancelled",
            )
            if committed or await self._terminal_is_persisted(submission_id):
                await self._try_ack_input(task, transport_id)
            await self._sync_durable_session_status()
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


    async def aclose(self) -> None:
        """Release this runner's clients without deleting its sandbox.

        Runners are reconstructed for each execution process/turn, while the
        sandbox ID belongs to the persisted Session.  Closing the browser,
        MCP, model and sandbox *handles* must therefore be idempotent and
        non-destructive.
        """
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            resources = (
                ("browser", self._browser, ("cleanup", "aclose", "close")),
                ("MCP", self._mcp_tool, ("cleanup", "aclose", "close")),
                ("LLM", self._llm, ("aclose", "cleanup", "close")),
                ("sandbox", self._sandbox, ("aclose",)),
            )
            for name, resource, methods in resources:
                try:
                    await _close_resource(resource, *methods)
                except Exception as exc:
                    # Continue closing the remaining independent resources;
                    # errors are observed here rather than lost in a detached
                    # background task.
                    logger.error(
                        "Failed to close Agent %s %s handle: %s",
                        self._agent_id,
                        name,
                        safe_exception_summary(exc),
                    )


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
        sandbox_provider: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "session_id": session_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "sandbox_id": sandbox_id,
            "sandbox_provider": sandbox_provider,
        }

    async def create_runner(self, params: Dict[str, Any]) -> AgentTaskRunner:
        sandbox_id = params["sandbox_id"]
        # Do not turn a transient provider/network lookup error into a second
        # billable sandbox. Implementations return None only for authoritative
        # not-found and raise when the state is inconclusive.
        sandbox = None
        browser = None
        llm = None
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
                or session.sandbox_id != sandbox_id
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
            sandbox = await self._sandbox_cls.get(sandbox_id)
            if not sandbox:
                # Worker processes do not own the session lifecycle lease and
                # therefore must never allocate a replacement. The API-side
                # provisioner will reconcile ownership and dispatch a task
                # carrying the replacement sandbox ID.
                raise SandboxProvisioningRequiredError(
                    "Persisted sandbox is missing; API provisioning is required"
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
                sandbox=sandbox,
                browser=browser,
                agent_repository=self._agent_repository,
                session_repository=self._session_repository,
                file_storage=self._file_storage,
                mcp_repository=self._mcp_repository,
                llm=llm,
                search_engine=self._search_engine,
                turn_submission_repository=self._turn_submission_repository,
            )
        except BaseException:
            # A runner never took ownership, so release every constructed
            # client handle here.  The persisted sandbox itself is retained.
            for name, resource, methods in (
                ("browser", browser, ("cleanup", "aclose", "close")),
                ("LLM", llm, ("aclose", "cleanup", "close")),
                ("sandbox", sandbox, ("aclose",)),
            ):
                try:
                    await _close_resource(resource, *methods)
                except Exception as exc:
                    logger.error(
                        "Failed to close %s after runner construction error: %s",
                        name,
                        safe_exception_summary(exc),
                    )
            raise
