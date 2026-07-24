import hashlib
import asyncio
import base64
import binascii
import hmac
import secrets
from typing import Any, Optional
from datetime import datetime, UTC
import httpx
from app.domain.models.user import User, UserRole
from app.domain.repositories.user_repository import UserRepository
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
    
    def __init__(self, user_repository: UserRepository, token_service: TokenService):
        self.user_repository = user_repository
        self.settings = get_settings()
        self.token_service = token_service
    
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
        """Verify password against hash"""
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
        """Generate unique user ID"""
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

    async def revoke_user_tokens(self, user_id: str) -> None:
        """Invalidate every token issued before a credential/state change."""

        from app.infrastructure.storage.redis import get_redis

        cutoff_ms = int(datetime.now(UTC).timestamp() * 1000)
        await get_redis().client.set(
            self._jwt_user_cutoff_key(user_id),
            str(cutoff_ms),
            ex=self._maximum_revocation_ttl(),
        )

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
    
    async def register_user(self, fullname: str, password: str, email: str, role: UserRole = UserRole.USER) -> User:
        """Register a new user"""
        logger.info("Registering user")

        if self.settings.auth_provider != "password":
            raise BadRequestError("Registration is not allowed")
        if not self.settings.registration_enabled:
            raise BadRequestError("Public registration is disabled")
        
        # Validate input
        if not fullname or len(fullname.strip()) < 2:
            raise ValidationError("Full name must be at least 2 characters long")
        
        if not email or '@' not in email:
            raise ValidationError("Valid email is required")
        
        if not password or len(password) < 6:
            raise ValidationError("Password must be at least 6 characters long")
        
        # Check if email already exists
        if await self.user_repository.email_exists(email):
            raise ValidationError("Email already exists")
        
        # Hash password
        password_hash = await asyncio.to_thread(self._hash_password, password)
        
        # Create user
        user = User(
            id=self._generate_user_id(),
            fullname=fullname.strip(),
            email=email.lower(),
            password_hash=password_hash,
            role=role,
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        
        # Save to database
        created_user = await self.user_repository.create_user(user)
        
        logger.info(f"User registered successfully: {created_user.id}")
        return created_user
    
    async def authenticate_user(self, email: str, password: str) -> Optional[User]:
        """Authenticate user by email and password"""
        logger.debug("Authenticating user")
        
        # Handle different auth providers
        if self.settings.auth_provider == "none":
            # No authentication required - return a default user
            return User(
                id="anonymous",
                fullname="anonymous",
                email="anonymous@localhost",
                role=UserRole.USER,
                is_active=True
            )
        
        elif self.settings.auth_provider == "local":
            # Local authentication using configured credentials
            if (email == self.settings.local_auth_email and 
                password == self.settings.local_auth_password):
                return User(
                    id="local_admin",
                    fullname="Local Admin",
                    email=email,
                    role=UserRole.ADMIN,
                    is_active=True
                )
            else:
                logger.warning("Local authentication failed")
                return None
        
        elif self.settings.auth_provider == "password":
            # Database password authentication
            user = await self.user_repository.get_user_by_email(email)
            if not user:
                logger.warning("Password authentication failed: user not found")
                return None
            
            if not user.is_active:
                logger.warning(
                    "Password authentication failed: inactive user_id=%s",
                    user.id,
                )
                return None
            
            if not user.password_hash:
                logger.warning(
                    "Password authentication failed: missing hash for user_id=%s",
                    user.id,
                )
                return None
            
            # Verify password
            password_valid = await asyncio.to_thread(
                self._verify_password, password, user.password_hash
            )
            if not password_valid:
                logger.warning(
                    "Password authentication failed: invalid credential for "
                    "user_id=%s",
                    user.id,
                )
                return None

            if self._password_needs_rehash(user.password_hash):
                user.password_hash = await asyncio.to_thread(
                    self._hash_password, password
                )
            
            # Update last login
            user.update_last_login()
            await self.user_repository.update_user(user)
            
            logger.info("User authenticated successfully: user_id=%s", user.id)
            return user

        elif self.settings.auth_provider == "sub2api":
            raise BadRequestError("Use Sub2API authentication token")
        
        else:
            raise ValueError(f"Unsupported auth provider: {self.settings.auth_provider}")
    
    async def login_with_tokens(self, email: str, password: str) -> AuthToken:
        """Authenticate user and return JWT tokens"""
        user = await self.authenticate_user(email, password)
        
        if not user:
            raise UnauthorizedError("Invalid email or password")
        
        # Generate a pair in one revocable token family.
        access_token, refresh_token = self.token_service.create_token_pair(user)
        
        return AuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            user=user
        )
    
    async def refresh_access_token(self, refresh_token: str) -> AuthToken:
        """Refresh access token using refresh token"""
        if self.settings.auth_provider == "sub2api":
            return await self._refresh_sub2api_access_token(refresh_token)
        payload = self.token_service.verify_token(
            refresh_token, expected_type="refresh"
        )
        
        if not payload or await self._is_token_revoked(refresh_token):
            raise UnauthorizedError("Invalid refresh token")

        # SET NX closes the two-replica refresh race. Only the first caller may
        # mint a replacement pair from this token.
        await self._consume_jwt_refresh(refresh_token, payload)
        
        # Get user from database
        user_id = payload.get("sub")
        if self.settings.auth_provider == "password":
            user = await self.user_repository.get_user_by_id(user_id)
        else:
            try:
                user = User(
                    id=str(user_id),
                    fullname=str(payload.get("fullname") or "Local User"),
                    email=str(payload.get("email") or "local@localhost"),
                    role=UserRole(payload.get("role", "user")),
                    is_active=bool(payload.get("is_active", True)),
                    auth_provider=self.settings.auth_provider,
                )
            except (TypeError, ValueError):
                user = None
        
        if not user or not user.is_active:
            raise UnauthorizedError("User not found or inactive")
        
        session_id = str(payload.get("sid") or self.token_service.new_session_id())
        new_access_token = self.token_service.create_access_token(
            user, session_id=session_id
        )
        new_refresh_token = self.token_service.create_refresh_token(
            user, session_id=session_id
        )
        
        return AuthToken(
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            token_type="bearer",
        )
    
    async def verify_token(self, token: str) -> Optional[User]:
        """Verify JWT token and return user"""
        if not token or await self._is_token_revoked(token):
            return None
        if self.settings.auth_provider == "sub2api":
            return await self._verify_sub2api_token(token)
        user_info = self.token_service.get_user_from_token(token)
        
        if not user_info:
            return None
        
        # For database users, verify user still exists and is active
        if self.settings.auth_provider == "password":
            user = await self.user_repository.get_user_by_id(user_info["id"])
            if not user or not user.is_active:
                return None
            return user
        
        # For local/none authentication, create user from token info
        return User(
            id=user_info["id"],
            fullname=user_info["fullname"],
            email=user_info.get("email"),
            role=UserRole(user_info.get("role", "user")),
            is_active=user_info.get("is_active", True)
        )
    
    async def logout(
        self, token: Optional[str], *, refresh_token: Optional[str] = None
    ) -> bool:
        """Logout by revoking the whole local family or both external tokens."""
        if self.settings.auth_provider == "none":
            raise BadRequestError("Logout is not allowed")
        from app.infrastructure.storage.redis import get_redis

        redis = get_redis().client
        if self.settings.auth_provider == "sub2api":
            if not refresh_token:
                raise BadRequestError(
                    "refresh_token is required to fully revoke Sub2API login"
                )
            await self._revoke_sub2api_family(token, refresh_token)
            return True

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
            await redis.set(
                self._jwt_family_revocation_key(str(payload["sid"])),
                "1",
                ex=ttl_seconds,
            )
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
    
    async def change_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        """Change user password"""
        logger.info(f"Changing password for user: {user_id}")
        
        # Get user
        user = await self.user_repository.get_user_by_id(user_id)
        if not user:
            raise ValidationError("User not found")
        
        if not user.is_active:
            raise UnauthorizedError("User account is inactive")
        
        # Verify old password
        password_valid = bool(user.password_hash) and await asyncio.to_thread(
            self._verify_password, old_password, user.password_hash
        )
        if not password_valid:
            raise UnauthorizedError("Invalid old password")
        
        # Validate new password
        if not new_password or len(new_password) < 6:
            raise ValidationError("New password must be at least 6 characters long")
        
        # Hash new password
        new_password_hash = await asyncio.to_thread(
            self._hash_password, new_password
        )
        
        # Update user password
        user.password_hash = new_password_hash
        user.updated_at = datetime.utcnow()
        
        await self.user_repository.update_user(user)
        await self.revoke_user_tokens(user_id)
        
        logger.info(f"Password changed successfully for user: {user_id}")
        return True
    
    async def change_fullname(self, user_id: str, new_fullname: str) -> User:
        """Change user fullname"""
        logger.info(f"Changing fullname for user: {user_id}")
        
        # Get user
        user = await self.user_repository.get_user_by_id(user_id)
        if not user:
            raise ValidationError("User not found")
        
        if not user.is_active:
            raise UnauthorizedError("User account is inactive")
        
        # Validate new fullname
        if not new_fullname or len(new_fullname.strip()) < 2:
            raise ValidationError("Full name must be at least 2 characters long")
        
        # Update user fullname
        user.fullname = new_fullname.strip()
        user.updated_at = datetime.utcnow()
        
        updated_user = await self.user_repository.update_user(user)
        
        logger.info(f"Fullname changed successfully for user: {user_id}")
        return updated_user
    
    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        """Get user by ID"""
        return await self.user_repository.get_user_by_id(user_id)
    
    async def deactivate_user(self, user_id: str) -> bool:
        """Deactivate user account"""
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
        """Activate user account"""
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
        
        # Get user by email
        user = await self.user_repository.get_user_by_email(email)
        if not user:
            raise ValidationError("User not found")
        
        if not user.is_active:
            raise UnauthorizedError("User account is inactive")
        
        # Validate new password
        if not new_password or len(new_password) < 6:
            raise ValidationError("New password must be at least 6 characters long")
        
        # Hash new password
        new_password_hash = await asyncio.to_thread(
            self._hash_password, new_password
        )
        
        # Update user password
        user.password_hash = new_password_hash
        user.updated_at = datetime.utcnow()
        
        await self.user_repository.update_user(user)
        await self.revoke_user_tokens(user.id)
        
        logger.info("Password reset successfully for user_id=%s", user.id)
        return True
