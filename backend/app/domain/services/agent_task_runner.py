from typing import Optional, AsyncGenerator, List
import asyncio
import logging
import os
import re
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
from app.domain.external.browser import Browser
from app.domain.external.search import SearchEngine
from app.domain.external.file import FileStorage
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.external.task import TaskRunner, Task
from app.domain.repositories.session_repository import SessionRepository
from app.domain.repositories.mcp_repository import MCPRepository
from app.domain.models.session import SessionStatus
from app.domain.models.file import FileInfo
from app.domain.services.tools.mcp import MCPToolkit
from app.domain.models.tool_result import ToolResult
from app.domain.models.search import SearchResults

logger = logging.getLogger(__name__)

class AgentTaskRunner(TaskRunner):
    """Agent task that can be cancelled"""
    _DELIVERABLE_ROOT = "/home/ubuntu/upload"
    _GENERATING_FILE_FUNCTIONS = {"file_write", "file_str_replace"}
    _ARTIFACT_EXTENSIONS = {
        ".csv",
        ".docx",
        ".html",
        ".htm",
        ".jpeg",
        ".jpg",
        ".json",
        ".log",
        ".md",
        ".pdf",
        ".png",
        ".pptx",
        ".py",
        ".tar",
        ".tgz",
        ".ts",
        ".txt",
        ".vue",
        ".xlsx",
        ".zip",
    }
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
        search_engine: Optional[SearchEngine] = None,
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
            self._search_engine,
        )

    async def _put_and_add_event(self, task: Task, event: AgentEvent) -> None:
        event_id = await task.output_stream.put(event.model_dump_json())
        event.id = event_id
        await self._session_repository.add_event(self._session_id, event)
    
    async def _pop_event(self, task: Task) -> AgentEvent:
        event_id, event_str = await task.input_stream.pop()
        if event_str is None:
            logger.warning(f"Agent {self._agent_id} received empty message")
            return
        event = TypeAdapter(AgentEvent).validate_json(event_str)
        event.id = event_id
        return event
    
    async def _get_browser_screenshot(self) -> str:
        screenshot = await self._browser.screenshot()
        result = await self._file_storage.upload_file(screenshot, "screenshot.png", self._user_id)
        return result.file_id

    def _normalize_sandbox_path(self, file_path: str) -> str:
        """Normalize common model-produced sandbox path variants."""
        path = (file_path or "").strip().strip("\"'`")
        if path.startswith("~/"):
            return f"/home/ubuntu/{path[2:]}"
        return path

    def _looks_like_artifact_path(self, file_path: str) -> bool:
        path = self._normalize_sandbox_path(file_path).lower()
        if path.endswith(".tar.gz"):
            return True
        return PurePosixPath(path).suffix in self._ARTIFACT_EXTENSIONS

    def _is_auto_deliverable_path(self, file_path: str) -> bool:
        path = self._normalize_sandbox_path(file_path)
        return path == self._DELIVERABLE_ROOT or path.startswith(f"{self._DELIVERABLE_ROOT}/")

    def _extract_artifact_paths(self, text: str) -> List[str]:
        """Extract plausible generated file paths from shell commands or model text."""
        if not text:
            return []

        suffixes = sorted(
            (extension.lstrip(".") for extension in self._ARTIFACT_EXTENSIONS),
            key=len,
            reverse=True,
        )
        suffix_pattern = "|".join(re.escape(suffix) for suffix in suffixes)
        path_pattern = re.compile(
            rf"(?P<path>(?:~|/)[^\s\"'`<>|;&]*?\.(?:tar\.gz|{suffix_pattern}))",
            re.IGNORECASE,
        )

        paths = []
        for match in path_pattern.finditer(text):
            path = self._normalize_sandbox_path(match.group("path"))
            if path and path not in paths:
                paths.append(path)
        return paths

    def _remember_generated_artifact(self, file_info: Optional[FileInfo]) -> None:
        if not file_info or not file_info.file_path:
            return
        if self._looks_like_artifact_path(file_info.file_path) and self._is_auto_deliverable_path(file_info.file_path):
            self._generated_artifacts[file_info.file_path] = file_info

    def _remember_synced_artifact(self, file_info: Optional[FileInfo]) -> None:
        if not file_info or not file_info.file_path:
            return
        if self._looks_like_artifact_path(file_info.file_path):
            self._synced_artifacts[file_info.file_path] = file_info

    async def _resolve_existing_sandbox_file(self, file_path: str) -> Optional[str]:
        """Resolve a model-provided attachment path to an existing sandbox file."""
        if not file_path:
            return None

        normalized_path = self._normalize_sandbox_path(file_path)
        direct_candidates = [normalized_path]
        if normalized_path and not normalized_path.startswith("/"):
            direct_candidates.extend(
                [
                    f"{self._DELIVERABLE_ROOT}/{normalized_path}",
                    f"/home/ubuntu/{normalized_path}",
                    f"/tmp/{normalized_path}",
                ]
            )

        for candidate in direct_candidates:
            try:
                await self._sandbox.file_download(candidate)
                return candidate
            except Exception:
                pass

        basename = PurePosixPath(normalized_path).name
        if not basename:
            return None

        search_dirs = []
        parent = str(PurePosixPath(normalized_path).parent)
        if parent == ".":
            parent = ""
        for candidate in [parent, "/home/ubuntu", self._DELIVERABLE_ROOT, "/tmp"]:
            if candidate and candidate not in search_dirs:
                search_dirs.append(candidate)

        for search_dir in search_dirs:
            try:
                result = await self._sandbox.file_find(search_dir, f"**/{basename}")
                files = (result.data or {}).get("files") or []
                for candidate in files:
                    try:
                        await self._sandbox.file_download(candidate)
                        logger.warning(
                            "Resolved missing attachment path %s to %s",
                            normalized_path,
                            candidate,
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
            resolved_path = await self._resolve_existing_sandbox_file(file_path)
            normalized_path = self._normalize_sandbox_path(file_path)
            if not resolved_path and fallback_content and PurePosixPath(normalized_path).suffix.lower() == ".md":
                resolved_path = (
                    normalized_path
                    if normalized_path.startswith("/")
                    else f"{self._DELIVERABLE_ROOT}/{normalized_path}"
                )
                logger.warning(
                    "Attachment file %s was missing; materializing markdown from final message",
                    resolved_path,
                )
                await self._sandbox.file_write(
                    file=resolved_path,
                    content=fallback_content,
                    trailing_newline=True,
                )
                resolved_path = await self._resolve_existing_sandbox_file(resolved_path)

            if not resolved_path:
                logger.warning("Attachment file not found in sandbox: %s", file_path)
                return None

            file_path = resolved_path
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
            logger.exception(f"Agent {self._agent_id} failed to sync file: {e}")
    
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
            logger.exception(f"Agent {self._agent_id} failed to sync file: {e}")

    async def _sync_message_attachments_to_storage(self, event: MessageEvent) -> None:
        """Sync message attachments and update event attachments"""
        attachments: List[FileInfo] = []
        seen_paths = set()
        try:
            if event.attachments:
                for attachment in event.attachments:
                    file_info = await self._sync_file_to_storage(attachment.file_path, event.message)
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
                if path in seen_paths:
                    continue
                attachments.append(file_info)
                seen_paths.add(path)

            event.attachments = attachments
        except Exception as e:
            logger.exception(f"Agent {self._agent_id} failed to sync attachments to storage: {e}")
    
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
            logger.exception(f"Agent {self._agent_id} failed to sync attachments to event: {e}")

    async def _sync_shell_artifacts(self, event: ToolEvent, shell_result: Optional[ToolResult]) -> None:
        """Best-effort sync for files created through shell commands."""
        text_parts = []
        for key in ("command", "exec_dir"):
            value = event.function_args.get(key)
            if isinstance(value, str):
                text_parts.append(value)

        if shell_result and getattr(shell_result, "data", None):
            data = shell_result.data or {}
            for key in ("command", "output"):
                value = data.get(key)
                if isinstance(value, str):
                    text_parts.append(value)

            console = data.get("console") or []
            if isinstance(console, list):
                for record in console:
                    if hasattr(record, "model_dump"):
                        record = record.model_dump()
                    if isinstance(record, dict):
                        for key in ("command", "output"):
                            value = record.get(key)
                            if isinstance(value, str):
                                text_parts.append(value)

        for path in self._extract_artifact_paths("\n".join(text_parts)):
            if not self._looks_like_artifact_path(path):
                continue
            await self._sync_file_to_storage(path, generated=True)

    # TODO: refactor this function
    async def _handle_tool_event(self, event: ToolEvent):
        """Generate tool content"""
        try:
            if event.status == ToolStatus.CALLED:
                if event.tool_name == "browser":
                    event.tool_content = BrowserToolContent(screenshot=await self._get_browser_screenshot())
                elif event.tool_name == "preview":
                    result_data = {}
                    if event.function_result and getattr(event.function_result, "data", None):
                        result_data = event.function_result.data or {}
                    event.tool_content = PreviewToolContent(
                        url=result_data.get("url") or event.function_args.get("url", ""),
                        title=result_data.get("title") or event.function_args.get("title"),
                    )
                elif event.tool_name == "search":
                    search_results: ToolResult[SearchResults] = event.function_result
                    logger.debug(f"Search tool results: {search_results}")
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
                    logger.debug(f"Processing MCP tool event: function_result={event.function_result}")
                    if event.function_result:
                        if hasattr(event.function_result, 'data') and event.function_result.data:
                            logger.debug(f"MCP tool result data: {event.function_result.data}")
                            event.tool_content = McpToolContent(result=event.function_result.data)
                        elif hasattr(event.function_result, 'success') and event.function_result.success:
                            logger.debug(f"MCP tool result (success, no data): {event.function_result}")
                            result_data = event.function_result.model_dump() if hasattr(event.function_result, 'model_dump') else str(event.function_result)
                            event.tool_content = McpToolContent(result=result_data)
                        else:
                            logger.debug(f"MCP tool result (fallback): {event.function_result}")
                            event.tool_content = McpToolContent(result=str(event.function_result))
                    else:
                        logger.warning("MCP tool: No function_result found")
                        event.tool_content = McpToolContent(result="No result available")
                    
                    logger.debug(f"MCP tool_content set to: {event.tool_content}")
                    if event.tool_content:
                        logger.debug(f"MCP tool_content.result: {event.tool_content.result}")
                        logger.debug(f"MCP tool_content dict: {event.tool_content.model_dump()}")
                else:
                    logger.warning(f"Agent {self._agent_id} received unknown tool event: {event.tool_name}")
        except Exception as e:
            logger.exception(f"Agent {self._agent_id} failed to generate tool content: {e}")

    async def run(self, task: Task) -> None:
        """Process agent's message queue and run the agent's flow"""
        try:
            logger.info(f"Agent {self._agent_id} message processing task started")
            await self._sandbox.ensure_sandbox()
            await self._mcp_tool.initialized(await self._mcp_repository.get_mcp_config())
            while not await task.input_stream.is_empty():
                event = await self._pop_event(task)
                message = ""
                if isinstance(event, MessageEvent):
                    message = event.message or ""
                    await self._sync_message_attachments_to_sandbox(event)
                    
                logger.info(f"Agent {self._agent_id} received new message: {message[:50]}...")

                message_obj = Message(message=message, attachments=[attachment.file_path for attachment in event.attachments])
                
                async for event in self._run_flow(message_obj):
                    await self._put_and_add_event(task, event)
                    if isinstance(event, TitleEvent):
                        await self._session_repository.update_title(self._session_id, event.title)
                    elif isinstance(event, MessageEvent):
                        await self._session_repository.update_latest_message(self._session_id, event.message, event.timestamp)
                        await self._session_repository.increment_unread_message_count(self._session_id)
                    elif isinstance(event, WaitEvent):
                        await self._session_repository.update_status(self._session_id, SessionStatus.WAITING)
                        return
                    if not await task.input_stream.is_empty():
                        break

            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)
        except asyncio.CancelledError:
            logger.info(f"Agent {self._agent_id} task cancelled")
            await self._put_and_add_event(task, DoneEvent())
            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)
        except Exception as e:
            logger.exception(f"Agent {self._agent_id} task encountered exception: {str(e)}")
            
            # If debugger is attached, trigger breakpoint for debugging
            # You can also manually set ENABLE_DEBUG_BREAK=1 environment variable
            if debugpy.is_client_connected() or os.getenv('ENABLE_DEBUG_BREAK'):
                logger.debug("Debugger detected, triggering breakpoint")
                import traceback
                traceback.print_exc()
                debugpy.breakpoint()  # This will pause execution if a debugger is attached
            
            await self._put_and_add_event(task, ErrorEvent(error=f"Task error: {str(e)}"))
            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)
    
    async def _run_flow(self, message: Message) -> AsyncGenerator[BaseEvent, None]:
        """Process a single message through the agent's flow and yield events"""
        if not message.message:
            logger.warning(f"Agent {self._agent_id} received empty message")
            yield ErrorEvent(error="No message")
            return

        async for event in self._flow.run(message):
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


    async def destroy(self) -> None:
        """Destroy the task and release resources"""
        logger.info("Starting to destroy agent task")
        
        # Destroy sandbox environment
        if self._sandbox:
            logger.debug(f"Destroying Agent {self._agent_id}'s sandbox environment")
            await self._sandbox.destroy()
        
        if self._mcp_tool:
            logger.debug(f"Destroying Agent {self._agent_id}'s MCP tool")
            await self._mcp_tool.cleanup()
        
        logger.debug(f"Agent {self._agent_id} has been fully closed and resources cleared")
