"""Protocol for opaque server-side auth sessions (Redis-backed)."""

from __future__ import annotations

from typing import Optional, Protocol

from app.domain.models.auth_session import AuthSession


class SessionStore(Protocol):
    """Store and index auth sessions for revoke / sliding TTL."""

    async def get_user_generation(self, user_id: str) -> int:
        """Return the current revoke-all generation for a user."""
        ...

    async def create(
        self,
        session: AuthSession,
        ttl_seconds: int,
        *,
        expected_generation: Optional[int] = None,
    ) -> bool:
        """Persist and index a session iff the user's generation still matches."""
        ...

    async def get(self, session_id: str) -> Optional[AuthSession]:
        """Load a session by opaque id, or None if missing/expired."""
        ...

    async def touch(self, session_id: str, ttl_seconds: int) -> Optional[AuthSession]:
        """Sliding renewal: refresh last_seen_at / expires_at and Redis TTL."""
        ...

    async def rotate(
        self,
        old_session_id: str,
        session: AuthSession,
        ttl_seconds: int,
        *,
        expected_generation: int,
    ) -> bool:
        """Atomically replace a live session iff its user fence still matches."""
        ...

    async def delete(self, session_id: str) -> bool:
        """Delete one session and remove it from the user index."""
        ...

    async def delete_all_for_user(self, user_id: str) -> int:
        """Delete every session for a user. Returns count removed."""
        ...

    async def list_ids_for_user(self, user_id: str) -> list[str]:
        """Return session ids currently indexed for a user."""
        ...
