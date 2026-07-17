from pydantic import BaseModel, Field, RootModel, model_validator
from typing import ClassVar, Dict, Any, Literal, Optional, Union, List, get_args
from datetime import datetime
import time
import uuid
from enum import Enum
from app.domain.models.plan import Plan, Step
from app.domain.models.file import FileInfo
import json
from app.domain.models.search import SearchResultItem
from app.domain.utils.model_output import (
    HIDDEN_CONTENT_TYPES,
    VISIBLE_CONTENT_TYPES,
    normalize_model_content,
)
from app.domain.utils.time import utc_now


class PlanStatus(str, Enum):
    """Plan status enum"""
    CREATED = "created"
    UPDATED = "updated"
    COMPLETED = "completed"


class StepStatus(str, Enum):
    """Step status enum"""
    STARTED = "started"
    FAILED = "failed"
    COMPLETED = "completed"


class ToolStatus(str, Enum):
    """Tool status enum"""
    CALLING = "calling"
    CALLED = "called"


class BaseEvent(BaseModel):
    """Base class for agent events"""
    type: Literal[""] = ""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    # Stable logical client submission UUID. Never overwrite this with a Redis
    # Stream ID; transport cursors live in ``transport_id``.
    turn_id: Optional[str] = None
    transport_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=utc_now)


class AcceptedEvent(BaseEvent):
    """Early acknowledgement that Mongo durably accepted a logical turn."""

    type: Literal["accepted"] = "accepted"
    submission_id: str
    state: str

class ErrorEvent(BaseEvent):
    """Error event"""
    type: Literal["error"] = "error"
    error: str

class PlanEvent(BaseEvent):
    """Plan related events"""
    type: Literal["plan"] = "plan"
    plan: Plan
    status: PlanStatus
    step: Optional[Step] = None

class BrowserToolContent(BaseModel):
    """Browser tool content"""
    screenshot: str


class PreviewToolContent(BaseModel):
    """Interactive web preview content."""

    url: str
    title: Optional[str] = None

class SearchToolContent(BaseModel):
    """Search tool content"""
    results: List[SearchResultItem]

class ShellToolContent(BaseModel):
    """Shell tool content"""
    console: Any

class FileToolContent(BaseModel):
    """File tool content"""
    content: str

class McpToolContent(BaseModel):
    """MCP tool content"""
    result: Any

ToolContent = Union[
    BrowserToolContent,
    PreviewToolContent,
    SearchToolContent,
    ShellToolContent,
    FileToolContent,
    McpToolContent
]

class ToolEvent(BaseEvent):
    """Tool related events"""
    type: Literal["tool"] = "tool"
    tool_call_id: str
    tool_name: str
    tool_content: Optional[ToolContent] = None
    function_name: str
    function_args: Dict[str, Any]
    status: ToolStatus
    function_result: Optional[Any] = None

class TitleEvent(BaseEvent):
    """Title event"""
    type: Literal["title"] = "title"
    title: str

class StepEvent(BaseEvent):
    """Step related events"""
    type: Literal["step"] = "step"
    step: Step
    status: StepStatus

class MessageEvent(BaseEvent):
    """Message event"""
    type: Literal["message"] = "message"
    role: Literal["user", "assistant"] = "assistant"
    message: str
    attachments: Optional[List[FileInfo]] = None

    _VISIBLE_CONTENT_TYPES: ClassVar[set[str]] = VISIBLE_CONTENT_TYPES
    _HIDDEN_CONTENT_TYPES: ClassVar[set[str]] = HIDDEN_CONTENT_TYPES

    @model_validator(mode="before")
    @classmethod
    def normalize_event_message(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "message" not in data:
            return data
        # User text is data, not model output. Preserve literal <think> examples.
        if data.get("role") == "user" and isinstance(data["message"], str):
            return data
        return {**data, "message": cls.normalize_message(data["message"])}

    @classmethod
    def normalize_message(cls, value: Any) -> str:
        """Normalize model content blocks to safe user-visible text."""
        return normalize_model_content(value)

class DoneEvent(BaseEvent):
    """Done event"""
    type: Literal["done"] = "done"

class WaitEvent(BaseEvent):
    """Wait event"""
    type: Literal["wait"] = "wait"

AgentEvent = Union[
    AcceptedEvent,
    ErrorEvent,
    PlanEvent, 
    ToolEvent,
    StepEvent,
    MessageEvent,
    DoneEvent,
    TitleEvent,
    WaitEvent,
]
