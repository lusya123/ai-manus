from typing import Any, Dict, Optional, List, Type, TypeVar, Generic, get_args, Self
from datetime import datetime, timezone, UTC
from beanie import Document
from pydantic import BaseModel, Field
from app.domain.models.agent import Agent
from app.domain.models.event import AgentEvent
from app.infrastructure.models.memory_serialization import deserialize_memory, serialize_memory
from app.domain.models.session import Session, SessionStatus
from app.domain.models.file import FileInfo
from app.domain.models.user import User, UserRole
from app.domain.models.claw import Claw, ClawStatus, ClawMessage
from app.domain.models.turn_submission import TurnSubmission, TurnSubmissionState
from app.core.config import get_settings
from app.domain.utils.claw_credentials import (
    claw_api_key_digest,
    claw_api_key_hmac_secrets,
)
from app.infrastructure.external.llm.security import (
    decrypt_model_api_key,
    encrypt_model_api_key,
    LegacyModelCredentialMigrationRequired,
)
from pymongo import IndexModel, ASCENDING, DESCENDING

T = TypeVar('T', bound=BaseModel)

class BaseDocument(Document, Generic[T]):
    def __init_subclass__(cls, id_field="id", domain_model_class: Type[T] = None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._ID_FIELD = id_field
        cls._DOMAIN_MODEL_CLASS = domain_model_class
    
    def update_from_domain(self, domain_obj: T) -> None:
        """Update the document from domain model"""
        data = domain_obj.model_dump(exclude={'id', 'created_at'})
        data[self._ID_FIELD] = domain_obj.id
        if hasattr(self, 'updated_at'):
            data['updated_at'] = datetime.now(UTC)
        
        for field, value in data.items():
            setattr(self, field, value)
    
    def to_domain(self) -> T:
        """Convert MongoDB document to domain model"""
        # Convert to dict and map agent_id to id field
        data = self.model_dump(exclude={'id'})
        data['id'] = data.pop(self._ID_FIELD)
        return self._DOMAIN_MODEL_CLASS.model_validate(data)
    
    @classmethod
    def from_domain(cls, domain_obj: T) -> Self:
        """Create a new MongoDB agent from domain"""
        # Convert to dict and map id to agent_id field
        data = domain_obj.model_dump()
        data[cls._ID_FIELD] = data.pop('id')
        return cls.model_validate(data)

class UserDocument(BaseDocument[User], id_field="user_id", domain_model_class=User):
    """MongoDB document for User"""
    user_id: str
    fullname: str
    email: str  # Now required field for login
    password_hash: Optional[str] = None
    role: UserRole = UserRole.USER
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_login_at: Optional[datetime] = None
    auth_provider: Optional[str] = None
    external_id: Optional[str] = None
    external_user: Optional[Dict[str, Any]] = None

    class Settings:
        name = "users"
        indexes = [
            "user_id",
            "fullname",  # Keep fullname index but not unique
            IndexModel([("email", ASCENDING)], unique=True),  # Email as unique index
        ]

class AgentDocument(BaseDocument[Agent], id_field="agent_id", domain_model_class=Agent):
    """MongoDB document for Agent"""
    agent_id: str
    model_id: Optional[str] = None
    model_name: str
    model_provider: str = ""
    api_base: Optional[str] = None
    # ``api_key`` is retained only to read documents created before encrypted
    # BYOK storage was introduced.  New writes always clear it.
    api_key: Optional[str] = Field(default=None, repr=False)
    api_key_encrypted: Optional[str] = Field(default=None, repr=False)
    # None means a pre-marker custom-branch document. Those records may be a
    # copied deployment key or a real user BYOK key and require explicit
    # migration; silently guessing either way can leak or break credentials.
    is_byok: Optional[bool] = None
    temperature: float
    max_tokens: int
    # Raw persisted memory blobs; conversion to/from the domain Memory model
    # (including legacy-format upgrades) is handled by the memory serializer.
    memories: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "agents"
        indexes = [
            "agent_id",
        ]

    def to_domain(self) -> Agent:
        """Convert to the domain Agent, deserializing memory blobs safely."""
        data = self.model_dump(
            exclude={'id', 'memories', 'api_key', 'api_key_encrypted'}
        )
        data['id'] = data.pop(self._ID_FIELD)
        api_key = None
        is_byok = bool(self.is_byok)
        if self.api_key_encrypted:
            api_key = decrypt_model_api_key(
                self.api_key_encrypted, get_settings()
            )
            is_byok = True
        elif self.api_key:
            # ``is_byok`` is the migration discriminator. Releases before
            # per-session BYOK copied the then-current deployment credential
            # into every Agent document with is_byok=false. Comparing that
            # plaintext to today's key/model would break old sessions whenever
            # an operator rotates deployment configuration.
            if self.is_byok is None:
                raise LegacyModelCredentialMigrationRequired(
                    "Legacy Agent credential has no ownership marker; run "
                    "scripts/migrate_agent_credentials.py before serving it"
                )
            if self.is_byok:
                api_key = self.api_key
                is_byok = True
            else:
                api_key = None
                is_byok = False
        data['api_key'] = api_key
        data['is_byok'] = is_byok
        data['memories'] = {
            name: deserialize_memory(raw) for name, raw in (self.memories or {}).items()
        }
        return Agent.model_validate(data)

    @classmethod
    def from_domain(cls, agent: Agent) -> "AgentDocument":
        """Create a document from the domain Agent, serializing memory."""
        data = agent.model_dump(exclude={'memories', 'api_key'})
        data[cls._ID_FIELD] = data.pop('id')
        data['api_key'] = None
        data['api_key_encrypted'] = (
            encrypt_model_api_key(agent.api_key, get_settings())
            if agent.is_byok and agent.api_key
            else None
        )
        doc = cls.model_validate(data)
        doc.memories = {name: serialize_memory(m) for name, m in agent.memories.items()}
        return doc


class SessionDocument(BaseDocument[Session], id_field="session_id", domain_model_class=Session):
    """MongoDB model for Session"""
    session_id: str
    user_id: str  # User ID that owns this session
    sandbox_id: Optional[str] = None
    sandbox_provider: Optional[str] = None
    agent_id: str
    task_id: Optional[str] = None
    title: Optional[str] = None
    unread_message_count: int = 0
    latest_message: Optional[str] = None
    latest_message_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    events: List[AgentEvent] = Field(default_factory=list)
    status: SessionStatus
    files: List[FileInfo] = Field(default_factory=list)
    is_shared: Optional[bool] = False
    # Optional at the persistence boundary for documents written before public
    # capability epochs existed. MongoSessionRepository atomically backfills it
    # before converting the document to the stricter domain Session model.
    share_epoch: Optional[str] = None
    class Settings:
        name = "sessions"
        indexes = [
            "session_id",
            "user_id",
            IndexModel(
                [("user_id", ASCENDING), ("latest_message_at", DESCENDING)],
                name="user_id_latest_message_at",
            ),
        ]


class TurnSubmissionDocument(Document):
    """Independent durable source of truth for accepted logical turns."""

    session_id: str
    submission_id: str
    user_id: str
    agent_id: str
    task_id: Optional[str] = None
    request_hash: str
    input_json: str = Field(repr=False)
    resumes_waiting: bool = False
    state: TurnSubmissionState = TurnSubmissionState.PENDING
    stream_id: Optional[str] = None
    claim_owner: Optional[str] = None
    claim_until: Optional[datetime] = None
    attempt: int = 0
    output_sequence: int = 0
    terminal_event_id: Optional[str] = None
    terminal_error: Optional[str] = None
    expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "turn_submissions"
        indexes = [
            IndexModel(
                [("session_id", ASCENDING), ("submission_id", ASCENDING)],
                unique=True,
                name="session_submission_unique",
            ),
            IndexModel(
                [("session_id", ASCENDING), ("state", ASCENDING)],
                name="session_state",
            ),
            IndexModel(
                [("user_id", ASCENDING), ("state", ASCENDING)],
                name="user_state",
            ),
            IndexModel([("claim_until", ASCENDING)], name="claim_until"),
            IndexModel(
                [("expires_at", ASCENDING)],
                expireAfterSeconds=0,
                name="terminal_turn_ttl",
            ),
        ]

    @classmethod
    def from_domain(cls, turn: TurnSubmission) -> "TurnSubmissionDocument":
        return cls.model_validate(turn.model_dump())

    def to_domain(self) -> TurnSubmission:
        return TurnSubmission.model_validate(self.model_dump(exclude={"id"}))


class TurnQuotaReservation(BaseModel):
    key: str
    session_id: str
    submission_id: str
    reserved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TurnQuotaDocument(Document):
    """Atomic active-turn reservation bucket for one user or session."""

    scope_key: str
    active_turns: List[TurnQuotaReservation] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "turn_quotas"
        indexes = [
            IndexModel([("scope_key", ASCENDING)], unique=True),
        ]


class TurnOutputEventDocument(Document):
    """Per-turn durable output/outbox independent of bounded Session history."""

    session_id: str
    submission_id: str
    event_id: str
    sequence: int
    event: AgentEvent
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None

    class Settings:
        name = "turn_output_events"
        indexes = [
            IndexModel(
                [
                    ("session_id", ASCENDING),
                    ("submission_id", ASCENDING),
                    ("event_id", ASCENDING),
                ],
                unique=True,
                name="turn_output_event_unique",
            ),
            IndexModel(
                [
                    ("session_id", ASCENDING),
                    ("submission_id", ASCENDING),
                    ("sequence", ASCENDING),
                    ("event_id", ASCENDING),
                ],
                name="turn_output_sequence_order",
            ),
            # Retain the timestamp index for compatibility/diagnostics. Replay
            # order uses the atomic sequence above, never a client/event clock.
            IndexModel(
                [
                    ("session_id", ASCENDING),
                    ("submission_id", ASCENDING),
                    ("created_at", ASCENDING),
                    ("event_id", ASCENDING),
                ],
                name="turn_output_order",
            ),
            IndexModel(
                [("expires_at", ASCENDING)],
                expireAfterSeconds=0,
                name="terminal_turn_output_ttl",
            ),
        ]


class ClawDocument(BaseDocument[Claw], id_field="claw_id", domain_model_class=Claw):
    """MongoDB document for Claw instance"""
    claw_id: str
    user_id: str
    container_name: Optional[str] = None
    container_ip: Optional[str] = None
    # Legacy plaintext field, retained only so the repository can lazily
    # migrate pre-HMAC records.  New serialization never writes it.
    api_key: Optional[str] = Field(default=None, repr=False, exclude=True)
    api_key_digest: Optional[str] = Field(default=None, repr=False)
    status: ClawStatus = ClawStatus.CREATING
    error_message: Optional[str] = None
    expires_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    messages: List[ClawMessage] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "claws"
        indexes = [
            "claw_id",
            "api_key_digest",
            IndexModel([("user_id", ASCENDING)], unique=True),  # One claw per user
        ]

    def to_domain(self) -> Claw:
        """Convert without ever reconstructing or exposing key material."""

        data = self.model_dump(
            exclude={"id", "api_key", "api_key_digest", "messages"}
        )
        data["id"] = data.pop(self._ID_FIELD)
        data["api_key"] = None
        return Claw.model_validate(data)

    @classmethod
    def from_domain(cls, claw: Claw) -> "ClawDocument":
        """Persist only the server-keyed digest of an ephemeral runtime key."""

        if not claw.api_key:
            raise ValueError("A new Claw record requires a runtime API key")
        data = claw.model_dump()
        data[cls._ID_FIELD] = data.pop("id")
        data["api_key"] = None
        current_hmac_secret = claw_api_key_hmac_secrets(get_settings())[0]
        data["api_key_digest"] = claw_api_key_digest(
            claw.api_key,
            current_hmac_secret,
        )
        return cls.model_validate(data)
