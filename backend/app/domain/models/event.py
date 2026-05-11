from pydantic import BaseModel, Field, RootModel, field_validator
from typing import Dict, Any, Literal, Optional, Union, List, get_args
from datetime import datetime
import time
import uuid
from enum import Enum
from app.domain.models.plan import Plan, Step
from app.domain.models.file import FileInfo
import json
from app.domain.models.search import SearchResultItem


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
    timestamp: datetime = Field(default_factory=lambda: datetime.now())

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
    """Interactive web preview content"""
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

    @field_validator("message", mode="before")
    @classmethod
    def normalize_message(cls, value: Any) -> str:
        """Normalize model content blocks to plain text for SSE/storage events."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    if isinstance(item.get("text"), str):
                        parts.append(item["text"])
                    elif "content" in item:
                        parts.append(cls.normalize_message(item["content"]))
                    else:
                        parts.append(json.dumps(item, ensure_ascii=False))
                elif hasattr(item, "text") and isinstance(item.text, str):
                    parts.append(item.text)
                else:
                    parts.append(str(item))
            return "\n".join(part for part in parts if part)
        if isinstance(value, dict):
            if isinstance(value.get("text"), str):
                return value["text"]
            if "content" in value:
                return cls.normalize_message(value["content"])
            return json.dumps(value, ensure_ascii=False)
        return str(value)

class DoneEvent(BaseEvent):
    """Done event"""
    type: Literal["done"] = "done"

class WaitEvent(BaseEvent):
    """Wait event"""
    type: Literal["wait"] = "wait"

AgentEvent = Union[
    ErrorEvent,
    PlanEvent, 
    ToolEvent,
    StepEvent,
    MessageEvent,
    DoneEvent,
    TitleEvent,
    WaitEvent,
]
