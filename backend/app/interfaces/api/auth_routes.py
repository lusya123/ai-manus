from typing import Optional
from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import logging

from app.application.services.auth_service import AuthService
from app.application.services.email_service import EmailService
from app.application.errors.exceptions import (
    UnauthorizedError, ForbiddenError, NotFoundError, BadRequestError
)
from app.interfaces.dependencies import get_auth_service, get_current_user, get_email_service
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.auth import (
    LoginRequest, RegisterRequest, ChangePasswordRequest, ChangeFullnameRequest, RefreshTokenRequest, LogoutRequest,
    SendVerificationCodeRequest, ResetPasswordRequest,
    LoginResponse, RegisterResponse, AuthStatusResponse, RefreshTokenResponse,
    UserResponse
)
from app.core.config import get_settings
from app.domain.models.user import User
from app.domain.models.auth_session import AuthClientType, CredentialSource

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session_cookie(response: Response, session_id: str, client: AuthClientType) -> None:
    settings = get_settings()
    if client in (AuthClientType.IOS, AuthClientType.ANDROID):
        max_age = settings.session_app_ttl_days * 24 * 3600
    else:
        max_age = settings.session_web_ttl_days * 24 * 3600
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_id,
        max_age=max_age,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
    )


def _client_ip(request: Request) -> Optional[str]:
    # Forwarded headers are untrusted unless a deployment installs a trusted
    # proxy middleware that rewrites Request.client.
    return request.client.host if request.client else "unknown"


async def _issue_legacy_jwt_browser_cookie(
    *,
    response: Response,
    http_request: Request,
    current_user: User,
    bearer_credentials: Optional[HTTPAuthorizationCredentials],
    auth_service: AuthService,
) -> None:
    """Exchange a verified legacy access JWT for one revocable browser session."""

    settings = get_settings()
    if settings.auth_provider not in {"password", "local"}:
        return
    if bearer_credentials is None:
        return

    bearer_token = bearer_credentials.credentials
    if await auth_service.resolve_session_token(bearer_token) is not None:
        # The Bearer value is already an opaque Redis session. Re-minting it as
        # though it were a legacy JWT would create duplicate login authority.
        return
    # Capture the revoke-all fence before re-validating the legacy credential.
    # Session creation uses a generation CAS, so password changes/logout-all
    # racing this exchange cannot publish a newly revoked browser session.
    generation = await auth_service.session_store.get_user_generation(
        current_user.id
    )
    resolved = await auth_service.resolve_jwt_grace(bearer_token)
    if (
        resolved is None
        or resolved.source != CredentialSource.JWT_GRACE
        or resolved.user_id != current_user.id
        or not resolved.jwt_payload
    ):
        # Ordinary opaque Bearer credentials intentionally take this path: they
        # are not JWT grace credentials and must not mint another session.
        return

    cookie_id = http_request.cookies.get(settings.session_cookie_name)
    if cookie_id:
        cookie_credentials = await auth_service.resolve_session_token(cookie_id)
        if (
            cookie_credentials is not None
            and cookie_credentials.user_id == current_user.id
        ):
            return

    payload = resolved.jwt_payload
    family_id = str(payload.get("sid") or "").strip()
    issued_ms = payload.get("iat_ms")
    if not isinstance(issued_ms, (int, float)):
        try:
            issued_ms = int(float(payload.get("iat") or 0) * 1000)
        except (TypeError, ValueError, OverflowError):
            issued_ms = 0
    migrated_from = (
        f"jwt-family:{family_id}"
        if family_id
        else f"jwt-user:{current_user.id}:{int(issued_ms)}"
    )
    candidate = await auth_service.create_auth_session(
        current_user,
        client=AuthClientType.WEB,
        ip=_client_ip(http_request),
        user_agent=http_request.headers.get("user-agent"),
        rotated_from=migrated_from,
        expected_generation=generation,
    )
    selected_session_id = candidate.session_id

    try:
        if family_id:
            stale_winner: Optional[str] = None
            for _attempt in range(3):
                winner = await auth_service.claim_jwt_session_migration(
                    family_id,
                    candidate.session_id,
                    auth_service._payload_ttl(payload),
                    replace_session_id=stale_winner,
                )
                if winner is None:
                    raise UnauthorizedError("Legacy login was revoked")
                if winner == candidate.session_id:
                    selected_session_id = winner
                    break

                winner_session = await auth_service.session_store.get(winner)
                if winner_session is None:
                    # Replace only the exact mapping proven stale. The Lua claim
                    # rechecks both the mapping value and family revocation.
                    stale_winner = winner
                    continue
                if (
                    winner_session.user_id != current_user.id
                    or winner_session.rotated_from != migrated_from
                ):
                    raise UnauthorizedError("Invalid migrated login session")
                winner_credentials = await auth_service.resolve_session_token(winner)
                if (
                    winner_credentials is not None
                    and winner_credentials.user_id == current_user.id
                ):
                    selected_session_id = winner
                    break
                stale_winner = winner
            else:
                raise UnauthorizedError("Legacy login migration is unavailable")

            if selected_session_id != candidate.session_id:
                await auth_service.session_store.delete(candidate.session_id)
        elif await auth_service._is_token_revoked(bearer_token):
            # Legacy JWTs without a family id are fenced by the per-user cutoff.
            raise UnauthorizedError("Legacy login was revoked")
    except BaseException:
        if selected_session_id == candidate.session_id:
            await auth_service.session_store.delete(candidate.session_id)
        raise

    _set_session_cookie(response, selected_session_id, AuthClientType.WEB)

@router.post("/login", response_model=APIResponse[LoginResponse])
async def login(
    request: LoginRequest,
    response: Response,
    http_request: Request,
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[LoginResponse]:
    """User login — creates Redis session + Set-Cookie."""
    settings = get_settings()
    await auth_service.enforce_rate_limit(
        "login-ip", _client_ip(http_request),
        limit=settings.auth_login_ip_attempts_per_window,
        window_seconds=settings.auth_login_window_seconds,
    )
    await auth_service.enforce_rate_limit(
        "login-account", request.email,
        limit=settings.auth_login_attempts_per_window,
        window_seconds=settings.auth_login_window_seconds,
    )
    client = auth_service.parse_client(request.client)
    auth_result = await auth_service.login_with_session(
        request.email,
        request.password,
        client=client,
        ip=_client_ip(http_request),
        user_agent=http_request.headers.get("user-agent"),
    )
    _set_session_cookie(response, auth_result.access_token, client)
    return APIResponse.success(LoginResponse(
        user=UserResponse.from_domain(auth_result.user),
        access_token=auth_result.access_token,
        refresh_token=auth_result.refresh_token or auth_result.access_token,
        token_type=auth_result.token_type
    ))


@router.post("/register", response_model=APIResponse[RegisterResponse])
async def register(
    request: RegisterRequest,
    response: Response,
    http_request: Request,
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[RegisterResponse]:
    """User registration — creates Redis session + Set-Cookie."""
    settings = get_settings()
    await auth_service.enforce_rate_limit(
        "register-ip", _client_ip(http_request),
        limit=settings.auth_register_attempts_per_hour,
        window_seconds=3600,
    )
    user = await auth_service.register_user(
        fullname=request.fullname,
        password=request.password,
        email=request.email
    )
    client = auth_service.parse_client(request.client)
    session = await auth_service.create_auth_session(
        user,
        client=client,
        ip=_client_ip(http_request),
        user_agent=http_request.headers.get("user-agent"),
    )
    _set_session_cookie(response, session.session_id, client)
    return APIResponse.success(RegisterResponse(
        user=UserResponse.from_domain(user),
        access_token=session.session_id,
        refresh_token=session.session_id,
        token_type="bearer"
    ))


@router.get("/status", response_model=APIResponse[AuthStatusResponse])
async def get_auth_status(
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[AuthStatusResponse]:
    settings = get_settings()
    return APIResponse.success(AuthStatusResponse(
        auth_provider=settings.auth_provider
    ))


@router.post("/change-password", response_model=APIResponse[dict])
async def change_password(
    request: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[dict]:
    await auth_service.change_password(current_user.id, request.old_password, request.new_password)
    return APIResponse.success({})


@router.post("/change-fullname", response_model=APIResponse[UserResponse])
async def change_fullname(
    request: ChangeFullnameRequest,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[UserResponse]:
    updated_user = await auth_service.change_fullname(current_user.id, request.fullname)
    return APIResponse.success(UserResponse.from_domain(updated_user))


@router.get("/me", response_model=APIResponse[UserResponse])
async def get_current_user_info(
    response: Response,
    http_request: Request,
    current_user: User = Depends(get_current_user),
    bearer_credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
    auth_service: AuthService = Depends(get_auth_service),
) -> APIResponse[UserResponse]:
    settings = get_settings()
    if settings.auth_provider == "sub2api" and bearer_credentials:
        cookie_id = http_request.cookies.get(settings.session_cookie_name)
        cookie_session = (
            await auth_service.session_store.get(cookie_id)
            if cookie_id
            else None
        )
        if not cookie_session or cookie_session.user_id != current_user.id:
            session = await auth_service.create_auth_session(
                current_user,
                client=AuthClientType.WEB,
                ip=_client_ip(http_request),
                user_agent=http_request.headers.get("user-agent"),
            )
            if cookie_session:
                await auth_service.session_store.delete(cookie_session.session_id)
            _set_session_cookie(response, session.session_id, session.client)
    await _issue_legacy_jwt_browser_cookie(
        response=response,
        http_request=http_request,
        current_user=current_user,
        bearer_credentials=bearer_credentials,
        auth_service=auth_service,
    )
    return APIResponse.success(UserResponse.from_domain(current_user))


@router.get("/user/{user_id}", response_model=APIResponse[UserResponse])
async def get_user(
    user_id: str,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[UserResponse]:
    if current_user.role != "admin":
        raise ForbiddenError("Admin access required")
    user = await auth_service.get_user_by_id(user_id)
    if not user:
        raise NotFoundError("User not found")
    return APIResponse.success(UserResponse.from_domain(user))


@router.post("/user/{user_id}/deactivate", response_model=APIResponse[dict])
async def deactivate_user(
    user_id: str,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[dict]:
    if current_user.role != "admin":
        raise ForbiddenError("Admin access required")
    if current_user.id == user_id:
        raise BadRequestError("Cannot deactivate your own account")
    await auth_service.deactivate_user(user_id)
    return APIResponse.success({})


@router.post("/user/{user_id}/activate", response_model=APIResponse[dict])
async def activate_user(
    user_id: str,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[dict]:
    if current_user.role != "admin":
        raise ForbiddenError("Admin access required")
    await auth_service.activate_user(user_id)
    return APIResponse.success({})


@router.post("/refresh", response_model=APIResponse[RefreshTokenResponse])
async def refresh_token(
    response: Response,
    http_request: Request,
    request: RefreshTokenRequest,
    bearer_credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[RefreshTokenResponse]:
    """Slide auth session (body refresh_token, Bearer, or Cookie)."""
    settings = get_settings()
    body_token = request.refresh_token
    bearer = bearer_credentials.credentials if bearer_credentials else None
    cookie_id = http_request.cookies.get(settings.session_cookie_name)
    selected_token = body_token or bearer or cookie_id

    if cookie_id and not bearer and not body_token:
        if http_request.headers.get("X-Requested-With") != "XMLHttpRequest":
            raise UnauthorizedError("CSRF check failed")

    await auth_service.enforce_rate_limit(
        "refresh-token", selected_token or "missing",
        limit=settings.auth_refresh_attempts_per_minute,
        window_seconds=60,
    )
    await auth_service.enforce_rate_limit(
        "refresh-ip", _client_ip(http_request),
        limit=settings.auth_refresh_attempts_per_minute * 2,
        window_seconds=60,
    )

    token_result = await auth_service.refresh_access_token(
        refresh_token=body_token or bearer,
        cookie_session_id=cookie_id,
        rotate=request.rotate,
    )
    if settings.auth_provider == "sub2api":
        _, session = await auth_service.establish_sub2api_session(
            token_result.access_token,
            client=AuthClientType.WEB,
            ip=_client_ip(http_request),
            user_agent=http_request.headers.get("user-agent"),
            rotated_from=cookie_id,
        )
        if cookie_id and cookie_id != session.session_id:
            await auth_service.session_store.delete(cookie_id)
        _set_session_cookie(response, session.session_id, session.client)
    else:
        _set_session_cookie(response, token_result.access_token, AuthClientType.UNKNOWN)
    return APIResponse.success(RefreshTokenResponse(
        access_token=token_result.access_token,
        refresh_token=token_result.refresh_token or token_result.access_token,
        token_type=token_result.token_type
    ))


@router.post("/logout", response_model=APIResponse[dict])
async def logout(
    response: Response,
    http_request: Request,
    request: LogoutRequest | None = None,
    current_user: User = Depends(get_current_user),
    bearer_credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[dict]:
    if get_settings().auth_provider == "none":
        raise BadRequestError("Logout is not allowed")

    settings = get_settings()
    token = (
        bearer_credentials.credentials
        if bearer_credentials
        else http_request.cookies.get(settings.session_cookie_name)
    )
    await auth_service.logout(
        token,
        refresh_token=request.refresh_token if request else None,
        cookie_session_id=http_request.cookies.get(settings.session_cookie_name),
    )
    _clear_session_cookie(response)
    return APIResponse.success({})


@router.post("/logout-all", response_model=APIResponse[dict])
async def logout_all(
    response: Response,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[dict]:
    if get_settings().auth_provider == "none":
        raise BadRequestError("Logout is not allowed")
    count = await auth_service.logout_all(current_user.id)
    _clear_session_cookie(response)
    return APIResponse.success({"revoked": count})


@router.post("/send-verification-code", response_model=APIResponse[dict])
async def send_verification_code(
    request: SendVerificationCodeRequest,
    http_request: Request,
    auth_service: AuthService = Depends(get_auth_service),
    email_service: EmailService = Depends(get_email_service)
) -> APIResponse[dict]:
    if get_settings().auth_provider != "password":
        raise BadRequestError("Password reset is not available")
    await auth_service.enforce_rate_limit(
        "password-reset-ip",
        _client_ip(http_request),
        limit=get_settings().auth_password_reset_attempts_per_hour,
        window_seconds=3600,
    )
    await auth_service.enforce_rate_limit(
        "password-reset-account",
        request.email,
        limit=get_settings().auth_password_reset_attempts_per_hour,
        window_seconds=3600,
    )

    user = await auth_service.user_repository.get_user_by_email(request.email)
    if not user:
        raise NotFoundError("User not found")
    if not user.is_active:
        raise BadRequestError("User account is inactive")
    await email_service.send_verification_code(request.email)
    return APIResponse.success({})


@router.post("/reset-password", response_model=APIResponse[dict])
async def reset_password(
    request: ResetPasswordRequest,
    http_request: Request,
    auth_service: AuthService = Depends(get_auth_service),
    email_service: EmailService = Depends(get_email_service)
) -> APIResponse[dict]:
    if get_settings().auth_provider != "password":
        raise BadRequestError("Password reset is not available")
    await auth_service.enforce_rate_limit(
        "password-reset-confirm-ip",
        _client_ip(http_request),
        limit=get_settings().auth_password_reset_attempts_per_hour,
        window_seconds=3600,
    )
    await auth_service.enforce_rate_limit(
        "password-reset-confirm-account",
        request.email,
        limit=get_settings().auth_password_reset_attempts_per_hour,
        window_seconds=3600,
    )

    if not await email_service.verify_code(request.email, request.verification_code):
        raise UnauthorizedError("Invalid or expired verification code")
    await auth_service.reset_password(request.email, request.new_password)
    return APIResponse.success({})
