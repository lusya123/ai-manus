import hashlib
import secrets
from typing import Any, Optional
from datetime import datetime, UTC
import httpx
from app.domain.models.user import User, UserRole
from app.domain.repositories.user_repository import UserRepository
from app.application.errors.exceptions import UnauthorizedError, ValidationError, BadRequestError
from app.core.config import get_settings
from app.application.services.token_service import TokenService
from app.domain.models.auth import AuthToken
import logging

logger = logging.getLogger(__name__)


class AuthService:
    """Authentication service handling user authentication and authorization"""
    
    def __init__(self, user_repository: UserRepository, token_service: TokenService):
        self.user_repository = user_repository
        self.settings = get_settings()
        self.token_service = token_service
    
    def _hash_password(self, password: str) -> str:
        """Hash password using configured algorithm"""
        salt = self.settings.password_salt or ''
        
        return self._pbkdf2_sha256(password, salt)
    
    def _pbkdf2_sha256(self, password: str, salt: str) -> str:
        """PBKDF2 with SHA-256 implementation"""
        password_bytes = password.encode('utf-8')
        salt_bytes = salt.encode('utf-8')
        
        # Use configured rounds
        rounds = self.settings.password_hash_rounds or 10
        
        # Generate hash
        hash_bytes = hashlib.pbkdf2_hmac('sha256', password_bytes, salt_bytes, rounds)
        
        # Return salt + hash as hex string
        return salt + hash_bytes.hex()
    
    def _verify_password(self, password: str, password_hash: str) -> bool:
        """Verify password against hash"""
        if not password_hash:
            return False
        
        try:
            # Generate hash with extracted salt
            generated_hash = self._hash_password(password)

            logger.info(f"Generated hash: {generated_hash} vs expected hash: {password_hash}")
            return generated_hash == password_hash
        except Exception as e:
            logger.error(f"Password verification error: {e}")
            return False
    
    def _generate_user_id(self) -> str:
        """Generate unique user ID"""
        return secrets.token_urlsafe(16)

    def _sub2api_auth_url(self, path: str) -> str:
        base_url = (self.settings.sub2api_base_url or "").rstrip("/")
        if not base_url:
            raise UnauthorizedError("Sub2API auth is not configured")
        return f"{base_url}/{path.lstrip('/')}"

    def _coerce_datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                logger.debug("Failed to parse Sub2API datetime: %s", value)
        return datetime.now(UTC)

    def _map_sub2api_user(self, data: dict[str, Any]) -> User:
        external_id = str(data.get("id") or data.get("user_id") or data.get("sub") or "")
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
        if len(fullname.strip()) < 2:
            fullname = f"Sub2API User {external_id}"

        sub2api_role = str(data.get("role") or "").lower()
        role = UserRole.ADMIN if sub2api_role == "admin" else UserRole.USER
        status = str(data.get("status") or "active").lower()
        is_active = status not in {"inactive", "disabled", "banned", "deleted", "blocked"}

        return User(
            id=f"sub2api:{external_id}",
            fullname=fullname,
            email=email,
            role=role,
            is_active=is_active,
            created_at=self._coerce_datetime(data.get("created_at")),
            updated_at=self._coerce_datetime(data.get("updated_at")),
            last_login_at=self._coerce_datetime(data["last_login_at"]) if data.get("last_login_at") else None,
            auth_provider="sub2api",
            external_id=external_id,
            external_user=data,
        )

    async def _verify_sub2api_token(self, token: str) -> Optional[User]:
        if not token:
            return None

        url = self._sub2api_auth_url(self.settings.sub2api_auth_me_path)
        try:
            async with httpx.AsyncClient(timeout=self.settings.sub2api_timeout_seconds) as client:
                response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            logger.warning("Sub2API auth request failed: %s", e)
            return None

        if response.status_code != 200:
            logger.warning("Sub2API auth rejected token: status=%s", response.status_code)
            return None

        try:
            payload = response.json()
        except ValueError:
            logger.warning("Sub2API auth returned non-JSON response")
            return None

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            logger.warning("Sub2API auth returned invalid response shape")
            return None

        code = payload.get("code")
        if code not in (None, 0):
            logger.warning("Sub2API auth returned error code: %s", code)
            return None

        return self._map_sub2api_user(data)

    async def _refresh_sub2api_access_token(self, refresh_token: str) -> AuthToken:
        url = self._sub2api_auth_url(self.settings.sub2api_auth_refresh_path)
        try:
            async with httpx.AsyncClient(timeout=self.settings.sub2api_timeout_seconds) as client:
                response = await client.post(url, json={"refresh_token": refresh_token})
        except httpx.HTTPError as e:
            logger.warning("Sub2API token refresh request failed: %s", e)
            raise UnauthorizedError("Token refresh failed")

        if response.status_code != 200:
            raise UnauthorizedError("Invalid refresh token")

        try:
            payload = response.json()
        except ValueError:
            raise UnauthorizedError("Invalid refresh token")

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or payload.get("code") not in (None, 0):
            raise UnauthorizedError("Invalid refresh token")

        access_token = data.get("access_token")
        if not access_token:
            raise UnauthorizedError("Invalid refresh token")

        return AuthToken(
            access_token=access_token,
            refresh_token=data.get("refresh_token"),
            token_type=str(data.get("token_type") or "bearer").lower(),
        )
    
    async def register_user(self, fullname: str, password: str, email: str, role: UserRole = UserRole.USER) -> User:
        """Register a new user"""
        logger.info(f"Registering user: {email}")

        if self.settings.auth_provider != "password":
            raise BadRequestError("Registration is not allowed")
        
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
        password_hash = self._hash_password(password)
        
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
        logger.debug(f"Authenticating user: {email}")
        
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
                logger.warning(f"Local authentication failed for user: {email}")
                return None
        
        elif self.settings.auth_provider == "password":
            # Database password authentication
            user = await self.user_repository.get_user_by_email(email)
            if not user:
                logger.warning(f"User not found: {email}")
                return None
            
            if not user.is_active:
                logger.warning(f"User account is inactive: {email}")
                return None
            
            if not user.password_hash:
                logger.warning(f"User has no password hash: {email}")
                return None
            
            # Verify password
            if not self._verify_password(password, user.password_hash):
                logger.warning(f"Invalid password for user: {email}")
                return None
            
            # Update last login
            user.update_last_login()
            await self.user_repository.update_user(user)
            
            logger.info(f"User authenticated successfully: {email}")
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
        
        # Generate JWT tokens
        access_token = self.token_service.create_access_token(user)
        refresh_token = self.token_service.create_refresh_token(user)
        
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

        payload = self.token_service.verify_token(refresh_token)
        
        if not payload:
            raise UnauthorizedError("Invalid refresh token")
        
        if payload.get("type") != "refresh":
            raise UnauthorizedError("Invalid token type")
        
        # Get user from database
        user_id = payload.get("sub")
        user = await self.user_repository.get_user_by_id(user_id)
        
        if not user or not user.is_active:
            raise UnauthorizedError("User not found or inactive")
        
        # Generate new access token
        new_access_token = self.token_service.create_access_token(user)
        
        return AuthToken(
            access_token=new_access_token,
            token_type="bearer"
        )
    
    async def verify_token(self, token: str) -> Optional[User]:
        """Verify JWT token and return user"""
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
    
    async def logout(self, token: str) -> bool:
        """Logout user by revoking token"""
        if self.settings.auth_provider == "none":
            raise BadRequestError("Logout is not allowed")
        return self.token_service.revoke_token(token)
    
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
        if not user.password_hash or not self._verify_password(old_password, user.password_hash):
            raise UnauthorizedError("Invalid old password")
        
        # Validate new password
        if not new_password or len(new_password) < 6:
            raise ValidationError("New password must be at least 6 characters long")
        
        # Hash new password
        new_password_hash = self._hash_password(new_password)
        
        # Update user password
        user.password_hash = new_password_hash
        user.updated_at = datetime.utcnow()
        
        await self.user_repository.update_user(user)
        
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
        logger.info(f"Resetting password for user: {email}")
        
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
        new_password_hash = self._hash_password(new_password)
        
        # Update user password
        user.password_hash = new_password_hash
        user.updated_at = datetime.utcnow()
        
        await self.user_repository.update_user(user)
        
        logger.info(f"Password reset successfully for user: {email}")
        return True
