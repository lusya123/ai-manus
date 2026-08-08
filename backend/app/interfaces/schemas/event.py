from pydantic import BaseModel, Field
from typing import Any, Union, Literal, Dict, Optional, List, Self, Type
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from app.domain.models.plan import ExecutionStatus
from app.interfaces.schemas.file import FileInfoResponse, SharedFileInfoResponse
from app.domain.models.file import FileInfo
from app.domain.models.event import (
    BrowserToolContent,
    PreviewToolContent,
    ToolContent,
    ToolStatus,
)
from app.domain.utils.time import epoch_seconds, utc_now
from app.domain.models.event import (
    AgentEvent,
    AcceptedEvent,
    ErrorEvent,
    PlanEvent,
    MessageEvent,
    TitleEvent,
    ToolEvent,
    StepEvent,
    FileUpdateEvent,
    TerminalUpdateEvent,
)


_PUBLIC_PREVIEW_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


def _shared_preview_content(
    content: PreviewToolContent,
) -> Optional[PreviewToolContent]:
    """Return the minimal preview metadata safe for a public transcript.

    Shared previews need only a sandbox-local address. Rebuild that address
    from its non-secret components so credentials, query strings, fragments,
    third-party origins, and display titles cannot cross the unauthenticated
    sharing boundary.
    """

    raw_url = content.url.strip()
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or host not in _PUBLIC_PREVIEW_HOSTS
    ):
        return None

    canonical_host = f"[{host}]" if ":" in host else host
    netloc = f"{canonical_host}:{port}" if port is not None else canonical_host
    return PreviewToolContent(
        url=urlunsplit(
            (
                parsed.scheme.lower(),
                netloc,
                parsed.path or "/",
                "",
                "",
            )
        ),
        title=None,
    )


class BaseEventData(BaseModel):
    event_id: Optional[str]
    turn_id: Optional[str] = None
    transport_cursor: Optional[str] = None
    timestamp: datetime = Field(default_factory=utc_now)

    class Config:
        json_encoders = {
            datetime: epoch_seconds
        }

    @classmethod
    def base_event_data(cls, event: AgentEvent) -> dict:
        return {
            "event_id": event.id,
            "turn_id": event.turn_id,
            "transport_cursor": event.transport_id,
            "timestamp": epoch_seconds(event.timestamp)
        }
    
    @classmethod
    def from_event(cls, event: AgentEvent) -> Self:
        return cls(
            **cls.base_event_data(event),
            **event.model_dump(
                exclude={"type", "id", "turn_id", "transport_id", "timestamp"}
            )
        )

class CommonEventData(BaseEventData):
    class Config:
        json_encoders = {
            datetime: epoch_seconds
        }
        extra = "allow"

class BaseStreamEvent(BaseModel):
    event: str
    data: BaseEventData

    @classmethod
    def from_event(cls, event: AgentEvent) -> Self:
        data_class: Type[BaseEventData] = cls.model_fields["data"].annotation or BaseEventData
        return cls(
            event=event.type,
            data=data_class.from_event(event)
        )

class MessageEventData(BaseEventData):
    role: Literal["user", "assistant"]
    content: str
    attachments: Optional[List[FileInfoResponse]] = None

class MessageStreamEvent(BaseStreamEvent):
    event: Literal["message"] = "message"
    data: MessageEventData

    @classmethod
    async def from_event_async(cls, event: MessageEvent) -> Self:
        return cls(
            data=MessageEventData(
                **BaseEventData.base_event_data(event),
                role=event.role,
                content=event.message,
                attachments=[await FileInfoResponse.from_domain(attachment) for attachment in event.attachments] if event.attachments else None
            )
        )


class SharedMessageEventData(BaseEventData):
    """Public message shape without owner or internal file metadata."""

    role: Literal["user", "assistant"]
    content: str
    attachments: Optional[List[SharedFileInfoResponse]] = None


class SharedMessageStreamEvent(BaseStreamEvent):
    event: Literal["message"] = "message"
    data: SharedMessageEventData

class ToolEventData(BaseEventData):
    tool_call_id: str
    name: str
    status: ToolStatus
    function: str
    args: Dict[str, Any]
    content: Optional[ToolContent] = None

class ToolStreamEvent(BaseStreamEvent):
    event: Literal["tool"] = "tool"
    data: ToolEventData

    @classmethod
    async def from_event_async(cls, event: ToolEvent) -> Self:
        content = event.tool_content
        if isinstance(content, BrowserToolContent):
            screenshot = content.screenshot
            if screenshot:
                from app.interfaces.dependencies import get_file_service
                try:
                    screenshot = (
                        await get_file_service().create_internal_signed_url(
                            screenshot
                        )
                    )
                except FileNotFoundError:
                    # Screenshot enrichment is best-effort.  A timeout can
                    # persist an empty ID, and historical GridFS artifacts can
                    # disappear independently of the durable event.  Neither
                    # case should make the entire session history unreadable.
                    screenshot = ""
            content = BrowserToolContent(screenshot=screenshot)
        return cls(
            data=ToolEventData(
                **BaseEventData.base_event_data(event),
                tool_call_id=event.tool_call_id,
                name=event.tool_name,
                status=event.status,
                function=event.function_name,
                args=event.function_args,
                content=content
            )
        )

class DoneStreamEvent(BaseStreamEvent):
    event: Literal["done"] = "done"

class WaitStreamEvent(BaseStreamEvent):
    event: Literal["wait"] = "wait"


class TerminalUpdateEventData(BaseEventData):
    shell_id: str
    output: Any = None
    description: Optional[str] = None


class TerminalUpdateStreamEvent(BaseStreamEvent):
    event: Literal["terminal_update"] = "terminal_update"
    data: TerminalUpdateEventData


class FileUpdateEventData(BaseEventData):
    path: str
    content: str = ""
    old_content: Optional[str] = None
    file: Optional[FileInfoResponse] = None


class FileUpdateStreamEvent(BaseStreamEvent):
    event: Literal["file_update"] = "file_update"
    data: FileUpdateEventData

    @classmethod
    async def from_event_async(cls, event: FileUpdateEvent) -> Self:
        file_resp = (
            await FileInfoResponse.from_domain(event.file) if event.file else None
        )
        return cls(
            data=FileUpdateEventData(
                **BaseEventData.base_event_data(event),
                path=event.path,
                content=event.content,
                old_content=event.old_content,
                file=file_resp,
            )
        )


class ErrorEventData(BaseEventData):
    error: str

class ErrorStreamEvent(BaseStreamEvent):
    event: Literal["error"] = "error"
    data: ErrorEventData

class StepEventData(BaseEventData):
    status: ExecutionStatus
    id: str
    description: str

class StepStreamEvent(BaseStreamEvent):
    event: Literal["step"] = "step"
    data: StepEventData

    @classmethod
    def from_event(cls, event: StepEvent) -> Self:
        return cls(
            data=StepEventData(
                **BaseEventData.base_event_data(event),
                status=event.step.status,
                id=event.step.id,
                description=event.step.description
            )
        )

class TitleEventData(BaseEventData):
    title: str

class TitleStreamEvent(BaseStreamEvent):
    event: Literal["title"] = "title"
    data: TitleEventData

class PlanEventData(BaseEventData):
    steps: List[StepEventData]

class PlanStreamEvent(BaseStreamEvent):
    event: Literal["plan"] = "plan"
    data: PlanEventData

    @classmethod
    def from_event(cls, event: PlanEvent) -> Self:
        return cls(
            data=PlanEventData(
                **BaseEventData.base_event_data(event),
                steps=[StepEventData(
                    **BaseEventData.base_event_data(event),
                    status=step.status,
                    id=step.id, 
                    description=step.description
                ) for step in event.plan.steps]
            )
        )

class CommonStreamEvent(BaseStreamEvent):
    event: str
    data: CommonEventData

class AcceptedEventData(BaseEventData):
    submission_id: str
    state: str


class AcceptedStreamEvent(BaseStreamEvent):
    event: Literal["accepted"] = "accepted"
    data: AcceptedEventData

AgentStreamEvent = Union[
    AcceptedStreamEvent,
    PlanStreamEvent,
    MessageStreamEvent,
    TitleStreamEvent,
    ToolStreamEvent,
    StepStreamEvent,
    DoneStreamEvent,
    ErrorStreamEvent,
    WaitStreamEvent,
    TerminalUpdateStreamEvent,
    FileUpdateStreamEvent,
    CommonStreamEvent,
]

SharedAgentStreamEvent = Union[
    AcceptedStreamEvent,
    PlanStreamEvent,
    SharedMessageStreamEvent,
    TitleStreamEvent,
    ToolStreamEvent,
    StepStreamEvent,
    DoneStreamEvent,
    ErrorStreamEvent,
    WaitStreamEvent,
    TerminalUpdateStreamEvent,
    FileUpdateStreamEvent,
    CommonStreamEvent,
]

# Explicit registry: domain event type -> wire stream event class.
# Register new event types here when adding them to AgentEvent.
_EVENT_TYPE_TO_STREAM_CLASS: Dict[str, Type[BaseStreamEvent]] = {
    "accepted": AcceptedStreamEvent,
    "plan": PlanStreamEvent,
    "message": MessageStreamEvent,
    "title": TitleStreamEvent,
    "tool": ToolStreamEvent,
    "step": StepStreamEvent,
    "done": DoneStreamEvent,
    "error": ErrorStreamEvent,
    "wait": WaitStreamEvent,
    "terminal_update": TerminalUpdateStreamEvent,
    "file_update": FileUpdateStreamEvent,
}

class EventMapper:
    """Map AgentEvent (domain) to AgentStreamEvent (WS / REST wire format)"""

    @staticmethod
    async def event_to_stream_event(event: AgentEvent) -> AgentStreamEvent:
        stream_event_class = _EVENT_TYPE_TO_STREAM_CLASS.get(event.type, CommonStreamEvent)
        # Classes needing IO (e.g. signed URLs) define from_event_async
        from_event_async = getattr(stream_event_class, "from_event_async", None)
        if from_event_async is not None:
            return await from_event_async(event)
        return stream_event_class.from_event(event)

    @staticmethod
    async def events_to_stream_events(events: List[AgentEvent]) -> List[AgentStreamEvent]:
        """Create wire event list from domain event list"""
        return [
            await EventMapper.event_to_stream_event(event) for event in events if event
        ]

    @staticmethod
    async def event_to_shared_stream_event(
        event: AgentEvent,
        *,
        session_id: str,
        share_epoch: str,
        shared_files: Dict[str, FileInfo],
        file_service,
    ) -> Optional[SharedAgentStreamEvent]:
        """Map an event for an unauthenticated public session view.

        Public capabilities must be scoped to the live share epoch.  Reusing
        the authenticated mapper here would expose generic file URLs that stay
        usable after the owner unshares the session.
        """

        if isinstance(event, MessageEvent):
            attachments: List[SharedFileInfoResponse] = []
            for attachment in event.attachments or []:
                public_file = shared_files.get(attachment.file_id)
                if not public_file:
                    continue
                file_url = await file_service.create_shared_session_signed_url(
                    session_id,
                    public_file.file_id,
                    share_epoch,
                )
                attachments.append(
                    await SharedFileInfoResponse.from_domain(
                        public_file,
                        session_id,
                        file_url,
                    )
                )
            return SharedMessageStreamEvent(
                data=SharedMessageEventData(
                    **BaseEventData.base_event_data(event),
                    role=event.role,
                    content=event.message,
                    attachments=attachments or None,
                )
            )

        if isinstance(event, ToolEvent):
            # A public share is an unauthenticated transcript, not an execution
            # trace.  Tool arguments routinely contain file bodies, keystrokes,
            # terminal stdin, passwords, tokens, and provider-specific secrets.
            # Keep the stable object shape expected by the replay UI, but never
            # copy raw arguments into a public response.
            if (
                event.tool_name == "message"
                and event.function_name == "message_notify_user"
            ):
                return None

            private_content = event.tool_content
            public_content: Optional[ToolContent] = None
            if isinstance(private_content, BrowserToolContent):
                screenshot = shared_files.get(private_content.screenshot)
                if screenshot:
                    public_content = BrowserToolContent(
                        screenshot=(
                            await file_service.create_shared_session_signed_url(
                                session_id,
                                screenshot.file_id,
                                share_epoch,
                            )
                        )
                    )
            elif isinstance(private_content, PreviewToolContent):
                public_content = _shared_preview_content(private_content)
            return ToolStreamEvent(
                data=ToolEventData(
                    **BaseEventData.base_event_data(event),
                    tool_call_id=event.tool_call_id,
                    name=event.tool_name,
                    status=event.status,
                    function=event.function_name,
                    args={},
                    content=public_content,
                )
            )

        # Fail closed for execution-only events. Terminal output and file
        # updates can contain command output, source text, internal paths, and
        # owner-scoped signed file URLs. Public shares expose only transcript
        # events and explicitly sanitized tool summaries.
        if isinstance(
            event,
            (AcceptedEvent, PlanEvent, StepEvent, TerminalUpdateEvent, FileUpdateEvent),
        ):
            return None
        if isinstance(event, ErrorEvent):
            return ErrorStreamEvent(
                data=ErrorEventData(
                    **BaseEventData.base_event_data(event),
                    error="Agent execution failed",
                )
            )
        if isinstance(event, TitleEvent) or event.type in {"done", "wait"}:
            return await EventMapper.event_to_stream_event(event)
        # Unknown event kinds must be reviewed before they cross the
        # unauthenticated sharing boundary.
        return None

    @staticmethod
    async def events_to_shared_stream_events(
        events: List[AgentEvent],
        *,
        session_id: str,
        share_epoch: str,
        shared_files: Dict[str, FileInfo],
        file_service,
    ) -> List[SharedAgentStreamEvent]:
        """Create a capability-safe event list for a public share."""

        return [
            mapped
            for event in events
            if event
            if (
                mapped := await EventMapper.event_to_shared_stream_event(
                    event,
                    session_id=session_id,
                    share_epoch=share_epoch,
                    shared_files=shared_files,
                    file_service=file_service,
                )
            )
            is not None
        ]
