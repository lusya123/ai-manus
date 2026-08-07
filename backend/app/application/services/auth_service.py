import hashlib
import asyncio
import base64
import binascii
import hmac
import secrets
from typing import Any, Optional
from datetime import datetime, timedelta, UTC
import httpx
from app.domain.models.user import User, UserRole
from app.domain.repositories.user_repository import UserRepository
from app.domain.external.session_store import SessionStore
from app.domain.models.auth_session import (
    AuthSession,
    AuthClientType,
    CredentialSource,
    ResolvedCredentials,
)
from app.application.errors.exceptions import (
    UnauthorizedError,
    ValidationError,
    BadRequestError,
    ServiceUnavailableError,
    TooManyRequestsError,
)
from app.core.config import get_settings
from app.application.services.token_service import TokenService
from app.domain.models.auth import AuthToken
from app.domain.utils.error_reporting import safe_exception_summary
import logging

logger = logging.getLogger(__name__)


class AuthService:
    """Authentication service handling user authentication and authorization"""

    _PASSWORD_SCHEME = "pbkdf2_sha256"
    _MIN_PASSWORD_HASH_ROUNDS = 600_000
    _MAX_ACCEPTED_PASSWORD_HASH_ROUNDS = 10_000_000

    def __init__(
        self,
        user_repository: UserRepository,
        token_service: TokenService,
        session_store: SessionStore,
    ):
        self.user_repository = user_repository
        self.settings = get_settings()
        self.token_service = token_service
        self.session_store = session_store

    def _hash_password(self, password: str) -> str:
        """Create a self-describing hash with a fresh per-user salt."""

        rounds = max(
            int(self.settings.password_hash_rounds or 0),
            self._MIN_PASSWORD_HASH_ROUNDS,
        )
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, rounds
        )
        encoded_salt = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
        encoded_digest = (
            base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        )
        return f"{self._PASSWORD_SCHEME}${rounds}${encoded_salt}${encoded_digest}"

    @staticmethod
    def _decode_password_hash_part(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )

    def _legacy_password_hash(self, password: str) -> str:
        """Reproduce the pre-migration global-salt/low-cost hash exactly."""

        salt = self.settings.password_salt or ""
        rounds = int(self.settings.password_legacy_hash_rounds or 10)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            rounds,
        )
        return salt + digest.hex()

    def _verify_password(self, password: str, password_hash: str) -> bool:
        if not password_hash:
            return False
        try:
            parts = password_hash.split("$")
            if len(parts) == 4 and parts[0] == self._PASSWORD_SCHEME:
                rounds = int(parts[1])
                if not (
                    1 <= rounds <= self._MAX_ACCEPTED_PASSWORD_HASH_ROUNDS
                ):
                    return False
                salt = self._decode_password_hash_part(parts[2])
                expected = self._decode_password_hash_part(parts[3])
                actual = hashlib.pbkdf2_hmac(
                    "sha256", password.encode("utf-8"), salt, rounds
                )
                return hmac.compare_digest(actual, expected)

            # Legacy records have no metadata. Use the explicitly retained old
            # deployment settings, then migrate after a successful login.
            return hmac.compare_digest(
                self._legacy_password_hash(password), password_hash
            )
        except (ValueError, TypeError, binascii.Error) as exc:
            logger.warning("Malformed password hash rejected: %s", type(exc).__name__)
            return False

    def _password_needs_rehash(self, password_hash: str) -> bool:
        try:
            scheme, rounds, _salt, _digest = password_hash.split("$")
            configured_rounds = max(
                int(self.settings.password_hash_rounds or 0),
                self._MIN_PASSWORD_HASH_ROUNDS,
            )
            return scheme != self._PASSWORD_SCHEME or int(rounds) < configured_rounds
        except (ValueError, TypeError):
            return True

    def _generate_user_id(self) -> str:
        return secrets.token_urlsafe(16)

    def _sub2api_auth_url(self, path: str) -> str:
        base_url = (self.settings.sub2api_base_url or "").rstrip("/")
        if not base_url:
            raise UnauthorizedError("Sub2API auth is not configured")
        return f"{base_url}/{path.lstrip('/')}"

    @staticmethod
    def _sub2api_revocation_key(token: str) -> str:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"auth:sub2api:revoked:{digest}"

    @staticmethod
    def _sub2api_token_digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @classmethod
    def _sub2api_family_mapping_key(cls, token: str) -> str:
        return f"auth:sub2api:family-of:{cls._sub2api_token_digest(token)}"

    @staticmethod
    def _sub2api_family_revocation_key(family_id: str) -> str:
        return f"auth:sub2api:family-revoked:{family_id}"

    @classmethod
    def _sub2api_refresh_used_key(cls, token: str) -> str:
        return f"auth:sub2api:refresh-used:{cls._sub2api_token_digest(token)}"

    @staticmethod
    def _jwt_revocation_key(token: str) -> str:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"auth:jwt:revoked:{digest}"

    @staticmethod
    def _jwt_family_revocation_key(session_id: str) -> str:
        return f"auth:jwt:family-revoked:{session_id}"

    @staticmethod
    def _jwt_migrated_session_key(session_id: str) -> str:
        return f"auth:jwt:migrated-session:{session_id}"

    @staticmethod
    def _jwt_jti_revocation_key(jti: str) -> str:
        return f"auth:jwt:jti-revoked:{jti}"

    @staticmethod
    def _jwt_user_cutoff_key(user_id: str) -> str:
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
        return f"auth:jwt:user-revoked-before:{digest}"

    @staticmethod
    def _jwt_refresh_used_key(token: str, payload: dict[str, Any]) -> str:
        identifier = payload.get("jti") or hashlib.sha256(
            token.encode("utf-8")
        ).hexdigest()
        return f"auth:jwt:refresh-used:{identifier}"

    def _maximum_revocation_ttl(self) -> int:
        return max(
            int(self.settings.jwt_refresh_token_expire_days * 24 * 60 * 60),
            24 * 60 * 60,
        )

    def _sub2api_revocation_ttl(self) -> int:
        return max(
            int(self.settings.sub2api_refresh_token_max_age_days * 24 * 60 * 60),
            self._maximum_revocation_ttl(),
        )

    def _payload_ttl(
        self, payload: dict[str, Any], *, fallback: Optional[int] = None
    ) -> int:
        expires_at = payload.get("exp")
        if isinstance(expires_at, (int, float)):
            return max(
                1,
                int(expires_at - datetime.now(UTC).timestamp()) + 1,
            )
        return fallback or self._maximum_revocation_ttl()

    async def _is_token_revoked(self, token: str) -> bool:
        """Check Manus-local revocation without storing the raw bearer token."""
        from app.infrastructure.storage.redis import get_redis

        try:
            redis = get_redis().client
        except RuntimeError:
            # Unit-level service use may not have run application lifespan yet.
            return False
        try:
            if self.settings.auth_provider == "sub2api":
                if await redis.exists(self._sub2api_revocation_key(token)):
                    return True
                family_id = await redis.get(
                    self._sub2api_family_mapping_key(token)
                )
                return bool(
                    family_id
                    and await redis.exists(
                        self._sub2api_family_revocation_key(str(family_id))
                    )
                )

            payload = self.token_service.verify_token(token)
            keys = [self._jwt_revocation_key(token)]
            if payload:
                if payload.get("sid"):
                    keys.append(
                        self._jwt_family_revocation_key(str(payload["sid"]))
                    )
                if payload.get("jti"):
                    keys.append(
                        self._jwt_jti_revocation_key(str(payload["jti"]))
                    )
            for key in keys:
                if await redis.exists(key):
                    return True

            if payload and payload.get("sub"):
                cutoff = await redis.get(
                    self._jwt_user_cutoff_key(str(payload["sub"]))
                )
                if cutoff is not None:
                    issued_ms = payload.get("iat_ms")
                    if isinstance(issued_ms, (int, float)):
                        return int(issued_ms) <= int(cutoff)
                    issued_seconds = payload.get("iat")
                    if isinstance(issued_seconds, (int, float)):
                        return int(issued_seconds) <= int(int(cutoff) / 1000)
            return False
        except Exception as exc:
            # The production application requires Redis at startup. If it later
            # becomes unavailable, fail closed instead of resurrecting a token
            # that may have been logged out.
            logger.warning(
                "Token revocation check failed: %s",
                safe_exception_summary(exc),
            )
            return True

    async def _is_sub2api_token_revoked(self, token: str) -> bool:
        """Backward-compatible wrapper for Sub2API-specific callers/tests."""
        return await self._is_token_revoked(token)

    async def _revoke_sub2api_token(self, token: str) -> None:
        from app.infrastructure.storage.redis import get_redis

        await get_redis().client.set(
            self._sub2api_revocation_key(token),
            "1",
            ex=self._sub2api_revocation_ttl(),
        )

    async def _begin_sub2api_refresh(self, token: str) -> tuple[str, str]:
        """Reserve one opaque refresh token and resolve its rotation family."""

        from app.infrastructure.storage.redis import get_redis

        redis = get_redis().client
        ttl = self._sub2api_revocation_ttl()
        mapping_key = self._sub2api_family_mapping_key(token)
        family_id = await redis.get(mapping_key)
        if not family_id:
            family_id = self._sub2api_token_digest(token)
            await redis.set(mapping_key, family_id, ex=ttl, nx=True)
            family_id = await redis.get(mapping_key) or family_id
        if (
            await redis.exists(self._sub2api_revocation_key(token))
            or await redis.exists(
                self._sub2api_family_revocation_key(str(family_id))
            )
        ):
            raise UnauthorizedError("Invalid refresh token")

        owner = secrets.token_urlsafe(24)
        reserved = await redis.set(
            self._sub2api_refresh_used_key(token),
            owner,
            ex=max(30, int(self.settings.sub2api_timeout_seconds * 3)),
            nx=True,
        )
        if not reserved:
            raise UnauthorizedError("Refresh token has already been used")
        return str(family_id), owner

    async def _release_sub2api_refresh_reservation(
        self, token: str, owner: str
    ) -> None:
        from app.infrastructure.storage.redis import get_redis

        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
          return redis.call('DEL', KEYS[1])
        end
        return 0
        """
        try:
            await get_redis().client.eval(
                script, 1, self._sub2api_refresh_used_key(token), owner
            )
        except Exception as exc:
            logger.warning(
                "Failed to release external refresh reservation: %s",
                safe_exception_summary(exc),
            )

    async def _commit_sub2api_refresh_rotation(
        self,
        old_refresh_token: str,
        family_id: str,
        result: AuthToken,
    ) -> bool:
        """Atomically link rotated tokens and reject a concurrent logout."""

        from app.infrastructure.storage.redis import get_redis

        ttl = self._sub2api_revocation_ttl()
        new_refresh = result.refresh_token or ""
        script = """
        -- sub2api-rotation-commit-v1
        redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
        if ARGV[3] == '1' then
          redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
        end
        redis.call('SET', KEYS[6], 'used', 'EX', ARGV[2])
        if redis.call('EXISTS', KEYS[3]) == 1 then
          redis.call('SET', KEYS[4], '1', 'EX', ARGV[2])
          if ARGV[3] == '1' then
            redis.call('SET', KEYS[5], '1', 'EX', ARGV[2])
          end
          return 0
        end
        return 1
        """
        committed = await get_redis().client.eval(
            script,
            6,
            self._sub2api_family_mapping_key(result.access_token),
            self._sub2api_family_mapping_key(new_refresh or old_refresh_token),
            self._sub2api_family_revocation_key(family_id),
            self._sub2api_revocation_key(result.access_token),
            self._sub2api_revocation_key(new_refresh or old_refresh_token),
            self._sub2api_refresh_used_key(old_refresh_token),
            family_id,
            str(ttl),
            "1" if new_refresh else "0",
        )
        await self._revoke_sub2api_token(old_refresh_token)
        return bool(committed)

    async def _revoke_sub2api_family(
        self, access_token: Optional[str], refresh_token: str
    ) -> None:
        from app.infrastructure.storage.redis import get_redis

        redis = get_redis().client
        ttl = self._sub2api_revocation_ttl()
        family_id = await redis.get(
            self._sub2api_family_mapping_key(refresh_token)
        ) or self._sub2api_token_digest(refresh_token)
        await redis.set(
            self._sub2api_family_mapping_key(refresh_token),
            family_id,
            ex=ttl,
        )
        if access_token:
            await redis.set(
                self._sub2api_family_mapping_key(access_token),
                family_id,
                ex=ttl,
            )
        await redis.set(
            self._sub2api_family_revocation_key(str(family_id)),
            "1",
            ex=ttl,
        )
        await self._revoke_sub2api_token(refresh_token)
        if access_token:
            await self._revoke_sub2api_token(access_token)

    async def _consume_jwt_refresh(
        self, token: str, payload: dict[str, Any]
    ) -> None:
        """Atomically make a refresh token single-use across all replicas."""

        from app.infrastructure.storage.redis import get_redis

        try:
            consumed = await get_redis().client.set(
                self._jwt_refresh_used_key(token, payload),
                "1",
                ex=self._payload_ttl(payload),
                nx=True,
            )
        except Exception as exc:
            logger.warning(
                "Refresh-token replay check failed: %s",
                safe_exception_summary(exc),
            )
            raise UnauthorizedError("Token refresh failed") from exc
        if not consumed:
            raise UnauthorizedError("Refresh token has already been used")

    async def _commit_jwt_session_migration(
        self,
        family_id: str,
        session_id: str,
        ttl_seconds: int,
    ) -> tuple[bool, Optional[str]]:
        """Atomically replace a JWT family's migrated opaque session.

        The displaced opaque key is removed in the same Redis script as the
        mapping replacement.  Returning its id lets the caller perform an
        idempotent SessionStore cleanup as well, while the Lua deletion closes
        the process-crash gap between publishing the winner and revoking the
        previous browser session.
        """

        from app.infrastructure.storage.redis import get_redis

        script = """
        -- jwt-migration-commit-v2
        if redis.call('EXISTS', KEYS[1]) == 1 then
          return {0, ''}
        end
        local previous = redis.call('GET', KEYS[2])
        redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
        if previous and previous ~= ARGV[1] then
          local previous_key = ARGV[3] .. previous
          local raw = redis.call('GET', previous_key)
          redis.call('DEL', previous_key)
          if raw then
            local ok, payload = pcall(cjson.decode, raw)
            if ok and type(payload) == 'table'
              and payload['session_id'] == previous
              and type(payload['user_id']) == 'string' then
              redis.call(
                'SREM', ARGV[4] .. payload['user_id'], previous
              )
            end
          end
        end
        return {1, previous or ''}
        """
        result = await get_redis().client.eval(
            script,
            2,
            self._jwt_family_revocation_key(family_id),
            self._jwt_migrated_session_key(family_id),
            session_id,
            str(max(1, ttl_seconds)),
            "session:",
            "user_sessions:",
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise RuntimeError("Invalid JWT migration commit response")
        committed = bool(result[0])
        displaced = result[1]
        if isinstance(displaced, bytes):
            displaced = displaced.decode("utf-8")
        displaced_session_id = str(displaced) if displaced else None
        if displaced_session_id == session_id:
            displaced_session_id = None
        return committed, displaced_session_id

    async def _cleanup_displaced_jwt_session(
        self,
        displaced_session_id: Optional[str],
    ) -> None:
        """Finish an idempotent cleanup already made authoritative by Lua."""

        if not displaced_session_id:
            return
        try:
            await self.session_store.delete(displaced_session_id)
        except Exception as exc:
            # The commit Lua has already deleted the production Redis key and
            # user index member atomically.  A redundant adapter cleanup must
            # not turn that safe commit into a stale mapped-session failure.
            logger.warning(
                "Displaced JWT migration session cleanup failed: %s",
                safe_exception_summary(exc),
            )

    async def claim_jwt_session_migration(
        self,
        family_id: str,
        session_id: str,
        ttl_seconds: int,
        *,
        replace_session_id: Optional[str] = None,
    ) -> Optional[str]:
        """Claim one opaque browser session for a legacy JWT family.

        Access JWTs are reusable until they expire, so ``/auth/me`` exchanges
        can race across tabs or replicas.  This differs from refresh-token
        migration, where the refresh credential is consumed exactly once and a
        later opaque rotation intentionally replaces the family mapping.

        Return the winning opaque session id, or ``None`` when the family was
        revoked.  ``replace_session_id`` permits a caller to replace a mapping
        only after it proved that exact winner is stale; a concurrent healthy
        winner is never overwritten.
        """

        from app.infrastructure.storage.redis import get_redis

        script = """
        -- jwt-browser-session-claim-v1
        if redis.call('EXISTS', KEYS[1]) == 1 then
          return ''
        end
        local current = redis.call('GET', KEYS[2])
        if current then
          if ARGV[3] ~= '' and current == ARGV[3] then
            redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
            return ARGV[1]
          end
          return current
        end
        redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2], 'NX')
        return redis.call('GET', KEYS[2]) or ''
        """
        winner = await get_redis().client.eval(
            script,
            2,
            self._jwt_family_revocation_key(family_id),
            self._jwt_migrated_session_key(family_id),
            session_id,
            str(max(1, ttl_seconds)),
            replace_session_id or "",
        )
        return str(winner) if winner else None

    async def _revoke_jwt_family(
        self, family_id: str, *, ttl_seconds: int
    ) -> Optional[str]:
        """Revoke a JWT family and atomically detach its migrated Redis session."""

        from app.infrastructure.storage.redis import get_redis

        script = """
        -- jwt-family-revoke-v1
        redis.call('SET', KEYS[1], '1', 'EX', ARGV[1])
        local session_id = redis.call('GET', KEYS[2])
        redis.call('DEL', KEYS[2])
        return session_id or ''
        """
        session_id = await get_redis().client.eval(
            script,
            2,
            self._jwt_family_revocation_key(family_id),
            self._jwt_migrated_session_key(family_id),
            str(ttl_seconds),
        )
        return str(session_id) if session_id else None

    async def revoke_user_tokens(self, user_id: str) -> None:
        """Invalidate every token issued before a credential/state change."""

        from app.infrastructure.storage.redis import get_redis

        cutoff_ms = int(datetime.now(UTC).timestamp() * 1000)
        await get_redis().client.set(
            self._jwt_user_cutoff_key(user_id),
            str(cutoff_ms),
            ex=self._maximum_revocation_ttl(),
        )
        # Publish the cutoff before deleting sessions so a concurrent JWT
        # migration cannot create a session in the delete/set race window.
        await self.session_store.delete_all_for_user(user_id)

    async def _is_jwt_user_migration_revoked(self, marker: str) -> bool:
        """Check the cutoff embedded in a legacy no-family JWT migration marker."""

        from app.infrastructure.storage.redis import get_redis

        try:
            _prefix, user_id, issued_ms = marker.split(":", 2)
            cutoff = await get_redis().client.get(
                self._jwt_user_cutoff_key(user_id)
            )
            return cutoff is not None and int(issued_ms) <= int(cutoff)
        except (TypeError, ValueError):
            return True

    async def enforce_rate_limit(
        self,
        scope: str,
        identifier: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> None:
        """Apply a Redis-authoritative fixed-window authentication limit."""

        if limit <= 0 or window_seconds <= 0:
            raise ServiceUnavailableError("Authentication rate limit is misconfigured")
        from app.infrastructure.storage.redis import get_redis

        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        key = f"auth:rate:{scope}:{digest}"
        script = """
        local current = redis.call('INCR', KEYS[1])
        if current == 1 then
          redis.call('EXPIRE', KEYS[1], ARGV[1])
        end
        return current
        """
        try:
            current = int(
                await get_redis().client.eval(
                    script, 1, key, str(window_seconds)
                )
            )
        except Exception as exc:
            logger.warning(
                "Authentication rate-limit authority failed: %s",
                safe_exception_summary(exc),
            )
            raise ServiceUnavailableError(
                "Authentication temporarily unavailable"
            ) from exc
        if current > limit:
            raise TooManyRequestsError("Too many authentication attempts")

    @staticmethod
    def _coerce_datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                logger.debug("Failed to parse Sub2API datetime")
        return datetime.now(UTC)

    def _map_sub2api_user(self, data: dict[str, Any]) -> User:
        external_id = str(
            data.get("id") or data.get("user_id") or data.get("sub") or ""
        )
        if not external_id:
            raise UnauthorizedError("Sub2API user response is missing id")
        email = str(data.get("email") or "").strip().lower()
        if not email:
            email = f"sub2api-{external_id}@localhost"
        fullname = (
            str(data.get("username") or "").strip()
            or str(data.get("display_name") or "").strip()
            or str(data.get("name") or "").strip()
            or email.split("@", 1)[0]
            or f"Sub2API User {external_id}"
        )
        if len(fullname) < 2:
            fullname = f"Sub2API User {external_id}"
        role = (
            UserRole.ADMIN
            if str(data.get("role") or "").lower() == "admin"
            else UserRole.USER
        )
        status = str(data.get("status") or "active").lower()
        active = status not in {
            "inactive", "disabled", "banned", "deleted", "blocked"
        }
        return User(
            id=f"sub2api:{external_id}",
            fullname=fullname,
            email=email,
            role=role,
            is_active=active,
            created_at=self._coerce_datetime(data.get("created_at")),
            updated_at=self._coerce_datetime(data.get("updated_at")),
            last_login_at=(
                self._coerce_datetime(data["last_login_at"])
                if data.get("last_login_at")
                else None
            ),
            auth_provider="sub2api",
            external_id=external_id,
            external_user=data,
        )

    async def _verify_sub2api_token(self, token: str) -> Optional[User]:
        if not token:
            return None
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.sub2api_timeout_seconds
            ) as client:
                response = await client.get(
                    self._sub2api_auth_url(self.settings.sub2api_auth_me_path),
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "Sub2API auth request failed: %s",
                safe_exception_summary(exc),
            )
            return None
        if response.status_code != 200:
            logger.warning("Sub2API auth rejected token: status=%s", response.status_code)
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if (
            not isinstance(data, dict)
            or payload.get("code") not in (None, 0)
        ):
            return None
        return self._map_sub2api_user(data)

    async def _refresh_sub2api_access_token(self, refresh_token: str) -> AuthToken:
        try:
            family_id, reservation_owner = await self._begin_sub2api_refresh(
                refresh_token
            )
        except UnauthorizedError:
            raise
        except Exception as exc:
            logger.warning(
                "Sub2API refresh authority failed: %s",
                safe_exception_summary(exc),
            )
            raise UnauthorizedError("Token refresh failed") from exc
        rotation_finalized = False
        try:
            try:
                async with httpx.AsyncClient(
                    timeout=self.settings.sub2api_timeout_seconds
                ) as client:
                    response = await client.post(
                        self._sub2api_auth_url(
                            self.settings.sub2api_auth_refresh_path
                        ),
                        json={"refresh_token": refresh_token},
                    )
            except httpx.HTTPError as exc:
                logger.warning(
                    "Sub2API token refresh request failed: %s",
                    safe_exception_summary(exc),
                )
                raise UnauthorizedError("Token refresh failed")
            if response.status_code != 200:
                raise UnauthorizedError("Invalid refresh token")
            try:
                payload = response.json()
            except ValueError:
                raise UnauthorizedError("Invalid refresh token")
            data = payload.get("data") if isinstance(payload, dict) else None
            if (
                not isinstance(data, dict)
                or payload.get("code") not in (None, 0)
                or not data.get("access_token")
            ):
                raise UnauthorizedError("Invalid refresh token")
            result = AuthToken(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token"),
                token_type=str(data.get("token_type") or "bearer").lower(),
            )
            committed = await self._commit_sub2api_refresh_rotation(
                refresh_token, family_id, result
            )
            rotation_finalized = True
            if not committed:
                raise UnauthorizedError("Login was logged out during refresh")
            return result
        finally:
            if not rotation_finalized:
                await self._release_sub2api_refresh_reservation(
                    refresh_token, reservation_owner
                )
    
    def _generate_session_id(self) -> str:
        return secrets.token_urlsafe(32)

    def _ttl_seconds_for_client(self, client: AuthClientType) -> int:
        if client in (AuthClientType.IOS, AuthClientType.ANDROID):
            return max(1, self.settings.session_app_ttl_days * 24 * 3600)
        return max(1, self.settings.session_web_ttl_days * 24 * 3600)

    def parse_client(self, raw: Optional[str]) -> AuthClientType:
        if not raw:
            return AuthClientType.UNKNOWN
        try:
            return AuthClientType(raw.lower())
        except ValueError:
            return AuthClientType.UNKNOWN

    def _build_auth_session(
        self,
        user: User,
        *,
        client: AuthClientType,
        ip: Optional[str],
        user_agent: Optional[str],
        rotated_from: Optional[str],
        revocation_generation: int,
    ) -> tuple[AuthSession, int]:
        now = datetime.now(UTC)
        ttl = self._ttl_seconds_for_client(client)
        return AuthSession(
            session_id=self._generate_session_id(),
            user_id=user.id,
            client=client,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl),
            last_seen_at=now,
            ip=ip,
            user_agent=user_agent,
            rotated_from=rotated_from,
            revocation_generation=revocation_generation,
            user_snapshot=(
                user.model_dump(mode="json")
                if self.settings.auth_provider == "sub2api"
                else None
            ),
        ), ttl

    async def create_auth_session(
        self,
        user: User,
        client: AuthClientType = AuthClientType.WEB,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        rotated_from: Optional[str] = None,
        expected_generation: Optional[int] = None,
    ) -> AuthSession:
        if expected_generation is None:
            expected_generation = await self.session_store.get_user_generation(
                user.id
            )
        session, ttl = self._build_auth_session(
            user,
            client=client,
            ip=ip,
            user_agent=user_agent,
            rotated_from=rotated_from,
            revocation_generation=expected_generation,
        )
        created = await self.session_store.create(
            session,
            ttl,
            expected_generation=expected_generation,
        )
        if not created:
            raise UnauthorizedError("Login was revoked during session creation")
        return session

    async def _rotate_auth_session(
        self,
        old_session: AuthSession,
        user: User,
        *,
        rotated_from: Optional[str],
    ) -> AuthSession:
        generation = old_session.revocation_generation
        session, ttl = self._build_auth_session(
            user,
            client=old_session.client,
            ip=old_session.ip,
            user_agent=old_session.user_agent,
            rotated_from=rotated_from,
            revocation_generation=generation,
        )
        rotated = await self.session_store.rotate(
            old_session.session_id,
            session,
            ttl,
            expected_generation=generation,
        )
        if not rotated:
            raise UnauthorizedError("Session was logged out during refresh")
        return session

    async def _user_from_id(self, user_id: str) -> Optional[User]:
        if self.settings.auth_provider == "password":
            user = await self.user_repository.get_user_by_id(user_id)
            if not user or not user.is_active:
                return None
            return user
        if self.settings.auth_provider == "local" and user_id == "local_admin":
            return User(
                id="local_admin",
                fullname="Local Admin",
                email=self.settings.local_auth_email,
                role=UserRole.ADMIN,
                is_active=True,
                auth_provider="local",
            )
        return None

    async def resolve_session_token(self, token: str) -> Optional[ResolvedCredentials]:
        session = await self.session_store.get(token)
        if not session:
            return None
        ttl = self._ttl_seconds_for_client(session.client)
        session = await self.session_store.touch(token, ttl)
        if not session:
            return None
        return ResolvedCredentials(
            session_id=session.session_id,
            user_id=session.user_id,
            source=CredentialSource.BEARER,
            jwt_payload=session.user_snapshot,
        )

    async def resolve_jwt_grace(self, token: str) -> Optional[ResolvedCredentials]:
        if self.settings.auth_provider == "sub2api":
            if await self._is_token_revoked(token):
                return None
            user = await self._verify_sub2api_token(token)
            if not user or not user.is_active:
                return None
            return ResolvedCredentials(
                session_id=None,
                user_id=user.id,
                source=CredentialSource.JWT_GRACE,
                jwt_payload=user.model_dump(mode="json"),
            )
        if not self.settings.session_jwt_grace_enabled:
            return None
        payload = self.token_service.verify_token(token)
        if not payload or await self._is_token_revoked(token):
            return None
        if payload.get("type", "access") != "access":
            return None
        user_id = payload.get("sub")
        if not user_id:
            return None
        return ResolvedCredentials(
            session_id=None,
            user_id=user_id,
            source=CredentialSource.JWT_GRACE,
            jwt_payload=payload,
        )

    async def resolve_credentials(
        self,
        bearer_token: Optional[str] = None,
        cookie_session_id: Optional[str] = None,
    ) -> Optional[ResolvedCredentials]:
        """Resolve credentials: Bearer session/JWT → Cookie session_id."""
        if bearer_token:
            resolved = await self.resolve_session_token(bearer_token)
            if resolved:
                resolved.source = CredentialSource.BEARER
                return resolved
            resolved = await self.resolve_jwt_grace(bearer_token)
            if resolved:
                return resolved

        if cookie_session_id:
            resolved = await self.resolve_session_token(cookie_session_id)
            if resolved:
                resolved.source = CredentialSource.COOKIE
                return resolved

        return None

    async def user_from_resolved(self, resolved: ResolvedCredentials) -> Optional[User]:
        if self.settings.auth_provider == "sub2api" and resolved.jwt_payload:
            try:
                user = User.model_validate(resolved.jwt_payload)
            except (TypeError, ValueError):
                return None
            return user if user.is_active else None
        if resolved.source == CredentialSource.JWT_GRACE and resolved.jwt_payload:
            if self.settings.auth_provider == "password":
                return await self._user_from_id(resolved.user_id)
            return User(
                id=resolved.jwt_payload.get("sub"),
                fullname=resolved.jwt_payload.get("fullname") or "user",
                email=resolved.jwt_payload.get("email"),
                role=UserRole(resolved.jwt_payload.get("role", "user")),
                is_active=resolved.jwt_payload.get("is_active", True),
            )
        return await self._user_from_id(resolved.user_id)

    async def register_user(
        self, fullname: str, password: str, email: str, role: UserRole = UserRole.USER
    ) -> User:
        logger.info("Registering user")

        if self.settings.auth_provider != "password":
            raise BadRequestError("Registration is not allowed")
        if not self.settings.registration_enabled:
            raise BadRequestError("Public registration is disabled")
        if not fullname or len(fullname.strip()) < 2:
            raise ValidationError("Full name must be at least 2 characters long")
        if not email or '@' not in email:
            raise ValidationError("Valid email is required")
        if not password or len(password) < 6:
            raise ValidationError("Password must be at least 6 characters long")
        if await self.user_repository.email_exists(email):
            raise ValidationError("Email already exists")

        password_hash = await asyncio.to_thread(self._hash_password, password)
        user = User(
            id=self._generate_user_id(),
            fullname=fullname.strip(),
            email=email.lower(),
            password_hash=password_hash,
            role=role,
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        created_user = await self.user_repository.create_user(user)
        logger.info(f"User registered successfully: {created_user.id}")
        return created_user

    async def _authenticate_user_with_generation(
        self, email: str, password: str
    ) -> tuple[Optional[User], Optional[int]]:
        """Authenticate and capture the user's session fence before validation."""

        logger.debug("Authenticating user")

        if self.settings.auth_provider == "none":
            user = User(
                id="anonymous",
                fullname="anonymous",
                email="anonymous@localhost",
                role=UserRole.USER,
                is_active=True,
            )
            generation = await self.session_store.get_user_generation(user.id)
            return user, generation

        if self.settings.auth_provider == "local":
            generation = await self.session_store.get_user_generation("local_admin")
            if (
                email == self.settings.local_auth_email
                and password == self.settings.local_auth_password
            ):
                return User(
                    id="local_admin",
                    fullname="Local Admin",
                    email=email,
                    role=UserRole.ADMIN,
                    is_active=True,
                    auth_provider="local",
                ), generation
            logger.warning("Local authentication failed")
            return None, None

        if self.settings.auth_provider == "password":
            user = await self.user_repository.get_user_by_email(email)
            if not user:
                logger.warning("Password authentication failed: user not found")
                return None, None
            if not user.is_active:
                logger.warning(
                    "Password authentication failed: inactive user_id=%s",
                    user.id,
                )
                return None, None
            if not user.password_hash:
                logger.warning(
                    "Password authentication failed: missing hash for user_id=%s",
                    user.id,
                )
                return None, None

            # Capture before the expensive password verification.  A
            # concurrent password change/logout-all advances this generation;
            # the later session create then fails its Redis CAS instead of
            # resurrecting a credential that was verified before revocation.
            generation = await self.session_store.get_user_generation(user.id)
            password_valid = await asyncio.to_thread(
                self._verify_password, password, user.password_hash
            )
            if not password_valid:
                logger.warning(
                    "Password authentication failed: invalid credential for "
                    "user_id=%s",
                    user.id,
                )
                return None, None

            if self._password_needs_rehash(user.password_hash):
                user.password_hash = await asyncio.to_thread(
                    self._hash_password, password
                )
            
            # Update last login
            user.update_last_login()
            await self.user_repository.update_user(user)

            logger.info("User authenticated successfully: user_id=%s", user.id)
            return user, generation

        if self.settings.auth_provider == "sub2api":
            raise BadRequestError("Use Sub2API authentication token")

        raise ValueError(f"Unsupported auth provider: {self.settings.auth_provider}")

    async def authenticate_user(self, email: str, password: str) -> Optional[User]:
        """Authenticate user by email and password."""

        user, _generation = await self._authenticate_user_with_generation(
            email, password
        )
        return user

    async def login_with_session(
        self,
        email: str,
        password: str,
        client: AuthClientType = AuthClientType.WEB,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> AuthToken:
        user, generation = await self._authenticate_user_with_generation(
            email, password
        )
        if not user:
            raise UnauthorizedError("Invalid email or password")

        session = await self.create_auth_session(
            user,
            client=client,
            ip=ip,
            user_agent=user_agent,
            expected_generation=generation,
        )
        return AuthToken(
            access_token=session.session_id,
            refresh_token=session.session_id,
            token_type="bearer",
            user=user,
        )

    async def login_with_tokens(self, email: str, password: str) -> AuthToken:
        """Backward-compatible alias — issues Redis sessions, not JWTs."""
        return await self.login_with_session(email, password, client=AuthClientType.WEB)

    async def establish_sub2api_session(
        self,
        access_token: str,
        *,
        client: AuthClientType = AuthClientType.WEB,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        rotated_from: Optional[str] = None,
    ) -> tuple[User, AuthSession]:
        """Exchange a verified external credential for a local opaque session."""

        if self.settings.auth_provider != "sub2api":
            raise BadRequestError("External session exchange is not available")
        if not access_token or await self._is_token_revoked(access_token):
            raise UnauthorizedError("Invalid access token")
        user = await self._verify_sub2api_token(access_token)
        if not user or not user.is_active:
            raise UnauthorizedError("Invalid access token")
        session = await self.create_auth_session(
            user,
            client=client,
            ip=ip,
            user_agent=user_agent,
            rotated_from=rotated_from,
        )
        return user, session

    async def refresh_access_token(
        self,
        refresh_token: Optional[str] = None,
        cookie_session_id: Optional[str] = None,
        rotate: bool = False,
    ) -> AuthToken:
        token = refresh_token or cookie_session_id
        if not token:
            raise UnauthorizedError("Missing session credentials")

        if self.settings.auth_provider == "sub2api":
            if not refresh_token:
                raise UnauthorizedError("Missing refresh token")
            return await self._refresh_sub2api_access_token(refresh_token)

        session = await self.session_store.get(token)
        if session:
            user = await self._user_from_id(session.user_id)
            if not user or not user.is_active:
                raise UnauthorizedError("User not found or inactive")

            if rotate:
                migrated_from = (
                    session.rotated_from
                    if (session.rotated_from or "").startswith("jwt-")
                    else token
                )
                new_session = await self._rotate_auth_session(
                    session,
                    user,
                    rotated_from=migrated_from,
                )
                try:
                    if migrated_from.startswith("jwt-family:"):
                        family_id = migrated_from.removeprefix("jwt-family:")
                        (
                            committed,
                            displaced_session_id,
                        ) = await self._commit_jwt_session_migration(
                            family_id,
                            new_session.session_id,
                            self._maximum_revocation_ttl(),
                        )
                        if committed:
                            await self._cleanup_displaced_jwt_session(
                                displaced_session_id
                            )
                    elif migrated_from.startswith("jwt-user:"):
                        committed = not await self._is_jwt_user_migration_revoked(
                            migrated_from
                        )
                    else:
                        committed = True
                except Exception as exc:
                    await self.session_store.delete(new_session.session_id)
                    logger.warning(
                        "Opaque session rotation authority failed: %s",
                        safe_exception_summary(exc),
                    )
                    raise UnauthorizedError("Token refresh failed") from exc
                if not committed:
                    await self.session_store.delete(new_session.session_id)
                    raise UnauthorizedError("Invalid refresh token")
                return AuthToken(
                    access_token=new_session.session_id,
                    refresh_token=new_session.session_id,
                    token_type="bearer",
                )

            ttl = self._ttl_seconds_for_client(session.client)
            if not await self.session_store.touch(token, ttl):
                raise UnauthorizedError("Session was logged out during refresh")
            return AuthToken(
                access_token=token,
                refresh_token=token,
                token_type="bearer",
            )

        if not self.settings.session_jwt_grace_enabled:
            raise UnauthorizedError("Invalid refresh token")

        payload = self.token_service.verify_token(token, expected_type="refresh")
        if not payload:
            raise UnauthorizedError("Invalid refresh token")
        user_id = payload.get("sub")
        session_generation = (
            await self.session_store.get_user_generation(str(user_id))
            if user_id
            else None
        )
        if await self._is_token_revoked(token):
            raise UnauthorizedError("Invalid refresh token")
        await self._consume_jwt_refresh(token, payload)

        user = await self._user_from_id(str(user_id)) if user_id else None
        if not user or not user.is_active:
            if self.settings.auth_provider == "local" and user_id:
                user = User(
                    id=str(user_id),
                    fullname=str(payload.get("fullname") or "Local User"),
                    email=str(payload.get("email") or "local@localhost"),
                    role=UserRole(payload.get("role", "user")),
                    is_active=True,
                    auth_provider="local",
                )
            else:
                raise UnauthorizedError("User not found or inactive")

        family_id = payload.get("sid")
        issued_ms = payload.get("iat_ms")
        if not isinstance(issued_ms, (int, float)):
            issued_ms = int(float(payload.get("iat") or 0) * 1000)
        migrated_from = (
            f"jwt-family:{family_id}"
            if family_id
            else f"jwt-user:{user.id}:{int(issued_ms)}"
        )
        new_session = await self.create_auth_session(
            user,
            client=AuthClientType.UNKNOWN,
            rotated_from=migrated_from,
            expected_generation=session_generation,
        )
        try:
            if family_id:
                (
                    committed,
                    displaced_session_id,
                ) = await self._commit_jwt_session_migration(
                    str(family_id),
                    new_session.session_id,
                    self._payload_ttl(payload),
                )
                if committed:
                    await self._cleanup_displaced_jwt_session(
                        displaced_session_id
                    )
            else:
                # Legacy JWTs without a family id are governed by the per-user
                # cutoff. Re-check after session creation to close the refresh /
                # logout race without weakening the fail-closed revocation path.
                committed = not await self._is_token_revoked(token)
        except Exception as exc:
            await self.session_store.delete(new_session.session_id)
            logger.warning(
                "JWT session migration authority failed: %s",
                safe_exception_summary(exc),
            )
            raise UnauthorizedError("Token refresh failed") from exc
        if not committed:
            await self.session_store.delete(new_session.session_id)
            raise UnauthorizedError("Invalid refresh token")
        return AuthToken(
            access_token=new_session.session_id,
            refresh_token=new_session.session_id,
            token_type="bearer",
        )
    async def verify_token(self, token: str) -> Optional[User]:
        if not token:
            return None
        resolved = await self.resolve_session_token(token)
        if resolved:
            return await self.user_from_resolved(resolved)
        resolved = await self.resolve_jwt_grace(token)
        if resolved:
            return await self.user_from_resolved(resolved)
        return None

    async def logout(
        self,
        token: Optional[str],
        *,
        refresh_token: Optional[str] = None,
        cookie_session_id: Optional[str] = None,
    ) -> bool:
        """Logout by revoking the whole local family or both external tokens."""
        if self.settings.auth_provider == "none":
            raise BadRequestError("Logout is not allowed")
        if self.settings.auth_provider == "sub2api":
            if not refresh_token:
                raise BadRequestError(
                    "refresh_token is required to fully revoke Sub2API login"
                )
            await self._revoke_sub2api_family(token, refresh_token)
            if cookie_session_id:
                await self.session_store.delete(cookie_session_id)
            return True

        if token:
            session = await self.session_store.get(token)
            if session:
                migrated_from = session.rotated_from or ""
                if migrated_from.startswith("jwt-family:"):
                    family_id = migrated_from.removeprefix("jwt-family:")
                    linked_session_id = await self._revoke_jwt_family(
                        family_id,
                        ttl_seconds=self._maximum_revocation_ttl(),
                    )
                    if linked_session_id and linked_session_id != token:
                        await self.session_store.delete(linked_session_id)
                elif migrated_from.startswith("jwt-user:"):
                    await self.revoke_user_tokens(session.user_id)
                    return True
                return await self.session_store.delete(token)

        from app.infrastructure.storage.redis import get_redis

        redis = get_redis().client

        access_payload = (
            self.token_service.verify_token(token, expected_type="access")
            if token
            else None
        )
        refresh_payload = (
            self.token_service.verify_token(
                refresh_token, expected_type="refresh"
            )
            if refresh_token
            else None
        )
        if not access_payload and not refresh_payload:
            raise UnauthorizedError("A valid access or refresh token is required")
        if access_payload and refresh_payload:
            if (
                access_payload.get("sub") != refresh_payload.get("sub")
                or access_payload.get("sid") != refresh_payload.get("sid")
            ):
                raise BadRequestError("Access and refresh tokens do not match")
        payload = access_payload or refresh_payload
        ttl_seconds = self._maximum_revocation_ttl()
        if payload.get("sid"):
            linked_session_id = await self._revoke_jwt_family(
                str(payload["sid"]), ttl_seconds=ttl_seconds
            )
            if linked_session_id:
                await self.session_store.delete(linked_session_id)
        else:
            # Legacy access tokens predate session-family IDs. Revoke this
            # exact token and all older tokens for the account so their paired
            # legacy refresh token cannot resurrect the login.
            if token:
                await redis.set(
                    self._jwt_revocation_key(token),
                    "1",
                    ex=self._payload_ttl(payload, fallback=ttl_seconds),
                )
            if refresh_token:
                await redis.set(
                    self._jwt_revocation_key(refresh_token),
                    "1",
                    ex=self._payload_ttl(
                        refresh_payload or payload,
                        fallback=ttl_seconds,
                    ),
                )
            if payload.get("sub"):
                await self.revoke_user_tokens(str(payload["sub"]))
        return True

    async def logout_all(self, user_id: str) -> int:
        if self.settings.auth_provider == "none":
            raise BadRequestError("Logout is not allowed")
        count = await self.session_store.delete_all_for_user(user_id)
        if self.settings.auth_provider != "sub2api":
            from app.infrastructure.storage.redis import get_redis

            cutoff_ms = int(datetime.now(UTC).timestamp() * 1000)
            await get_redis().client.set(
                self._jwt_user_cutoff_key(user_id),
                str(cutoff_ms),
                ex=self._maximum_revocation_ttl(),
            )
        return count

    async def change_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        logger.info(f"Changing password for user: {user_id}")
        user = await self.user_repository.get_user_by_id(user_id)
        if not user:
            raise ValidationError("User not found")
        if not user.is_active:
            raise UnauthorizedError("User account is inactive")

        password_valid = bool(user.password_hash) and await asyncio.to_thread(
            self._verify_password, old_password, user.password_hash
        )
        if not password_valid:
            raise UnauthorizedError("Invalid old password")
        if not new_password or len(new_password) < 6:
            raise ValidationError("New password must be at least 6 characters long")

        new_password_hash = await asyncio.to_thread(
            self._hash_password, new_password
        )
        user.password_hash = new_password_hash
        user.updated_at = datetime.utcnow()
        await self.user_repository.update_user(user)
        await self.revoke_user_tokens(user_id)
        logger.info(f"Password changed successfully for user: {user_id}")
        return True

    async def change_fullname(self, user_id: str, new_fullname: str) -> User:
        logger.info(f"Changing fullname for user: {user_id}")
        user = await self.user_repository.get_user_by_id(user_id)
        if not user:
            raise ValidationError("User not found")
        if not user.is_active:
            raise UnauthorizedError("User account is inactive")
        if not new_fullname or len(new_fullname.strip()) < 2:
            raise ValidationError("Full name must be at least 2 characters long")
        user.fullname = new_fullname.strip()
        user.updated_at = datetime.utcnow()
        updated_user = await self.user_repository.update_user(user)
        logger.info(f"Fullname changed successfully for user: {user_id}")
        return updated_user

    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        return await self.user_repository.get_user_by_id(user_id)

    async def deactivate_user(self, user_id: str) -> bool:
        logger.info(f"Deactivating user: {user_id}")
        user = await self.user_repository.get_user_by_id(user_id)
        if not user:
            raise ValidationError("User not found")
        user.deactivate()
        await self.user_repository.update_user(user)
        await self.revoke_user_tokens(user_id)
        logger.info(f"User deactivated successfully: {user_id}")
        return True

    async def activate_user(self, user_id: str) -> bool:
        logger.info(f"Activating user: {user_id}")
        user = await self.user_repository.get_user_by_id(user_id)
        if not user:
            raise ValidationError("User not found")
        user.activate()
        await self.user_repository.update_user(user)
        logger.info(f"User activated successfully: {user_id}")
        return True

    async def reset_password(self, email: str, new_password: str) -> bool:
        """Reset user password with email"""
        logger.info("Resetting user password")

        if self.settings.auth_provider != "password":
            raise BadRequestError("Password reset is not allowed")
        user = await self.user_repository.get_user_by_email(email)
        if not user:
            raise ValidationError("User not found")
        if not user.is_active:
            raise UnauthorizedError("User account is inactive")
        if not new_password or len(new_password) < 6:
            raise ValidationError("New password must be at least 6 characters long")

        new_password_hash = await asyncio.to_thread(
            self._hash_password, new_password
        )
        user.password_hash = new_password_hash
        user.updated_at = datetime.utcnow()
        await self.user_repository.update_user(user)
        await self.revoke_user_tokens(user.id)

        logger.info("Password reset successfully for user_id=%s", user.id)
        return True
