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

class BaseSSEEvent(BaseModel):
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

class MessageSSEEvent(BaseSSEEvent):
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


class SharedMessageSSEEvent(BaseSSEEvent):
    event: Literal["message"] = "message"
    data: SharedMessageEventData

class ToolEventData(BaseEventData):
    tool_call_id: str
    name: str
    status: ToolStatus
    function: str
    args: Dict[str, Any]
    content: Optional[ToolContent] = None

class ToolSSEEvent(BaseSSEEvent):
    event: Literal["tool"] = "tool"
    data: ToolEventData

    @classmethod
    async def from_event_async(cls, event: ToolEvent) -> Self:
        content = event.tool_content
        if isinstance(content, BrowserToolContent):
            from app.interfaces.dependencies import get_file_service
            content = BrowserToolContent(
                screenshot=await get_file_service().create_internal_signed_url(
                    content.screenshot
                )
            )
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

class DoneSSEEvent(BaseSSEEvent):
    event: Literal["done"] = "done"

class WaitSSEEvent(BaseSSEEvent):
    event: Literal["wait"] = "wait"

class ErrorEventData(BaseEventData):
    error: str

class ErrorSSEEvent(BaseSSEEvent):
    event: Literal["error"] = "error"
    data: ErrorEventData

class StepEventData(BaseEventData):
    status: ExecutionStatus
    id: str
    description: str

class StepSSEEvent(BaseSSEEvent):
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

class TitleSSEEvent(BaseSSEEvent):
    event: Literal["title"] = "title"
    data: TitleEventData

class PlanEventData(BaseEventData):
    steps: List[StepEventData]

class PlanSSEEvent(BaseSSEEvent):
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

class CommonSSEEvent(BaseSSEEvent):
    event: str
    data: CommonEventData


class AcceptedEventData(BaseEventData):
    submission_id: str
    state: str


class AcceptedSSEEvent(BaseSSEEvent):
    event: Literal["accepted"] = "accepted"
    data: AcceptedEventData

AgentSSEEvent = Union[
    AcceptedSSEEvent,
    PlanSSEEvent,
    MessageSSEEvent,
    TitleSSEEvent,
    ToolSSEEvent,
    StepSSEEvent,
    DoneSSEEvent,
    ErrorSSEEvent,
    WaitSSEEvent,
    CommonSSEEvent,
]

SharedAgentSSEEvent = Union[
    AcceptedSSEEvent,
    PlanSSEEvent,
    SharedMessageSSEEvent,
    TitleSSEEvent,
    ToolSSEEvent,
    StepSSEEvent,
    DoneSSEEvent,
    ErrorSSEEvent,
    WaitSSEEvent,
    CommonSSEEvent,
]

# Explicit registry: domain event type -> SSE event class.
# Register new event types here when adding them to AgentEvent.
_EVENT_TYPE_TO_SSE_CLASS: Dict[str, Type[BaseSSEEvent]] = {
    "accepted": AcceptedSSEEvent,
    "plan": PlanSSEEvent,
    "message": MessageSSEEvent,
    "title": TitleSSEEvent,
    "tool": ToolSSEEvent,
    "step": StepSSEEvent,
    "done": DoneSSEEvent,
    "error": ErrorSSEEvent,
    "wait": WaitSSEEvent,
}

class EventMapper:
    """Map AgentEvent (domain) to SSEEvent (wire format)"""

    @staticmethod
    async def event_to_sse_event(event: AgentEvent) -> Optional[AgentSSEEvent]:
        # Plans, steps, and notification tool calls are private execution
        # progress. Persist them for recovery, but do not expose them as chat.
        if isinstance(event, (PlanEvent, StepEvent)):
            return None
        if (
            isinstance(event, ToolEvent)
            and event.tool_name == "message"
            and event.function_name == "message_notify_user"
        ):
            return None
        sse_event_class = _EVENT_TYPE_TO_SSE_CLASS.get(event.type, CommonSSEEvent)
        # Classes needing IO (e.g. signed URLs) define from_event_async
        from_event_async = getattr(sse_event_class, "from_event_async", None)
        if from_event_async is not None:
            return await from_event_async(event)
        return sse_event_class.from_event(event)

    @staticmethod
    async def events_to_sse_events(events: List[AgentEvent]) -> List[AgentSSEEvent]:
        """Create SSE event list from event list"""
        return [
            mapped
            for event in events
            if event
            if (mapped := await EventMapper.event_to_sse_event(event)) is not None
        ]

    @staticmethod
    async def event_to_shared_sse_event(
        event: AgentEvent,
        *,
        session_id: str,
        share_epoch: str,
        shared_files: Dict[str, FileInfo],
        file_service,
    ) -> Optional[SharedAgentSSEEvent]:
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
            return SharedMessageSSEEvent(
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
            return ToolSSEEvent(
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

        return await EventMapper.event_to_sse_event(event)

    @staticmethod
    async def events_to_shared_sse_events(
        events: List[AgentEvent],
        *,
        session_id: str,
        share_epoch: str,
        shared_files: Dict[str, FileInfo],
        file_service,
    ) -> List[SharedAgentSSEEvent]:
        """Create a capability-safe event list for a public share."""

        return [
            mapped
            for event in events
            if event
            if (
                mapped := await EventMapper.event_to_shared_sse_event(
                    event,
                    session_id=session_id,
                    share_epoch=share_epoch,
                    shared_files=shared_files,
                    file_service=file_service,
                )
            )
            is not None
        ]
