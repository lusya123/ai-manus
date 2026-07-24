from typing import Optional, Union
import logging
from functools import lru_cache
from fastapi import Request, Header, HTTPException, status, Depends, Query
from starlette.websockets import WebSocket
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.infrastructure.external.file.gridfsfile import get_file_storage
from app.infrastructure.external.search import get_search_engine
from app.domain.models.user import User, UserRole
from app.application.errors.exceptions import UnauthorizedError
from app.core.config import get_settings

# Import all required services
from app.application.services.agent_service import AgentService
from app.application.services.file_service import FileService
from app.application.services.auth_service import AuthService
from app.application.services.token_service import TokenService
from app.application.services.email_service import EmailService
from app.domain.utils.error_reporting import safe_exception_summary
from app.infrastructure.logging import redact_capability_text
from app.infrastructure.external.cache import get_cache
from app.infrastructure.external.llm import get_llm_factory

# Import all required dependencies for agent service
from app.domain.external.task import Task
from app.domain.services.agent_task_runner import AgentTaskRunnerFactory
from app.infrastructure.external.sandbox import get_sandbox_provider
from app.infrastructure.external.task.redis_task import RedisStreamTask
from app.infrastructure.external.coordination import RedisSessionLifecycleLease
from app.infrastructure.repositories.mongo_agent_repository import MongoAgentRepository
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
from app.infrastructure.repositories.mongo_turn_submission_repository import MongoTurnSubmissionRepository
from app.infrastructure.repositories.file_mcp_repository import FileMCPRepository
from app.infrastructure.repositories.user_repository import MongoUserRepository
from app.infrastructure.repositories.claw_repository import ClawRepository as MongoClawRepository
from app.application.services.claw_service import ClawService
from app.domain.services.claw_domain_service import ClawDomainService


# Configure logging
logger = logging.getLogger(__name__)

# Security scheme - Bearer Token only
security_bearer = HTTPBearer(auto_error=False)

def _get_task_cls() -> type[Task]:
    """Select the task backend implementation from the TASK_BACKEND setting."""
    settings = get_settings()
    backend = (settings.task_backend or "local").lower()
    if backend == "celery":
        from app.infrastructure.external.task.celery_task import CeleryTask
        logger.info("Using Celery task backend")
        return CeleryTask
    if backend != "local":
        logger.warning("Unknown TASK_BACKEND '%s', falling back to 'local'", backend)
    if settings.backend_replica_count > 1:
        raise RuntimeError(
            "TASK_BACKEND=local only supports one backend process because its "
            "task registry is process-local; set TASK_BACKEND=celery before "
            "using BACKEND_REPLICA_COUNT>1"
        )
    return RedisStreamTask


@lru_cache()
def get_agent_service() -> AgentService:
    """
    Get agent service instance with all required dependencies
    
    This function creates and returns an AgentService instance with all
    necessary dependencies. Uses lru_cache for singleton pattern.
    """
    logger.info("Creating AgentService instance")

    # Create all dependencies
    settings = get_settings()
    agent_repository = MongoAgentRepository()
    session_repository = MongoSessionRepository()
    turn_submission_repository = MongoTurnSubmissionRepository()
    sandbox_cls = get_sandbox_provider()
    task_cls = _get_task_cls()
    file_storage = get_file_storage()
    search_engine = get_search_engine()
    mcp_repository = FileMCPRepository()
    llm_factory = get_llm_factory()
    if (settings.sandbox_provider or "docker").strip().lower() == "agentbay":
        from app.infrastructure.external.sandbox.agentbay_provisioner import (
            AgentBayProvisioner,
        )
        from app.infrastructure.repositories.external.sandbox.mongo_agentbay_quota import (
            MongoAgentBayQuotaLedger,
        )

        ledger = MongoAgentBayQuotaLedger(
            deployment_id=str(settings.agentbay_deployment_id),
            max_total=settings.agentbay_max_sessions_total,
            max_per_user=settings.agentbay_max_sessions_per_user,
            config_version=settings.agentbay_quota_config_version,
            command_timeout_seconds=(
                settings.agentbay_quota_command_timeout_seconds
            ),
        )
        sandbox_provisioner = AgentBayProvisioner(
            ledger=ledger,
            session_repository=session_repository,
            deployment_id=str(settings.agentbay_deployment_id),
            sandbox_cls=sandbox_cls,
        )
    else:
        from app.infrastructure.external.sandbox.passthrough_provisioner import (
            PassthroughSandboxProvisioner,
        )

        sandbox_provisioner = PassthroughSandboxProvisioner(
            sandbox_cls, session_repository
        )
    
    # Register the factory used to rebuild task runners on the execution side.
    # For the local backend the runner is rebuilt in this process; for the
    # celery backend workers register their own factory (see app/worker.py).
    task_cls.set_runner_factory(AgentTaskRunnerFactory(
        agent_repository=agent_repository,
        session_repository=session_repository,
        turn_submission_repository=turn_submission_repository,
        sandbox_cls=sandbox_cls,
        file_storage=file_storage,
        mcp_repository=mcp_repository,
        llm_factory=llm_factory,
        search_engine=search_engine,
    ))
    
    # Create AgentService instance
    return AgentService(
        agent_repository=agent_repository,
        session_repository=session_repository,
        turn_submission_repository=turn_submission_repository,
        sandbox_cls=sandbox_cls,
        task_cls=task_cls,
        file_storage=file_storage,
        search_engine=search_engine,
        mcp_repository=mcp_repository,
        session_lifecycle_lease=RedisSessionLifecycleLease(),
        sandbox_provisioner=sandbox_provisioner,
    )


@lru_cache()
def get_file_service() -> FileService:
    """
    Get file service instance with required dependencies
    
    This function creates and returns a FileService instance with
    the necessary file storage and token service dependencies.
    """
    logger.info("Creating FileService instance")
    
    # Get dependencies
    file_storage = get_file_storage()
    token_service = get_token_service()
    
    return FileService(
        file_storage=file_storage,
        token_service=token_service,
    )


@lru_cache()
def get_auth_service() -> AuthService:
    """
    Get authentication service instance with required dependencies
    
    This function creates and returns an AuthService instance with
    the necessary user repository dependency.
    """
    logger.info("Creating AuthService instance")
    
    # Get user repository dependency
    user_repository = MongoUserRepository()
    
    return AuthService(
        user_repository=user_repository,
        token_service=get_token_service(),
    )


@lru_cache()
def get_token_service() -> TokenService:
    """Get token service instance"""
    logger.info("Creating TokenService instance")
    return TokenService()


@lru_cache()
def get_claw_service() -> ClawService:
    """Get claw service instance"""
    logger.info("Creating ClawService instance")
    settings = get_settings()
    claw_repository = MongoClawRepository()

    if settings.claw_address:
        from app.infrastructure.external.claw.fixed_claw_runtime import FixedClawRuntime
        claw_runtime = FixedClawRuntime(address=settings.claw_address)
    else:
        from app.infrastructure.external.claw.docker_claw_runtime import DockerClawRuntime
        claw_runtime = DockerClawRuntime()

    from app.infrastructure.external.claw.http_claw_client import HttpClawClient
    claw_client = HttpClawClient()

    claw_domain_service = ClawDomainService(
        claw_repository=claw_repository,
        claw_runtime=claw_runtime,
        claw_client=claw_client,
    )

    return ClawService(claw_domain_service=claw_domain_service)


@lru_cache()
def get_email_service() -> EmailService:
    """Get email service instance"""
    logger.info("Creating EmailService instance")
    cache = get_cache()
    return EmailService(cache=cache)


async def get_current_user(
    bearer_credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
    auth_service: AuthService = Depends(get_auth_service)
) -> User:
    """
    Get current authenticated user (required)
    
    This dependency enforces authentication using Bearer Token.
    If authentication fails, it raises an UnauthorizedError.
    """
    settings = get_settings()
    
    # If auth_provider is 'none', return anonymous user
    if settings.auth_provider == "none":
        return User(
            id="anonymous",
            fullname="anonymous",
            email="anonymous@localhost",
            role=UserRole.USER,
            is_active=True,
            auth_provider="none",
        )
    
    # Check if bearer token is provided
    if not bearer_credentials:
        raise UnauthorizedError("Authentication required")
    
    try:
        # Verify bearer token
        user = await auth_service.verify_token(bearer_credentials.credentials)
        
        if not user:
            raise UnauthorizedError("Invalid token")
            
        if not user.is_active:
            raise UnauthorizedError("User account is inactive")
            
        return user
        
    except Exception as e:
        logger.warning(
            "Authentication failed: %s", safe_exception_summary(e)
        )
        raise UnauthorizedError("Authentication failed")


async def get_optional_current_user(
    bearer_credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
    auth_service: AuthService = Depends(get_auth_service)
) -> Optional[User]:
    """
    Get current authenticated user (optional)
    
    This dependency allows both authenticated and anonymous access.
    Returns None if authentication fails or is not provided.
    
    Uses Bearer Token authentication.
    """
    settings = get_settings()
    
    # If auth_provider is 'none', return anonymous user
    if settings.auth_provider == "none":
        return User(
            id="anonymous",
            fullname="anonymous",
            email="anonymous@localhost",
            role=UserRole.USER,
            is_active=True,
            auth_provider="none",
        )
    
    # If no bearer token provided, return None
    if not bearer_credentials:
        return None
    
    try:
        # Try to verify bearer token
        user = await auth_service.verify_token(bearer_credentials.credentials)
        
        if user and user.is_active:
            return user
            
    except Exception as e:
        logger.warning(
            "Optional authentication failed: %s",
            safe_exception_summary(e),
        )
        
    return None

async def verify_signature(
    request: Request,
    signature: Optional[str] = Query(None),
    token_service: TokenService = Depends(get_token_service)
) -> str:
    return await _verify_signature(request, signature, token_service)

async def verify_signature_websocket(
    request: WebSocket,
    signature: Optional[str] = Query(None),
    token_service: TokenService = Depends(get_token_service)
) -> str:
    return await _verify_signature(request, signature, token_service)

async def _verify_signature(
    request: Union[Request, WebSocket],
    signature: Optional[str] = Query(None),
    token_service: TokenService = Depends(get_token_service)
) -> str:
    """
    Verify signature for signed URL access
    
    This dependency validates the signature parameter in the request URL.
    If the signature is missing or invalid, it raises an HTTPException.
    
    This is designed to work with both regular HTTP endpoints and WebSocket endpoints.
    For WebSocket connections, the exception will be raised before the connection is accepted,
    preventing invalid connections from being established.
    
    Args:
        request: The incoming request
        signature: The signature query parameter
        token_service: Token service for signature verification
        
    Returns:
        The verified signature string
        
    Raises:
        HTTPException: If signature is missing or invalid (status code 401)
    """
    if not signature:
        logger.error(
            "Missing signature for path: %s",
            redact_capability_text(request.url.path),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing signature"
        )
    
    if not token_service.verify_signed_url(str(request.url)):
        logger.error(
            "Invalid signature for path: %s",
            redact_capability_text(request.url.path),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature"
        )
    
    return signature
