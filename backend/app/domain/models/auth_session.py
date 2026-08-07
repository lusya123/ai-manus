"""Server-side auth session (opaque id in Redis; not a JWT)."""

from __future__ import annotations

from datetime import datetime, UTC
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class AuthClientType(str, Enum):
    WEB = "web"
    IOS = "ios"
    ANDROID = "android"
    UNKNOWN = "unknown"


class AuthSession(BaseModel):
    """Persisted login session bound to a user."""

    session_id: str
    user_id: str
    client: AuthClientType = AuthClientType.UNKNOWN
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    rotated_from: Optional[str] = None
    # Monotonic per-user fence captured before credential verification or
    # session rotation.  Revoke-all increments the Redis generation before it
    # removes sessions, so a delayed creator cannot resurrect an older login.
    revocation_generation: int = 0
    # Federated providers do not necessarily persist users in Manus' database.
    # Keep the verified identity snapshot server-side so an HttpOnly opaque
    # session can authenticate native WebSockets without exposing provider
    # credentials in the Cookie.
    user_snapshot: Optional[dict[str, Any]] = None

    def remaining_ttl_seconds(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(UTC)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return max(0, int((expires - now).total_seconds()))


class CredentialSource(str, Enum):
    BEARER = "bearer"
    COOKIE = "cookie"
    JWT_GRACE = "jwt_grace"


class ResolvedCredentials(BaseModel):
    """Result of resolving request credentials to a session/user id."""

    session_id: Optional[str] = None
    user_id: str
    source: CredentialSource
    jwt_payload: Optional[dict] = Field(default=None, exclude=True)
