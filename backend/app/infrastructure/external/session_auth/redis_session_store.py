"""Redis implementation of SessionStore for opaque auth sessions."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, UTC
from typing import Optional

from app.domain.models.auth_session import AuthSession
from app.infrastructure.storage.redis import get_redis

logger = logging.getLogger(__name__)

SESSION_KEY_PREFIX = "session:"
USER_SESSIONS_KEY_PREFIX = "user_sessions:"
USER_SESSION_GENERATION_KEY_PREFIX = "user_session_generation:"


class RedisSessionStore:
    """Redis-backed auth session store with per-user index for revoke-all."""

    def __init__(self) -> None:
        self._redis = get_redis()

    def _session_key(self, session_id: str) -> str:
        return f"{SESSION_KEY_PREFIX}{session_id}"

    def _user_index_key(self, user_id: str) -> str:
        return f"{USER_SESSIONS_KEY_PREFIX}{user_id}"

    def _user_generation_key(self, user_id: str) -> str:
        return f"{USER_SESSION_GENERATION_KEY_PREFIX}{user_id}"

    async def get_user_generation(self, user_id: str) -> int:
        await self._redis.initialize()
        raw = await self._redis.client.get(self._user_generation_key(user_id))
        return int(raw or 0)

    async def create(
        self,
        session: AuthSession,
        ttl_seconds: int,
        *,
        expected_generation: Optional[int] = None,
    ) -> bool:
        await self._redis.initialize()
        client = self._redis.client
        generation = (
            session.revocation_generation
            if expected_generation is None
            else int(expected_generation)
        )
        if session.revocation_generation != generation:
            return False
        payload = session.model_dump_json()
        script = """
        -- auth-session-create-v2
        local current = tonumber(redis.call('GET', KEYS[3]) or '0')
        local expected = tonumber(ARGV[4])
        if not current or not expected or current ~= expected then
          return 0
        end
        local index_existed = redis.call('EXISTS', KEYS[2])
        redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
        redis.call('SADD', KEYS[2], ARGV[3])
        local index_ttl = redis.call('TTL', KEYS[2])
        local requested_ttl = tonumber(ARGV[2])
        if index_existed == 0 or index_ttl == -1 then
          redis.call('EXPIRE', KEYS[2], requested_ttl)
        elseif index_ttl >= 0 and index_ttl < requested_ttl then
          redis.call('EXPIRE', KEYS[2], requested_ttl)
        end
        return 1
        """
        created = await client.eval(
            script,
            3,
            self._session_key(session.session_id),
            self._user_index_key(session.user_id),
            self._user_generation_key(session.user_id),
            payload,
            str(max(1, ttl_seconds)),
            session.session_id,
            str(generation),
        )
        return bool(created)

    async def get(self, session_id: str) -> Optional[AuthSession]:
        await self._redis.initialize()
        script = """
        -- auth-session-get-v2
        local raw = redis.call('GET', KEYS[1])
        if not raw then
          return false
        end
        local ok, payload = pcall(cjson.decode, raw)
        if not ok or type(payload) ~= 'table'
          or type(payload['user_id']) ~= 'string'
          or type(payload['session_id']) ~= 'string' then
          redis.call('DEL', KEYS[1])
          return false
        end
        local user_id = payload['user_id']
        local current = tonumber(
          redis.call('GET', ARGV[1] .. user_id) or '0'
        )
        local captured = tonumber(payload['revocation_generation'] or 0)
        if not current or not captured or current ~= captured then
          redis.call('DEL', KEYS[1])
          redis.call(
            'SREM', ARGV[2] .. user_id, payload['session_id']
          )
          return false
        end
        return raw
        """
        raw = await self._redis.client.eval(
            script,
            1,
            self._session_key(session_id),
            USER_SESSION_GENERATION_KEY_PREFIX,
            USER_SESSIONS_KEY_PREFIX,
        )
        if not raw:
            return None
        try:
            return AuthSession.model_validate_json(raw)
        except Exception:
            logger.warning("Corrupt auth session payload for %s", session_id)
            await self.delete(session_id)
            return None

    async def touch(self, session_id: str, ttl_seconds: int) -> Optional[AuthSession]:
        now = datetime.now(UTC)
        await self._redis.initialize()
        script = """
        -- auth-session-touch-v2
        local raw = redis.call('GET', KEYS[1])
        if not raw then
          return false
        end
        local ok, payload = pcall(cjson.decode, raw)
        if not ok or type(payload) ~= 'table'
          or type(payload['user_id']) ~= 'string'
          or type(payload['session_id']) ~= 'string' then
          redis.call('DEL', KEYS[1])
          return false
        end
        local user_id = payload['user_id']
        local current = tonumber(
          redis.call('GET', ARGV[2] .. user_id) or '0'
        )
        local captured = tonumber(payload['revocation_generation'] or 0)
        if not current or not captured or current ~= captured then
          redis.call('DEL', KEYS[1])
          redis.call(
            'SREM', ARGV[3] .. user_id, payload['session_id']
          )
          return false
        end
        local index_key = ARGV[3] .. user_id
        local index_existed = redis.call('EXISTS', index_key)
        redis.call('SADD', index_key, payload['session_id'])
        local index_ttl = redis.call('TTL', index_key)
        local requested_ttl = tonumber(ARGV[1])
        if index_existed == 0 or index_ttl == -1 then
          redis.call('EXPIRE', index_key, requested_ttl)
        elseif index_ttl >= 0 and index_ttl < requested_ttl then
          redis.call('EXPIRE', index_key, requested_ttl)
        end
        payload['last_seen_at'] = ARGV[4]
        payload['expires_at'] = ARGV[5]
        local updated = cjson.encode(payload)
        redis.call('SET', KEYS[1], updated, 'EX', ARGV[1])
        return updated
        """
        ttl = max(1, ttl_seconds)
        raw = await self._redis.client.eval(
            script,
            1,
            self._session_key(session_id),
            str(ttl),
            USER_SESSION_GENERATION_KEY_PREFIX,
            USER_SESSIONS_KEY_PREFIX,
            now.isoformat(),
            (now + timedelta(seconds=ttl)).isoformat(),
        )
        if not raw:
            return None
        try:
            session = AuthSession.model_validate_json(raw)
            session.last_seen_at = now
            session.expires_at = now + timedelta(seconds=ttl)
            return session
        except Exception:
            logger.warning("Corrupt auth session payload for %s", session_id)
            await self.delete(session_id)
            return None

    async def rotate(
        self,
        old_session_id: str,
        session: AuthSession,
        ttl_seconds: int,
        *,
        expected_generation: int,
    ) -> bool:
        """Atomically consume the old token and publish its replacement."""

        generation = int(expected_generation)
        if session.revocation_generation != generation:
            return False
        await self._redis.initialize()
        script = """
        -- auth-session-rotate-v2
        local current = tonumber(redis.call('GET', KEYS[4]) or '0')
        local expected = tonumber(ARGV[5])
        if not current or not expected or current ~= expected then
          return 0
        end
        local raw = redis.call('GET', KEYS[1])
        if not raw then
          return 0
        end
        local ok, old = pcall(cjson.decode, raw)
        if not ok or type(old) ~= 'table'
          or old['user_id'] ~= ARGV[4]
          or old['session_id'] ~= ARGV[3]
          or tonumber(old['revocation_generation'] or 0) ~= expected then
          return 0
        end
        local index_ttl = redis.call('TTL', KEYS[3])
        redis.call('DEL', KEYS[1])
        redis.call('SREM', KEYS[3], ARGV[3])
        redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
        redis.call('SADD', KEYS[3], ARGV[6])
        local requested_ttl = tonumber(ARGV[2])
        if index_ttl == -2 or index_ttl == -1 then
          redis.call('EXPIRE', KEYS[3], requested_ttl)
        elseif index_ttl >= 0 then
          redis.call(
            'EXPIRE', KEYS[3], math.max(index_ttl, requested_ttl)
          )
        end
        return 1
        """
        rotated = await self._redis.client.eval(
            script,
            4,
            self._session_key(old_session_id),
            self._session_key(session.session_id),
            self._user_index_key(session.user_id),
            self._user_generation_key(session.user_id),
            session.model_dump_json(),
            str(max(1, ttl_seconds)),
            old_session_id,
            session.user_id,
            str(generation),
            session.session_id,
        )
        return bool(rotated)

    async def delete(self, session_id: str) -> bool:
        await self._redis.initialize()
        script = """
        -- auth-session-delete-v2
        local raw = redis.call('GET', KEYS[1])
        if not raw then
          return 0
        end
        local ok, payload = pcall(cjson.decode, raw)
        redis.call('DEL', KEYS[1])
        if ok and type(payload) == 'table'
          and type(payload['user_id']) == 'string'
          and type(payload['session_id']) == 'string' then
          redis.call(
            'SREM', ARGV[1] .. payload['user_id'], payload['session_id']
          )
        end
        return 1
        """
        deleted = await self._redis.client.eval(
            script,
            1,
            self._session_key(session_id),
            USER_SESSIONS_KEY_PREFIX,
        )
        return bool(deleted)

    async def delete_all_for_user(self, user_id: str) -> int:
        await self._redis.initialize()
        client = self._redis.client
        script = """
        -- auth-session-revoke-all-v3
        local indexed = redis.call('SCARD', KEYS[1])
        redis.call('INCR', KEYS[2])
        redis.call('UNLINK', KEYS[1])
        return indexed
        """
        revoked = await client.eval(
            script,
            2,
            self._user_index_key(user_id),
            self._user_generation_key(user_id),
        )
        return int(revoked or 0)

    async def list_ids_for_user(self, user_id: str) -> list[str]:
        await self._redis.initialize()
        members = await self._redis.client.smembers(self._user_index_key(user_id))
        return list(members or [])
