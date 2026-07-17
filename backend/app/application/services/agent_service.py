from typing import Any, AsyncGenerator, Optional, List
import asyncio
import logging
import re
from datetime import datetime
from app.domain.models.session import Session, SessionSummary
from app.domain.repositories.session_repository import SessionRepository
from app.domain.repositories.turn_submission_repository import TurnSubmissionRepository
from app.domain.models.turn_submission import (
    TurnSubmission,
    TurnSubmissionCapacityError,
    TurnSubmissionConflictError,
    TurnSubmissionUnavailableError,
)

from app.interfaces.schemas.session import ShellViewResponse
from app.interfaces.schemas.file import FileViewResponse
from app.domain.services.agent_domain_service import AgentDomainService
from app.domain.models.event import AgentEvent
from typing import Type
from app.domain.models.agent import Agent
from app.domain.external.sandbox import Sandbox
from app.domain.external.search import SearchEngine
from app.domain.external.file import FileStorage
from app.domain.external.llm import LLM
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.external.task import Task
from app.domain.external.coordination import SessionLifecycleLease
from app.domain.external.sandbox_provisioner import SandboxProvisioner
from app.domain.external.agentbay_quota import AgentBayQuotaExceededError
from app.domain.models.file import FileInfo
from app.core.config import SUPPORTED_BYOK_PROVIDERS, get_settings
from app.application.errors.exceptions import (
    BadRequestError,
    ConflictError,
    ServiceUnavailableError,
    TooManyRequestsError,
)
from app.domain.repositories.mcp_repository import MCPRepository
from app.domain.models.session import SessionStatus
from app.infrastructure.external.llm.security import (
    ModelCredentialEncryptionError,
    ModelEndpointValidationError,
    provider_api_base,
    provider_api_key,
    validate_model_credential_encryption,
    validate_public_model_endpoint,
)
from app.domain.utils.error_reporting import safe_exception_summary

# Set up logger
logger = logging.getLogger(__name__)


async def _aclose_sandbox_handle(sandbox: Any) -> None:
    """Close a request-scoped sandbox client without deleting its resource."""
    close = getattr(sandbox, "aclose", None)
    if callable(close):
        try:
            await close()
        except Exception as exc:
            logger.warning(
                "Failed to close request-scoped sandbox handle: %s",
                type(exc).__name__,
            )

_MODEL_CONFIG_FIELDS = {
    "model_id",
    "api_key",
    "api_base",
    "model_name",
    "model_provider",
}
_CUSTOM_MODEL_FIELDS = {"api_key", "api_base", "model_name", "model_provider"}
_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_PROVIDER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

class AgentService:
    def __init__(
        self,
        agent_repository: AgentRepository,
        session_repository: SessionRepository,
        sandbox_cls: Type[Sandbox],
        task_cls: Type[Task],
        file_storage: FileStorage,
        mcp_repository: MCPRepository,
        llm: Optional[LLM] = None,
        search_engine: Optional[SearchEngine] = None,
        session_lifecycle_lease: Optional[SessionLifecycleLease] = None,
        turn_submission_repository: Optional[TurnSubmissionRepository] = None,
        sandbox_provisioner: Optional[SandboxProvisioner] = None,
    ):
        logger.info("Initializing AgentService")
        self._agent_repository = agent_repository
        self._session_repository = session_repository
        self._file_storage = file_storage
        self._agent_domain_service = AgentDomainService(
            self._agent_repository,
            self._session_repository,
            sandbox_cls,
            task_cls,
            file_storage,
            mcp_repository,
            search_engine,
            session_lifecycle_lease,
            turn_submission_repository,
            sandbox_provisioner,
        )
        self._search_engine = search_engine
        self._sandbox_cls = sandbox_cls
        self._turn_submission_repository = turn_submission_repository

    @staticmethod
    def _verify_sandbox_provider(session: Session) -> None:
        """Reject request-scoped access through a differently configured provider."""
        persisted = str(
            getattr(session, "sandbox_provider", None) or ""
        ).strip().lower()
        configured = str(
            get_settings().sandbox_provider or "docker"
        ).strip().lower()
        if persisted and persisted != configured:
            raise RuntimeError(
                "Session sandbox belongs to a different provider"
            )
    
    async def create_session(self, user_id: str, model_config: Optional[Any] = None) -> Session:
        logger.info(f"Creating new session for user: {user_id}")
        agent = await self._create_agent(model_config)
        session = Session(agent_id=agent.id, user_id=user_id)
        logger.info(f"Created new Session with ID: {session.id} for user: {user_id}")
        try:
            await self._session_repository.save(session)
        except BaseException:
            # The Agent is persisted first so a session can never reference a
            # missing model configuration.  Roll it back if the second write
            # fails, including cancellation during session creation.
            try:
                await self._agent_repository.delete(agent.id)
            except Exception as exc:
                logger.error(
                    "Failed to roll back Agent %s after session creation failed: %s",
                    agent.id,
                    safe_exception_summary(exc),
                )
            raise
        return session

    @staticmethod
    def _model_config_data(model_config: Optional[Any]) -> dict[str, str]:
        if model_config is None:
            return {}
        if isinstance(model_config, dict):
            raw = dict(model_config)
        elif hasattr(model_config, "model_dump"):
            raw = model_config.model_dump()
        else:
            raw = {
                key: getattr(model_config, key)
                for key in _MODEL_CONFIG_FIELDS
                if hasattr(model_config, key)
            }

        unexpected = set(raw) - _MODEL_CONFIG_FIELDS
        if unexpected:
            raise BadRequestError(
                f"Unsupported model configuration fields: {', '.join(sorted(unexpected))}"
            )

        result: dict[str, str] = {}
        for key, value in raw.items():
            if value is None:
                continue
            if not isinstance(value, str):
                raise BadRequestError(f"{key} must be a string")
            value = value.strip()
            if not value:
                raise BadRequestError(f"{key} must not be empty")
            result[key] = value
        return result

    async def _resolve_model_config(self, model_config: Optional[Any]) -> dict[str, Any]:
        settings = get_settings()
        values = self._model_config_data(model_config)
        model_id = values.get("model_id")
        custom_fields = _CUSTOM_MODEL_FIELDS.intersection(values)

        if model_id and custom_fields:
            raise BadRequestError(
                "model_id cannot be combined with custom model credentials"
            )
        if custom_fields and custom_fields != _CUSTOM_MODEL_FIELDS:
            missing = ", ".join(sorted(_CUSTOM_MODEL_FIELDS - custom_fields))
            raise BadRequestError(f"Custom model configuration is missing: {missing}")

        if model_id:
            if not _MODEL_ID_PATTERN.fullmatch(model_id):
                raise BadRequestError("Invalid model_id")
            selected = next(
                (model for model in settings.available_models if model.id == model_id),
                None,
            )
            if selected is None:
                raise BadRequestError(f"Unknown model option: {model_id}")
            selected_key = provider_api_key(
                settings, selected.model_provider, selected.api_key
            )
            if not selected_key:
                raise BadRequestError(
                    f"No API key is configured for model option: {model_id}"
                )
            return {
                "model_id": model_id,
                "model_name": selected.model_name,
                "model_provider": selected.model_provider.lower(),
                "api_base": provider_api_base(
                    settings, selected.model_provider, selected.api_base
                ),
                # Catalog/server credentials are resolved by the LLM factory
                # and never copied into a per-session Agent document.
                "api_key": None,
                "is_byok": False,
            }

        if custom_fields:
            provider = values["model_provider"].lower()
            model_name = values["model_name"]
            api_key = values["api_key"]
            if not _PROVIDER_PATTERN.fullmatch(provider):
                raise BadRequestError("Invalid model_provider")
            if provider not in SUPPORTED_BYOK_PROVIDERS:
                raise BadRequestError(f"Unsupported custom model provider: {provider}")
            if not _MODEL_NAME_PATTERN.fullmatch(model_name):
                raise BadRequestError("Invalid model_name")
            if len(api_key) > 8192:
                raise BadRequestError("api_key is too long")
            try:
                api_base = await asyncio.to_thread(
                    validate_public_model_endpoint, values["api_base"]
                )
                validate_model_credential_encryption(settings)
            except (ModelEndpointValidationError, ModelCredentialEncryptionError) as exc:
                raise BadRequestError(str(exc)) from exc
            return {
                "model_id": None,
                "model_name": model_name,
                "model_provider": provider,
                "api_base": api_base,
                "api_key": api_key,
                "is_byok": True,
            }

        return {
            "model_id": None,
            "model_name": settings.model_name,
            "model_provider": settings.model_provider.lower(),
            "api_base": settings.api_base,
            # The deployment credential remains process configuration; it is
            # never duplicated in the Agent collection.
            "api_key": None,
            "is_byok": False,
        }

    async def _create_agent(self, model_config: Optional[Any] = None) -> Agent:
        logger.info("Creating new agent")
        settings = get_settings()
        model = await self._resolve_model_config(model_config)
        agent = Agent(
            model_id=model["model_id"],
            model_name=model["model_name"],
            model_provider=model["model_provider"],
            api_base=model["api_base"],
            api_key=model["api_key"],
            is_byok=model["is_byok"],
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
        )
        logger.info(f"Created new Agent with ID: {agent.id}")
        
        # Save agent to repository
        await self._agent_repository.save(agent)
        logger.info(f"Saved agent {agent.id} to repository")
        
        logger.info(f"Agent created successfully with ID: {agent.id}")
        return agent

    async def chat(
        self,
        session_id: str,
        user_id: str,
        message: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        event_id: Optional[str] = None,
        attachments: Optional[List[FileInfo]] = None,
        submission_id: Optional[str] = None,
        accepted_submission: Optional[TurnSubmission] = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        logger.info(
            "Starting chat: session_id=%s submission_id=%s has_message=%s",
            session_id,
            submission_id,
            bool(message),
        )
        # Directly use the domain service's chat method, which will check if the session exists
        async for event in self._agent_domain_service.chat(
            session_id,
            user_id,
            message,
            timestamp,
            event_id,
            attachments,
            submission_id=submission_id,
            accepted_submission=accepted_submission,
        ):
            logger.debug(
                "Received chat event: session_id=%s submission_id=%s type=%s",
                session_id,
                submission_id,
                type(event).__name__,
            )
            yield event
        logger.info(f"Chat with session {session_id} completed")

    async def accept_chat_submission(
        self,
        *,
        session_id: str,
        user_id: str,
        submission_id: str,
        message: str,
        timestamp: Optional[datetime] = None,
        attachments: Optional[List[FileInfo]] = None,
    ) -> TurnSubmission:
        """Durably accept and dispatch before SSE response headers are sent."""
        try:
            return await self._agent_domain_service.accept_chat_submission(
                session_id=session_id,
                user_id=user_id,
                submission_id=submission_id,
                message=message,
                timestamp=timestamp,
                attachments=attachments,
            )
        except TurnSubmissionConflictError as exc:
            raise ConflictError(str(exc)) from exc
        except TurnSubmissionCapacityError as exc:
            raise TooManyRequestsError(str(exc)) from exc
        except AgentBayQuotaExceededError as exc:
            raise TooManyRequestsError(
                "AgentBay sandbox capacity is currently full"
            ) from exc
        except TurnSubmissionUnavailableError as exc:
            raise ServiceUnavailableError(
                "Chat submission could not be durably accepted; retry with the same submission_id"
            ) from exc
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
    
    async def get_session(self, session_id: str, user_id: Optional[str] = None) -> Optional[Session]:
        """Get a session by ID, ensuring it belongs to the user"""
        logger.info(f"Getting session {session_id} for user {user_id}")
        if not user_id:
            session = await self._session_repository.find_by_id(session_id)
        else:
            session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
        if not session:
            logger.error(f"Session {session_id} not found for user {user_id}")
        return session

    async def get_agent(self, agent_id: str) -> Optional[Agent]:
        """Get persisted per-session model metadata."""
        return await self._agent_repository.find_by_id(agent_id)

    async def get_active_turns(
        self, session_id: str, user_id: str
    ) -> List[TurnSubmission]:
        """Return the user's nonterminal durable turns in submission order."""
        if self._turn_submission_repository is None:
            return []
        return await self._turn_submission_repository.list_active(
            session_id, user_id=user_id
        )
    
    async def get_all_sessions(self, user_id: str) -> List[SessionSummary]:
        """Get all sessions for a specific user (lightweight summaries)"""
        logger.info(f"Getting all sessions for user {user_id}")
        return await self._session_repository.find_summaries_by_user_id(user_id)

    async def delete_session(self, session_id: str, user_id: str) -> None:
        """Cancel work, destroy the sandbox, then delete the session record.

        Missing sessions are treated as already deleted.  Cleanup failures
        intentionally leave the record intact so the operation can be retried
        and a billable sandbox identifier is never orphaned.
        """
        logger.info(f"Deleting session {session_id} for user {user_id}")
        session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
        if not session:
            logger.info(
                "Session %s for user %s is already absent", session_id, user_id
            )
            return

        # Chat's first-message path uses the same lock.  Holding it through
        # cancellation and both deletes prevents a new task from starting
        # between resource cleanup and credential deletion.
        async with self._agent_domain_service._get_session_lock(session_id):
            async def delete_locked() -> None:
                # Re-read Mongo only after acquiring the distributed lease.
                # A preceding delete therefore stays deleted, and a preceding
                # enqueue exposes its task/sandbox for orderly cleanup.
                session = await self._session_repository.find_by_id_and_user_id(
                    session_id, user_id
                )
                if not session:
                    return
                await self._agent_domain_service.cleanup_session_resources(session)
                # Remove the credential first while the session is locked. If
                # the session delete fails, restore the Agent so the visible
                # session never points at missing model configuration.
                agent = await self._agent_repository.find_by_id(session.agent_id)
                try:
                    # Renewal loss can cancel this coroutine at any await. Keep
                    # both destructive writes in the rollback boundary.
                    await self._agent_repository.delete(session.agent_id)
                    await self._session_repository.delete(session_id)
                except BaseException:
                    if agent is not None:
                        try:
                            await asyncio.shield(
                                self._agent_repository.save(agent)
                            )
                        except Exception as exc:
                            logger.error(
                                "Failed to restore Agent %s after session deletion failed: %s",
                                session.agent_id,
                                safe_exception_summary(exc),
                            )
                    raise

            await self._agent_domain_service.run_session_lifecycle_exclusive(
                session_id, delete_locked
            )
        logger.info(f"Session {session_id} deleted successfully")

    async def stop_session(self, session_id: str, user_id: str) -> None:
        """Stop a session, ensuring it belongs to the user"""
        logger.info(f"Stopping session {session_id} for user {user_id}")
        # First verify the session belongs to the user
        session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
        if not session:
            logger.error(f"Session {session_id} not found for user {user_id}")
            raise RuntimeError("Session not found")
        async with self._agent_domain_service._get_session_lock(session_id):
            async def stop_locked() -> None:
                current = await self._session_repository.find_by_id_and_user_id(
                    session_id, user_id
                )
                if not current:
                    raise RuntimeError("Session not found")
                await self._agent_domain_service.stop_session(session_id)

            await self._agent_domain_service.run_session_lifecycle_exclusive(
                session_id, stop_locked
            )
        logger.info(f"Session {session_id} stopped successfully")

    async def clear_unread_message_count(self, session_id: str, user_id: str) -> None:
        """Clear the unread message count for a session, ensuring it belongs to the user"""
        logger.info(f"Clearing unread message count for session {session_id} for user {user_id}")
        session = await self._session_repository.find_by_id_and_user_id(
            session_id, user_id
        )
        if not session:
            logger.error(f"Session {session_id} not found for user {user_id}")
            raise RuntimeError("Session not found")
        await self._session_repository.update_unread_message_count(session_id, 0)
        logger.info(f"Unread message count cleared for session {session_id}")

    async def shutdown(self):
        logger.info("Closing all agents and cleaning up resources")
        # Clean up all Agents and their associated sandboxes
        await self._agent_domain_service.shutdown()
        logger.info("All agents closed successfully")

    async def shell_view(self, session_id: str, shell_session_id: str, user_id: str) -> ShellViewResponse:
        """View shell session output, ensuring session belongs to the user"""
        logger.info(f"Getting shell view for session {session_id} for user {user_id}")
        session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
        if not session:
            logger.error(f"Session {session_id} not found for user {user_id}")
            raise RuntimeError("Session not found")
        
        if not session.sandbox_id:
            raise RuntimeError("Session has no sandbox environment")
        self._verify_sandbox_provider(session)
        
        # Get sandbox and shell output
        sandbox = await self._sandbox_cls.get(session.sandbox_id)
        if not sandbox:
            raise RuntimeError("Sandbox environment not found")
        try:
            result = await sandbox.view_shell(shell_session_id, console=True)
            if result.success:
                return ShellViewResponse(**result.data)
            raise RuntimeError(f"Failed to get shell output: {result.message}")
        finally:
            await _aclose_sandbox_handle(sandbox)

    async def get_vnc_url(self, session_id: str) -> str:
        """Get VNC URL for a session, ensuring it belongs to the user"""
        logger.info(f"Getting VNC URL for session {session_id}")
        
        session = await self._session_repository.find_by_id(session_id)
        if not session:
            logger.error(f"Session {session_id} not found")
            raise RuntimeError("Session not found")
        
        if not session.sandbox_id:
            raise RuntimeError("Session has no sandbox environment")
        self._verify_sandbox_provider(session)
        
        # Get sandbox and return VNC URL
        sandbox = await self._sandbox_cls.get(session.sandbox_id)
        if not sandbox:
            raise RuntimeError("Sandbox environment not found")
        try:
            return sandbox.vnc_url
        finally:
            await _aclose_sandbox_handle(sandbox)

    async def get_preview_proxy_base_url(self, session_id: str) -> str:
        """Return the sandbox API URL used for web-preview proxying."""
        session = await self._session_repository.find_by_id(session_id)
        if not session or not session.sandbox_id:
            raise RuntimeError("Session has no sandbox environment")
        self._verify_sandbox_provider(session)
        sandbox = await self._sandbox_cls.get(session.sandbox_id)
        if not sandbox:
            raise RuntimeError("Sandbox environment not found")
        try:
            base_url = getattr(sandbox, "base_url", None)
            if not base_url:
                raise RuntimeError("Sandbox preview proxy URL not available")
            return base_url.rstrip("/")
        finally:
            await _aclose_sandbox_handle(sandbox)

    async def file_view(self, session_id: str, file_path: str, user_id: str) -> FileViewResponse:
        """View file content, ensuring session belongs to the user"""
        logger.info(f"Getting file view for session {session_id} for user {user_id}")
        session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
        if not session:
            logger.error(f"Session {session_id} not found for user {user_id}")
            raise RuntimeError("Session not found")
        
        if not session.sandbox_id:
            raise RuntimeError("Session has no sandbox environment")
        self._verify_sandbox_provider(session)
        
        # Get sandbox and file content
        sandbox = await self._sandbox_cls.get(session.sandbox_id)
        if not sandbox:
            raise RuntimeError("Sandbox environment not found")
        try:
            result = await sandbox.file_read(file_path)
            if result.success:
                return FileViewResponse(**result.data)
            raise RuntimeError(f"Failed to read file: {result.message}")
        finally:
            await _aclose_sandbox_handle(sandbox)
    
    async def is_session_shared(self, session_id: str) -> bool:
        """Check if a session is shared"""
        logger.info(f"Checking if session {session_id} is shared")
        session = await self._session_repository.find_by_id(session_id)
        if not session:
            logger.error(f"Session {session_id} not found")
            raise RuntimeError("Session not found")
        return session.is_shared

    async def get_session_files(self, session_id: str, user_id: Optional[str] = None) -> List[FileInfo]:
        """Get files for a session, ensuring it belongs to the user"""
        logger.info(f"Getting files for session {session_id} for user {user_id}")
        session = await self.get_session(session_id, user_id)
        return session.files
    
    async def get_shared_session_files(self, session_id: str) -> List[FileInfo]:
        """Get files for a shared session"""
        logger.info(f"Getting files for shared session {session_id}")
        session = await self._session_repository.find_by_id(session_id)
        if not session or not session.is_shared:
            logger.error(f"Shared session {session_id} not found or not shared")
            raise RuntimeError("Session not found")
        return session.files

    async def share_session(self, session_id: str, user_id: str) -> None:
        """Share a session, ensuring it belongs to the user"""
        logger.info(f"Sharing session {session_id} for user {user_id}")
        # First verify the session belongs to the user
        session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
        if not session:
            logger.error(f"Session {session_id} not found for user {user_id}")
            raise RuntimeError("Session not found")
        
        await self._session_repository.update_shared_status(session_id, True)
        logger.info(f"Session {session_id} shared successfully")

    async def unshare_session(self, session_id: str, user_id: str) -> None:
        """Unshare a session, ensuring it belongs to the user"""
        logger.info(f"Unsharing session {session_id} for user {user_id}")
        # First verify the session belongs to the user
        session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
        if not session:
            logger.error(f"Session {session_id} not found for user {user_id}")
            raise RuntimeError("Session not found")
        
        await self._session_repository.update_shared_status(session_id, False)
        logger.info(f"Session {session_id} unshared successfully")

    async def get_shared_session(self, session_id: str) -> Optional[Session]:
        """Get a shared session by ID (no user authentication required)"""
        logger.info(f"Getting shared session {session_id}")
        session = await self._session_repository.find_by_id(session_id)
        if not session or not session.is_shared:
            logger.error(f"Shared session {session_id} not found or not shared")
            return None
        return session
