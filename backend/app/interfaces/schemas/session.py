from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Optional, List
from uuid import UUID
from app.core.config import get_settings
from app.interfaces.schemas.event import AgentStreamEvent, SharedAgentStreamEvent
from app.domain.models.file import FileInfo
from app.domain.models.session import SessionStatus, SessionSummary, TaskMode
from app.domain.utils.time import epoch_seconds


class ChatAttachment(BaseModel):
    """File attachment reference in a chat request"""
    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(min_length=1, max_length=256)
    filename: Optional[str] = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_configured_lengths(self) -> "ChatAttachment":
        settings = get_settings()
        if len(self.file_id) > settings.chat_attachment_file_id_max_chars:
            raise ValueError("file_id is too long")
        if (
            self.filename is not None
            and len(self.filename) > settings.chat_attachment_filename_max_chars
        ):
            raise ValueError("filename is too long")
        return self

    def to_domain(self) -> FileInfo:
        return FileInfo(file_id=self.file_id, filename=self.filename)


class ChatRequest(BaseModel):
    """Chat request schema"""
    model_config = ConfigDict(extra="forbid")

    timestamp: Optional[int] = None
    message: Optional[str] = None
    attachments: Optional[List[ChatAttachment]] = Field(default=None, max_length=10)
    event_id: Optional[str] = Field(default=None, max_length=128)
    submission_id: Optional[UUID] = None

    @field_validator("message")
    @classmethod
    def validate_message_bytes(cls, message: Optional[str]) -> Optional[str]:
        if message is not None and len(message.encode("utf-8")) > get_settings().chat_max_message_bytes:
            raise ValueError("message is too large")
        return message

    @model_validator(mode="after")
    def validate_submission(self) -> "ChatRequest":
        settings = get_settings()
        if self.attachments and len(self.attachments) > settings.chat_max_attachments:
            raise ValueError("too many attachments")
        if (self.message or self.attachments) and self.submission_id is None:
            raise ValueError(
                "submission_id is required when message or attachments are present"
            )
        return self


class AgentModelConfigRequest(BaseModel):
    """Per-session model credentials imported from Sub2API."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    api_key: Optional[str] = Field(default=None, min_length=1, max_length=8192, repr=False)
    api_base: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    model_name: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    model_provider: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )

    @model_validator(mode="after")
    def validate_selection_mode(self) -> "AgentModelConfigRequest":
        custom_fields = ("api_key", "api_base", "model_name", "model_provider")
        provided_custom = [name for name in custom_fields if getattr(self, name)]
        if self.model_id and provided_custom:
            raise ValueError("model_id cannot be combined with custom model credentials")
        if provided_custom and len(provided_custom) != len(custom_fields):
            missing = ", ".join(
                name for name in custom_fields if not getattr(self, name)
            )
            raise ValueError(f"Custom model configuration is missing: {missing}")
        return self


class AgentModelConfigResponse(BaseModel):
    """Per-session model metadata safe to expose to the frontend."""

    model_id: Optional[str] = None
    api_base: Optional[str] = None
    model_name: Optional[str] = None
    model_provider: Optional[str] = None


class CreateSessionRequest(BaseModel):
    agent_model_config: Optional[AgentModelConfigRequest] = Field(
        default=None, alias="model_config"
    )


class PreviewUrlRequest(BaseModel):
    url: str
    expire_minutes: int = Field(15, ge=1, le=15)


class ShellViewRequest(BaseModel):
    """Shell view request schema"""
    session_id: str


class CreateSessionResponse(BaseModel):
    """Create session response schema"""
    session_id: str


class GetSessionResponse(BaseModel):
    """Get session response schema"""
    session_id: str
    title: Optional[str] = None
    status: SessionStatus
    model_config = ConfigDict(populate_by_name=True)

    events: List[AgentStreamEvent] = Field(default_factory=list)
    is_shared: bool = False
    agent_model_config: Optional[AgentModelConfigResponse] = Field(
        default=None, alias="model_config"
    )
    is_favorite: bool = False
    is_pinned: bool = False
    project_id: Optional[str] = None
    task_mode: TaskMode = TaskMode.AGENT


class ListSessionItem(BaseModel):
    """List session item schema"""
    session_id: str
    title: Optional[str] = None
    latest_message: Optional[str] = None
    latest_message_at: Optional[int] = None
    status: SessionStatus
    unread_message_count: int
    is_shared: bool = False
    is_favorite: bool = False
    is_pinned: bool = False
    project_id: Optional[str] = None
    task_mode: TaskMode = TaskMode.AGENT

    @staticmethod
    def from_domain(summary: SessionSummary) -> 'ListSessionItem':
        return ListSessionItem(
            session_id=summary.id,
            title=summary.title,
            status=summary.status,
            unread_message_count=summary.unread_message_count,
            latest_message=summary.latest_message,
            latest_message_at=(
                epoch_seconds(summary.latest_message_at)
                if summary.latest_message_at
                else None
            ),
            is_shared=summary.is_shared,
            is_favorite=summary.is_favorite,
            is_pinned=summary.is_pinned,
            project_id=summary.project_id,
            task_mode=summary.task_mode or TaskMode.AGENT,
        )


class ListSessionResponse(BaseModel):
    """List session response schema"""
    sessions: List[ListSessionItem]


class ConsoleRecord(BaseModel):
    """Console record schema"""
    ps1: str
    command: str
    output: str


class ShellViewResponse(BaseModel):
    """Shell view response schema"""
    output: str
    session_id: str
    console: Optional[List[ConsoleRecord]] = None


class UpdateSessionTitleRequest(BaseModel):
    """Update session title request schema"""
    title: str


class UpdateSessionTitleResponse(BaseModel):
    """Update session title response schema"""
    session_id: str
    title: str


class FavoriteSessionResponse(BaseModel):
    """Favorite session response schema"""
    session_id: str
    is_favorite: bool


class PinSessionRequest(BaseModel):
    """Pin / unpin session"""
    is_pinned: bool = True


class PinSessionResponse(BaseModel):
    """Pin session response schema"""
    session_id: str
    is_pinned: bool


class FavoriteLibraryFileResponse(BaseModel):
    """Favorite library file response schema"""
    file_id: str
    is_favorite: bool


class MoveSessionProjectRequest(BaseModel):
    """Move session to project (null to remove from project)"""
    project_id: Optional[str] = None


class MoveSessionProjectResponse(BaseModel):
    session_id: str
    project_id: Optional[str] = None


class UpdateSessionTaskModeRequest(BaseModel):
    """Update session task mode (agent | chat)"""
    task_mode: TaskMode


class UpdateSessionTaskModeResponse(BaseModel):
    session_id: str
    task_mode: TaskMode


class LibraryFileItem(BaseModel):
    session_id: str
    session_title: Optional[str] = None
    file_id: Optional[str] = None
    filename: Optional[str] = None
    file_path: Optional[str] = None
    content_type: Optional[str] = None
    size: Optional[int] = None
    upload_date: Optional[str] = None
    is_favorite: bool = False
    latest_message_at: Optional[int] = None


class LibraryResponse(BaseModel):
    files: List[LibraryFileItem]


class ShareSessionResponse(BaseModel):
    """Share session response schema"""
    session_id: str
    is_shared: bool


class SharedSessionResponse(BaseModel):
    """Shared session response schema (for public access)"""
    session_id: str
    title: Optional[str] = None
    status: SessionStatus
    events: List[SharedAgentStreamEvent] = Field(default_factory=list)
    is_shared: bool
