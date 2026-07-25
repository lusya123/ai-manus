from fastapi import (
    APIRouter,
    Depends,
    WebSocket,
    WebSocketDisconnect,
    Query,
    Request,
    Response,
    HTTPException,
)
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse
from typing import AsyncGenerator, List, Optional
from sse_starlette.event import ServerSentEvent
from datetime import UTC, datetime
import asyncio
import websockets
import logging
import httpx
import re
import posixpath
from urllib.parse import unquote, urlparse, urlsplit, urlunparse, urlunsplit
from app.interfaces.dependencies import get_file_service

from app.application.services.agent_service import AgentService
from app.application.services.token_service import TokenService
from app.application.errors.exceptions import NotFoundError, UnauthorizedError
from app.core.config import get_settings
from app.interfaces.dependencies import get_agent_service, get_current_user, get_optional_current_user, get_token_service, verify_signature, verify_signature_websocket
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.session import (
    ChatRequest, ShellViewRequest, CreateSessionResponse, GetSessionResponse,
    ListSessionItem, ListSessionResponse, ShellViewResponse,
    ShareSessionResponse, SharedSessionResponse, PreviewUrlRequest,
    CreateSessionRequest, AgentModelConfigResponse,
)
from app.interfaces.schemas.file import (
    FileViewRequest,
    FileViewResponse,
    SharedFileInfoResponse,
)
from app.interfaces.schemas.resource import AccessTokenRequest, SignedUrlResponse
from app.interfaces.schemas.event import EventMapper
from app.domain.models.file import FileInfo
from app.domain.models.event import BrowserToolContent, MessageEvent, ToolEvent
from app.domain.models.session import Session, SessionStatus
from app.domain.models.turn_submission import TurnSubmissionState
from app.domain.models.user import User
from app.domain.utils.error_reporting import safe_exception_summary

logger = logging.getLogger(__name__)
SESSION_POLL_INTERVAL = 5

router = APIRouter(prefix="/sessions", tags=["sessions"])

LOCAL_PREVIEW_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
PREVIEW_MAX_REQUEST_BYTES = 8 * 1024 * 1024
PREVIEW_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
PREVIEW_CONTENT_SECURITY_POLICY = (
    "sandbox allow-downloads allow-forms allow-modals allow-scripts; "
    "base-uri 'none'; object-src 'none'"
)
# Generated apps are untrusted documents on the main application's host. Only
# inert representation/redirect metadata may cross the proxy boundary; all
# policy, authentication, cookie, worker-scope, and CORS headers are replaced
# or discarded below.
PREVIEW_RESPONSE_HEADER_ALLOWLIST = frozenset(
    {
        "accept-ranges",
        "content-disposition",
        "content-language",
        "content-range",
        "etag",
        "last-modified",
        "location",
    }
)
_PREVIEW_PATH_DECODE_LIMIT = 16


def _share_authorized_file_index(session: Session) -> dict[str, FileInfo]:
    """Return files explicitly linked to this shared session's public events."""

    canonical_files = {file.file_id: file for file in session.files}
    files: dict[str, FileInfo] = {}
    for event in session.events:
        if isinstance(event, MessageEvent):
            for attachment in event.attachments or []:
                canonical_file = canonical_files.get(attachment.file_id)
                if canonical_file:
                    files.setdefault(canonical_file.file_id, canonical_file)
        elif (
            isinstance(event, ToolEvent)
            and isinstance(event.tool_content, BrowserToolContent)
            and event.tool_content.screenshot
        ):
            canonical_file = canonical_files.get(event.tool_content.screenshot)
            if canonical_file:
                files.setdefault(canonical_file.file_id, canonical_file)
    return files


def _blocked_preview_ports() -> set[int]:
    """Ports that expose sandbox control planes rather than user apps."""
    settings = get_settings()
    return {
        8080,
        8222,
        9222,
        5900,
        5901,
        30150,
        30151,
        30152,
        settings.sandbox_api_port,
        settings.sandbox_cdp_port,
        settings.sandbox_vnc_port,
        settings.agentbay_api_port,
        settings.agentbay_cdp_port,
        settings.agentbay_vnc_port,
    }


def _validate_preview_port(port: int) -> None:
    if not 1 <= port <= 65535:
        raise HTTPException(status_code=400, detail="Invalid preview port")
    if port in _blocked_preview_ports():
        raise HTTPException(
            status_code=403,
            detail="Sandbox management ports cannot be previewed",
        )


def _validate_preview_path(path: str) -> None:
    """Reject URL-normalization tricks before crossing the proxy boundary.

    Both Starlette and the downstream HTTP clients may decode or normalize a
    path. Repeated decoding catches direct, percent-encoded, and nested
    percent-encoded dot segments before any client gets a chance to turn them
    into a path outside ``/api/v1/proxy/{port}``.
    """

    candidate = path
    for _ in range(_PREVIEW_PATH_DECODE_LIMIT):
        if "\\" in candidate or any(
            ord(character) < 32 or ord(character) == 127
            for character in candidate
        ):
            raise HTTPException(status_code=400, detail="Invalid preview path")
        if any(segment in {".", ".."} for segment in candidate.split("/")):
            raise HTTPException(status_code=400, detail="Invalid preview path")
        try:
            decoded = unquote(candidate, errors="strict")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid preview path"
            ) from exc
        if decoded == candidate:
            return
        candidate = decoded
    raise HTTPException(status_code=400, detail="Invalid preview path")


def _build_preview_target_url(
    sandbox_base: str,
    port: int,
    path: str,
    request_query: str,
) -> str:
    """Build a sandbox proxy URL while preserving gateway capability queries."""

    _validate_preview_path(path)
    parsed_base = urlsplit(sandbox_base)
    if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
        raise ValueError("Invalid sandbox preview proxy URL")

    proxy_prefix = f"{parsed_base.path.rstrip('/')}/api/v1/proxy/{port}"
    target_path = f"{proxy_prefix}/{path}" if path else f"{proxy_prefix}/"
    # This assertion remains independent of the validator so future path
    # construction changes cannot silently escape the intended proxy prefix.
    normalized_target = posixpath.normpath(target_path)
    if normalized_target != proxy_prefix and not normalized_target.startswith(
        f"{proxy_prefix}/"
    ):
        raise ValueError("Invalid preview proxy target path")

    query = "&".join(
        part for part in (parsed_base.query, request_query) if part
    )
    return urlunsplit(
        (
            parsed_base.scheme,
            parsed_base.netloc,
            target_path,
            query,
            "",
        )
    )


def _rewrite_preview_location(location: str, prefix: str) -> Optional[str]:
    """Keep redirects inside the capability prefix or send them off-origin."""

    if any(ord(character) < 32 or ord(character) == 127 for character in location):
        return None
    parsed = urlsplit(location)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return None

    host = (parsed.hostname or "").lower()
    absolute = bool(parsed.scheme or parsed.netloc)
    if absolute and host not in LOCAL_PREVIEW_HOSTS:
        return location

    try:
        _validate_preview_path(parsed.path)
    except HTTPException:
        return None

    if absolute or parsed.path.startswith("/"):
        redirect_path = f"{prefix}{parsed.path or '/'}"
        return urlunsplit(("", "", redirect_path, parsed.query, parsed.fragment))
    # A safe relative redirect resolves below the already-prefixed request
    # path. Query-only and fragment-only redirects are safe for the same reason.
    return location


def _model_id_for_agent(
    model_name: str, model_provider: str, api_base: Optional[str]
) -> Optional[str]:
    settings = get_settings()
    for model in settings.available_models:
        if (
            model.model_name == model_name
            and model.model_provider == model_provider
            and (model.api_base or settings.api_base) == api_base
        ):
            return model.id
    return None


def _rewrite_preview_content(content: bytes, content_type: str, prefix: str) -> bytes:
    """Rewrite root-relative assets so apps work below the proxy prefix."""
    lowered = content_type.lower()
    if not any(
        kind in lowered
        for kind in ("text/html", "text/css", "javascript", "ecmascript")
    ):
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content

    escaped_prefix = prefix.rstrip("/")
    # Every rewrite inserts the prefix before an existing slash.  This cheap
    # upper bound prevents a small, adversarial HTML document containing only
    # root-relative attributes from expanding far beyond the proxy response
    # cap during regex substitution.
    maximum_rewritten_size = len(content) + text.count("/") * len(
        escaped_prefix.encode("utf-8")
    )
    if maximum_rewritten_size > PREVIEW_MAX_RESPONSE_BYTES:
        logger.warning("Skipping preview URL rewrite because expansion is too large")
        return content
    if "text/html" in lowered:
        text = re.sub(
            r'(\b(?:src|href|action|poster)=["\'])/(?!/)',
            rf'\1{escaped_prefix}/',
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r'(\bsrcset=["\'][^"\']*?)(\s|^)/(?!/)',
            rf'\1\2{escaped_prefix}/',
            text,
            flags=re.IGNORECASE,
        )
    if "text/css" in lowered or "text/html" in lowered:
        text = re.sub(r'url\((["\']?)/(?!/)', rf'url(\1{escaped_prefix}/', text)
    if "javascript" in lowered or "ecmascript" in lowered:
        text = re.sub(
            r'((?:from|import)\s*\(?\s*["\'])/(?!/)',
            rf'\1{escaped_prefix}/',
            text,
        )
    rewritten = text.encode("utf-8")
    return rewritten if len(rewritten) <= PREVIEW_MAX_RESPONSE_BYTES else content


async def _read_limited_request_body(request: Request) -> bytes:
    """Read a preview request body with a hard decoded-byte limit."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > PREVIEW_MAX_REQUEST_BYTES:
                raise HTTPException(
                    status_code=413, detail="Preview request body is too large"
                )
        except ValueError:
            # A malformed length must not bypass the streamed byte counter.
            pass

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > PREVIEW_MAX_REQUEST_BYTES:
            raise HTTPException(
                status_code=413, detail="Preview request body is too large"
            )
        body.extend(chunk)
    return bytes(body)


async def _read_limited_preview_response(response: httpx.Response) -> bytes:
    """Buffer only a bounded preview response for content rewriting."""
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > PREVIEW_MAX_RESPONSE_BYTES:
                raise HTTPException(
                    status_code=413, detail="Preview response body is too large"
                )
        except ValueError:
            pass

    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > PREVIEW_MAX_RESPONSE_BYTES:
            raise HTTPException(
                status_code=413, detail="Preview response body is too large"
            )
        body.extend(chunk)
    return bytes(body)

@router.put("", response_model=APIResponse[CreateSessionResponse])
async def create_session(
    request: CreateSessionRequest | None = None,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[CreateSessionResponse]:
    session = await agent_service.create_session(
        current_user.id, request.agent_model_config if request else None
    )
    return APIResponse.success(
        CreateSessionResponse(
            session_id=session.id,
        )
    )

@router.get("/{session_id}", response_model=APIResponse[GetSessionResponse])
async def get_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[GetSessionResponse]:
    session = await agent_service.get_session(session_id, current_user.id)
    if not session:
        raise NotFoundError("Session not found")
    agent = await agent_service.get_agent(session.agent_id)
    active_turns = await agent_service.get_active_turns(
        session_id, current_user.id
    )
    if any(
        turn.state == TurnSubmissionState.RUNNING for turn in active_turns
    ):
        effective_status = SessionStatus.RUNNING
    elif active_turns:
        effective_status = SessionStatus.PENDING
    else:
        effective_status = session.status
    model_config = None
    if agent:
        model_config = AgentModelConfigResponse(
            model_id=agent.model_id or _model_id_for_agent(
                agent.model_name, agent.model_provider, agent.api_base
            ),
            model_name=agent.model_name,
            model_provider=agent.model_provider,
            api_base=agent.api_base,
        )
    return APIResponse.success(GetSessionResponse(
        session_id=session.id,
        title=session.title,
        status=effective_status,
        events=await EventMapper.events_to_sse_events(session.events),
        is_shared=session.is_shared,
        agent_model_config=model_config,
    ))

@router.delete("/{session_id}", response_model=APIResponse[None])
async def delete_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[None]:
    await agent_service.delete_session(session_id, current_user.id)
    return APIResponse.success()

@router.post("/{session_id}/stop", response_model=APIResponse[None])
async def stop_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[None]:
    await agent_service.stop_session(session_id, current_user.id)
    return APIResponse.success()

@router.post("/{session_id}/clear_unread_message_count", response_model=APIResponse[None])
async def clear_unread_message_count(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[None]:
    await agent_service.clear_unread_message_count(session_id, current_user.id)
    return APIResponse.success()

@router.get("", response_model=APIResponse[ListSessionResponse])
async def get_all_sessions(
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[ListSessionResponse]:
    summaries = await agent_service.get_all_sessions(current_user.id)
    session_items = [ListSessionItem.from_domain(s) for s in summaries]
    return APIResponse.success(ListSessionResponse(sessions=session_items))

@router.post("")
async def stream_sessions(
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> EventSourceResponse:
    async def event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        while True:
            summaries = await agent_service.get_all_sessions(current_user.id)
            session_items = [ListSessionItem.from_domain(s) for s in summaries]
            yield ServerSentEvent(
                event="sessions",
                data=ListSessionResponse(sessions=session_items).model_dump_json()
            )
            await asyncio.sleep(SESSION_POLL_INTERVAL)
    return EventSourceResponse(event_generator())

@router.post("/{session_id}/chat")
async def chat(
    session_id: str,
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> EventSourceResponse:
    timestamp = (
        datetime.fromtimestamp(request.timestamp, UTC)
        if request.timestamp
        else None
    )
    attachments = (
        [attachment.to_domain() for attachment in request.attachments]
        if request.attachments
        else None
    )
    accepted_submission = None
    has_new_submission = bool(request.message) or bool(attachments)
    if has_new_submission:
        # Acceptance/409/429/503 happens before SSE response headers. Retrying
        # the same UUID after a lost response is therefore safe and resumable.
        accepted_submission = await agent_service.accept_chat_submission(
            session_id=session_id,
            user_id=current_user.id,
            submission_id=str(request.submission_id),
            message=request.message or "",
            timestamp=timestamp,
            attachments=attachments,
        )

    async def event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        async for event in agent_service.chat(
            session_id=session_id,
            user_id=current_user.id,
            message=request.message,
            timestamp=timestamp,
            event_id=request.event_id,
            attachments=attachments,
            submission_id=(
                str(request.submission_id) if request.submission_id else None
            ),
            accepted_submission=accepted_submission,
        ):
            logger.debug(
                "Received chat event: session_id=%s type=%s",
                session_id,
                type(event).__name__,
            )
            sse_event = await EventMapper.event_to_sse_event(event)
            logger.debug(
                "Mapped chat event: session_id=%s event=%s",
                session_id,
                sse_event.event if sse_event else "ignored",
            )
            if sse_event:
                yield ServerSentEvent(
                    event=sse_event.event,
                    data=sse_event.data.model_dump_json() if sse_event.data else None,
                    id=(
                        sse_event.data.transport_cursor
                        if sse_event.data
                        else None
                    ),
                )

    return EventSourceResponse(event_generator())

@router.post("/{session_id}/shell")
async def view_shell(
    session_id: str,
    request: ShellViewRequest,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[ShellViewResponse]:
    """View shell session output
    
    If the agent does not exist or fails to get shell output, an appropriate exception will be thrown and handled by the global exception handler
    
    Args:
        session_id: Session ID
        request: Shell view request containing session ID
        
    Returns:
        APIResponse with shell output
    """
    result = await agent_service.shell_view(session_id, request.session_id, current_user.id)
    return APIResponse.success(result)

@router.post("/{session_id}/file")
async def view_file(
    session_id: str,
    request: FileViewRequest,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[FileViewResponse]:
    """View file content
    
    If the agent does not exist or fails to get file content, an appropriate exception will be thrown and handled by the global exception handler
    
    Args:
        session_id: Session ID
        request: File view request containing file path
        
    Returns:
        APIResponse with file content
    """
    result = await agent_service.file_view(session_id, request.file, current_user.id)
    return APIResponse.success(result)

@router.websocket("/{session_id}/vnc")
async def vnc_websocket(
    websocket: WebSocket,
    session_id: str,
    signature: str = Depends(verify_signature_websocket),
    agent_service: AgentService = Depends(get_agent_service)
) -> None:
    """VNC WebSocket endpoint (binary mode)
    
    Establishes a connection with the VNC WebSocket service in the sandbox environment and forwards data bidirectionally
    Supports authentication via signed URL with signature verification
    
    Args:
        websocket: WebSocket connection
        session_id: Session ID
        signature: Verified signature from dependency injection
    """
    
    await websocket.accept(subprotocol="binary")
    logger.info(f"Accepted WebSocket connection for session {session_id}")
    
    try:
        # Get sandbox environment address with user validation
        sandbox_ws_url = await agent_service.get_vnc_url(session_id)

        # AgentBay gateway URLs contain short-lived bearer capabilities. Never
        # persist the resolved VNC URL in logs.
        logger.info("Connecting to sandbox VNC WebSocket for session %s", session_id)
    
        # Connect to sandbox WebSocket
        async with websockets.connect(sandbox_ws_url) as sandbox_ws:
            logger.info("Connected to sandbox VNC WebSocket for session %s", session_id)
            # Create two tasks to forward data bidirectionally
            async def forward_to_sandbox():
                try:
                    while True:
                        data = await websocket.receive_bytes()
                        await sandbox_ws.send(data)
                except WebSocketDisconnect:
                    logger.info("Web -> VNC connection closed")
                    pass
                except Exception as e:
                    logger.error(
                        "Error forwarding data to sandbox: %s",
                        safe_exception_summary(e),
                    )
            
            async def forward_from_sandbox():
                try:
                    while True:
                        data = await sandbox_ws.recv()
                        await websocket.send_bytes(data)
                except websockets.exceptions.ConnectionClosed:
                    logger.info("VNC -> Web connection closed")
                    pass
                except Exception as e:
                    logger.error(
                        "Error forwarding data from sandbox: %s",
                        safe_exception_summary(e),
                    )
            
            # Run two forwarding tasks concurrently
            forward_task1 = asyncio.create_task(forward_to_sandbox())
            forward_task2 = asyncio.create_task(forward_from_sandbox())
            forward_tasks = (forward_task1, forward_task2)
            try:
                # Wait for either task to complete (meaning connection closed).
                await asyncio.wait(
                    forward_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                logger.info("WebSocket connection closed")
            finally:
                # Parent cancellation must not strand the opposite direction's
                # socket read. Observe both task outcomes before the upstream
                # WebSocket context is allowed to close.
                for task in forward_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*forward_tasks, return_exceptions=True)
    
    except ConnectionError as e:
        summary = safe_exception_summary(e)
        logger.error("Unable to connect to sandbox environment: %s", summary)
        await websocket.close(
            code=1011,
            reason=f"Unable to connect to sandbox environment: {summary}",
        )
    except Exception as e:
        summary = safe_exception_summary(e)
        logger.error("WebSocket error: %s", summary)
        await websocket.close(code=1011, reason=f"WebSocket error: {summary}")

@router.get("/{session_id}/files")
async def get_session_files(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[List[FileInfo]]:
    files = await agent_service.get_session_files(session_id, current_user.id)
    return APIResponse.success(files)


@router.post("/{session_id}/vnc/signed-url", response_model=APIResponse[SignedUrlResponse])
async def create_vnc_signed_url(
    session_id: str,
    request_data: AccessTokenRequest,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
    token_service: TokenService = Depends(get_token_service)
) -> APIResponse[SignedUrlResponse]:
    """Generate signed URL for VNC WebSocket access
    
    This endpoint creates a signed URL that allows temporary access to the VNC
    WebSocket for a specific session without requiring authentication headers.
    """
    
    # Validate expiration time (max 15 minutes)
    expire_minutes = request_data.expire_minutes
    if expire_minutes > 15:
        expire_minutes = 15
    
    # Check if session exists and belongs to user
    session = await agent_service.get_session(session_id, current_user.id)
    if not session:
        raise NotFoundError("Session not found")
    
    # Create signed URL for VNC WebSocket
    ws_base_url = f"/api/v1/sessions/{session_id}/vnc"
    signed_url = token_service.create_signed_url(
        base_url=ws_base_url,
        expire_minutes=expire_minutes
    )
    
    logger.info(f"Created signed URL for VNC access for user {current_user.id}, session {session_id}")
    
    return APIResponse.success(SignedUrlResponse(
        signed_url=signed_url,
        expires_in=expire_minutes * 60,
    ))


@router.post("/{session_id}/preview-url", response_model=APIResponse[SignedUrlResponse])
async def create_preview_url(
    session_id: str,
    request_data: PreviewUrlRequest,
    current_user: Optional[User] = Depends(get_optional_current_user),
    agent_service: AgentService = Depends(get_agent_service),
    token_service: TokenService = Depends(get_token_service),
) -> APIResponse[SignedUrlResponse]:
    session = await agent_service.get_session(
        session_id, current_user.id if current_user else None
    )
    if not session:
        raise NotFoundError("Session not found")
    if not current_user and not session.is_shared:
        raise UnauthorizedError()

    raw_url = request_data.url.strip()
    parsed = urlparse(raw_url if "://" in raw_url else f"http://{raw_url}")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid preview URL")

    host = parsed.hostname or ""
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid preview port") from exc
    path = parsed.path or "/"
    if host.lower() not in LOCAL_PREVIEW_HOSTS:
        return APIResponse.success(
            SignedUrlResponse(
                signed_url=urlunparse(parsed),
                # This is an unchanged third-party URL, not a signed token we
                # control.  Do not claim it expires.
                expires_in=0,
            )
        )

    _validate_preview_port(port)
    _validate_preview_path(path)

    expire_minutes = min(request_data.expire_minutes, 15)
    shared_access = current_user is None
    resource_id = f"{session_id}:{port}"
    if shared_access:
        resource_id = f"{resource_id}:{session.share_epoch}"
    token = token_service.create_resource_access_token(
        resource_type="preview",
        resource_id=resource_id,
        user_id=current_user.id if current_user else "shared",
        expire_minutes=expire_minutes,
    )
    preview_path = f"/api/v1/sessions/{session_id}/preview/{token}/{port}{path}"
    if parsed.query:
        preview_path = f"{preview_path}?{parsed.query}"
    return APIResponse.success(
        SignedUrlResponse(
            signed_url=preview_path,
            expires_in=expire_minutes * 60,
        )
    )


@router.api_route(
    "/{session_id}/preview/{token}/{port}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
@router.api_route(
    "/{session_id}/preview/{token}/{port}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_preview(
    request: Request,
    session_id: str,
    token: str,
    port: int,
    path: str = "",
    agent_service: AgentService = Depends(get_agent_service),
    token_service: TokenService = Depends(get_token_service),
) -> Response:
    payload = token_service.verify_token(token)
    if (
        not payload
        or payload.get("type") != "resource_access"
        or payload.get("resource_type") != "preview"
    ):
        raise UnauthorizedError()

    _validate_preview_port(port)
    _validate_preview_path(path)
    shared_access = payload.get("user_id") == "shared"
    if shared_access:
        if request.method not in {"GET", "HEAD"}:
            raise HTTPException(
                status_code=405,
                detail="Shared previews are read-only",
                headers={"Allow": "GET, HEAD"},
            )
        shared_session = await agent_service.get_shared_session(session_id)
        if (
            not shared_session
            or payload.get("resource_id")
            != f"{session_id}:{port}:{shared_session.share_epoch}"
        ):
            raise UnauthorizedError()
    elif payload.get("resource_id") != f"{session_id}:{port}":
        raise UnauthorizedError()

    try:
        sandbox_base = await agent_service.get_preview_proxy_base_url(session_id)
        target_url = _build_preview_target_url(
            sandbox_base, port, path, request.url.query
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Unable to resolve preview proxy for session %s: %s",
            session_id,
            safe_exception_summary(exc),
        )
        raise HTTPException(
            status_code=502, detail="Preview application is unavailable"
        ) from None

    excluded = {
        "host",
        "connection",
        "content-length",
        "transfer-encoding",
        "content-encoding",
        "authorization",
        "cookie",
        "proxy-authorization",
        # The preview URL contains a bearer token in its path. Do not expose it
        # to the untrusted application through the browser's Referer header.
        "referer",
    }
    outbound_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in excluded
    }
    request_body = await _read_limited_request_body(request)
    try:
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=30.0
        ) as client:
            proxy_request = client.build_request(
                request.method,
                target_url,
                content=request_body,
                headers=outbound_headers,
            )
            proxied = await client.send(proxy_request, stream=True)
            try:
                proxied_content = await _read_limited_preview_response(proxied)
            finally:
                await proxied.aclose()
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.warning(
            "Preview upstream request failed for session %s: %s",
            session_id,
            safe_exception_summary(exc),
        )
        raise HTTPException(
            status_code=502, detail="Preview application is unavailable"
        ) from None

    response_headers = {
        key: value
        for key, value in proxied.headers.items()
        if key.lower() in PREVIEW_RESPONSE_HEADER_ALLOWLIST
    }
    response_headers["X-Content-Type-Options"] = "nosniff"
    response_headers["Content-Security-Policy"] = (
        PREVIEW_CONTENT_SECURITY_POLICY
    )
    response_headers["Referrer-Policy"] = "no-referrer"
    response_headers["Cache-Control"] = "no-store"
    response_headers["X-Frame-Options"] = "SAMEORIGIN"
    # CSP sandboxing gives the document an opaque ``null`` origin. Permit
    # credential-free calls back to its own capability URL so ordinary SPAs
    # remain usable, while never reflecting an upstream CORS policy.
    response_headers["Access-Control-Allow-Origin"] = "null"
    if request.method == "OPTIONS":
        response_headers["Access-Control-Allow-Methods"] = (
            "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS"
        )
        requested_headers = request.headers.get(
            "access-control-request-headers", ""
        )
        safe_requested_headers = [
            header.strip()
            for header in requested_headers.split(",")
            if header.strip()
            and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header.strip())
            and header.strip().lower()
            not in {"authorization", "cookie", "proxy-authorization"}
        ]
        if safe_requested_headers:
            response_headers["Access-Control-Allow-Headers"] = ", ".join(
                safe_requested_headers
            )
    prefix = f"/api/v1/sessions/{session_id}/preview/{token}/{port}"
    location = response_headers.get("location")
    if location:
        rewritten_location = _rewrite_preview_location(location, prefix)
        if rewritten_location is None:
            response_headers.pop("location", None)
        else:
            response_headers["location"] = rewritten_location

    content_type = proxied.headers.get("content-type", "")
    content = _rewrite_preview_content(proxied_content, content_type, prefix)
    return Response(
        content=content,
        status_code=proxied.status_code,
        headers=response_headers,
        media_type=content_type.split(";", 1)[0] if content_type else None,
    )


@router.post("/{session_id}/share", response_model=APIResponse[ShareSessionResponse])
async def share_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[ShareSessionResponse]:
    """Share a session to make it publicly accessible
    
    This endpoint marks a session as shared, allowing it to be accessed
    without authentication using the shared session endpoint.
    """
    await agent_service.share_session(session_id, current_user.id)
    return APIResponse.success(ShareSessionResponse(
        session_id=session_id,
        is_shared=True
    ))

@router.get("/{session_id}/share/files")
async def get_shared_session_files(
    session_id: str,
    agent_service: AgentService = Depends(get_agent_service),
    file_service=Depends(get_file_service),
) -> APIResponse[List[SharedFileInfoResponse]]:
    session = await agent_service.get_shared_session(session_id)
    if not session:
        raise NotFoundError("Shared session not found")
    files = _share_authorized_file_index(session).values()
    public_files = [
        await SharedFileInfoResponse.from_domain(
            file,
            session_id,
            await file_service.create_shared_session_signed_url(
                session_id, file.file_id, session.share_epoch
            ),
        )
        for file in files
    ]
    return APIResponse.success(public_files)


@router.get("/{session_id}/share/files/{file_id}")
async def download_shared_session_file(
    session_id: str,
    file_id: str,
    share_epoch: str = Query(..., min_length=1, max_length=128),
    signature: str = Depends(verify_signature),
    agent_service: AgentService = Depends(get_agent_service),
    file_service=Depends(get_file_service),
):
    """Download a shared file while its parent session remains public."""

    session = await agent_service.get_shared_session(session_id)
    if not session or session.share_epoch != share_epoch:
        raise NotFoundError("Shared session not found")
    shared_file = _share_authorized_file_index(session).get(file_id)
    if shared_file is None:
        raise NotFoundError("File not found")

    try:
        file_data, stored_info = await file_service.download_file_by_capability(
            file_id
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise NotFoundError("File not found") from exc

    import urllib.parse

    encoded_filename = urllib.parse.quote(stored_info.filename, safe="")
    return StreamingResponse(
        file_data,
        media_type=stored_info.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": (
                "attachment; filename*=UTF-8''" + encoded_filename
            )
        },
    )


@router.delete("/{session_id}/share", response_model=APIResponse[ShareSessionResponse])
async def unshare_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[ShareSessionResponse]:
    """Unshare a session to make it private again
    
    This endpoint marks a session as not shared, removing public access.
    """
    await agent_service.unshare_session(session_id, current_user.id)
    return APIResponse.success(ShareSessionResponse(
        session_id=session_id,
        is_shared=False
    ))


@router.get("/shared/{session_id}", response_model=APIResponse[SharedSessionResponse])
async def get_shared_session(
    session_id: str,
    agent_service: AgentService = Depends(get_agent_service),
    file_service=Depends(get_file_service),
) -> APIResponse[SharedSessionResponse]:
    """Get a shared session without authentication
    
    This endpoint allows public access to sessions that have been marked as shared.
    No authentication is required, but the session must be explicitly shared.
    """
    session = await agent_service.get_shared_session(session_id)
    if not session:
        raise NotFoundError("Shared session not found")
    
    return APIResponse.success(SharedSessionResponse(
        session_id=session.id,
        title=session.title,
        status=session.status,
        events=await EventMapper.events_to_shared_sse_events(
            session.events,
            session_id=session.id,
            share_epoch=session.share_epoch,
            shared_files=_share_authorized_file_index(session),
            file_service=file_service,
        ),
        is_shared=session.is_shared
    ))
