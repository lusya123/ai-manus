"""
Claw management API routes.
Endpoints for creating, managing, and chatting with OpenClaw instances.
"""
import json
import asyncio
import logging
import time
import httpx
import jwt
from fastapi import APIRouter, Depends, Header, UploadFile, File, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import Response

from app.application.services.claw_service import ClawService
from app.application.services.file_service import FileService
from app.application.errors.exceptions import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.domain.models.claw import ClawAttachment
from app.interfaces.dependencies import get_current_user, get_claw_service, get_file_service
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.claw import (
    ClawResponse,
    ClawHistoryResponse, ClawMessageSchema, ClawAttachmentSchema,
)
from app.interfaces.schemas.file import FileInfoResponse
from app.domain.external.file import (
    FileStorageBusyError,
    FileStorageQuotaExceededError,
    FileTooLargeError,
)
from app.domain.models.user import User
from app.domain.models.user import UserRole
from app.core.config import get_settings
from app.domain.utils.error_reporting import safe_exception_summary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/claw", tags=["claw"])


@router.get("", response_model=APIResponse[ClawResponse])
async def get_claw(
    current_user: User = Depends(get_current_user),
    claw_service: ClawService = Depends(get_claw_service),
) -> APIResponse[ClawResponse]:
    """Get the current user's claw instance"""
    claw = await claw_service.get_claw(current_user.id)
    if not claw:
        raise NotFoundError("No claw instance found")
    return APIResponse.success(ClawResponse.from_domain(claw))


@router.post("", response_model=APIResponse[ClawResponse])
async def create_claw(
    current_user: User = Depends(get_current_user),
    claw_service: ClawService = Depends(get_claw_service),
) -> APIResponse[ClawResponse]:
    """Create a new claw instance for the current user"""
    try:
        claw = await claw_service.create_claw(current_user.id)
    except RuntimeError as e:
        logger.warning(
            "[claw] create request failed for user=%s: %s",
            current_user.id,
            safe_exception_summary(e),
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Claw creation is unavailable; please retry",
        )
    return APIResponse.success(ClawResponse.from_domain(claw))


@router.delete("", response_model=APIResponse[dict])
async def delete_claw(
    current_user: User = Depends(get_current_user),
    claw_service: ClawService = Depends(get_claw_service),
) -> APIResponse[dict]:
    """Delete the current user's claw instance"""
    await claw_service.delete_claw(current_user.id)
    return APIResponse.success({})


@router.post("/upload", response_model=APIResponse[FileInfoResponse])
async def upload_claw_file(
    file: UploadFile = File(...),
    x_claw_api_key: str = Header(..., alias="X-Claw-Api-Key"),
    claw_service: ClawService = Depends(get_claw_service),
    file_service: FileService = Depends(get_file_service),
) -> APIResponse[FileInfoResponse]:
    """Upload a file from the claw workspace to Manus storage (authenticated by claw API key)"""
    user_id = await claw_service.verify_api_key(x_claw_api_key)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid claw API key")
    max_upload_bytes = max(1, int(get_settings().claw_upload_max_bytes))
    upload_size = file.size
    try:
        file.file.seek(0, 2)
        actual_size = file.file.tell()
        file.file.seek(0)
        upload_size = max(upload_size or 0, actual_size)
    except Exception as exc:
        if upload_size is None:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Unable to verify Claw upload size",
            ) from exc
    if upload_size < 0 or upload_size > max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Claw upload exceeds the configured size limit",
        )
    try:
        result = await file_service.upload_file(
            file_data=file.file,
            filename=file.filename or "file",
            user_id=user_id,
            content_type=file.content_type,
        )
    except (FileTooLargeError, FileStorageQuotaExceededError) as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except FileStorageBusyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="File storage is busy; please retry",
        ) from exc
    return APIResponse.success(await FileInfoResponse.from_domain(result))


@router.get("/files/{filename}")
async def download_claw_file(
    filename: str,
    current_user: User = Depends(get_current_user),
    claw_service: ClawService = Depends(get_claw_service),
):
    """Proxy a file download from the user's claw workspace"""
    try:
        content, content_type = await claw_service.get_file(current_user.id, filename)
        return Response(
            content=content,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Claw file request is unavailable",
        )
    except Exception as e:
        logger.error(
            "[claw-file] failed to proxy workspace file for user=%s: %s",
            current_user.id,
            safe_exception_summary(e),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to fetch file from claw")


@router.get("/resolve/{file_id}")
async def resolve_claw_file_meta(
    file_id: str,
    x_claw_api_key: str = Header(..., alias="X-Claw-Api-Key"),
    claw_service: ClawService = Depends(get_claw_service),
    file_service: FileService = Depends(get_file_service),
) -> APIResponse[FileInfoResponse]:
    """Get file metadata for manus-file:// resolution (authenticated by claw API key)"""
    user_id = await claw_service.verify_api_key(x_claw_api_key)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid claw API key")
    file_info = await file_service.get_file_info(file_id, user_id)
    if not file_info:
        raise NotFoundError("File not found")
    return APIResponse.success(await FileInfoResponse.from_domain(file_info))


@router.get("/resolve/{file_id}/download")
async def resolve_claw_file_download(
    file_id: str,
    x_claw_api_key: str = Header(..., alias="X-Claw-Api-Key"),
    claw_service: ClawService = Depends(get_claw_service),
    file_service: FileService = Depends(get_file_service),
):
    """Download file content for manus-file:// resolution (authenticated by claw API key)"""
    user_id = await claw_service.verify_api_key(x_claw_api_key)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid claw API key")
    try:
        file_data, file_info = await file_service.download_file(file_id, user_id)
    except (FileNotFoundError, PermissionError):
        raise NotFoundError("File not found")
    import urllib.parse
    encoded_filename = urllib.parse.quote(file_info.filename, safe='')
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        file_data,
        media_type=file_info.content_type or 'application/octet-stream',
        headers={'Content-Disposition': f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


@router.get("/history", response_model=APIResponse[ClawHistoryResponse])
async def get_history(
    current_user: User = Depends(get_current_user),
    claw_service: ClawService = Depends(get_claw_service),
    file_service: FileService = Depends(get_file_service),
) -> APIResponse[ClawHistoryResponse]:
    """Get chat history for the current user's claw"""
    raw_messages = await claw_service.get_history(current_user.id)
    schemas = []
    for m in raw_messages:
        schema = ClawMessageSchema.from_domain(m)
        if schema.attachments:
            for att in schema.attachments:
                try:
                    att.file_url = await file_service.create_signed_url(
                        att.file_id, current_user.id
                    )
                except Exception:
                    pass
        schemas.append(schema)
    return APIResponse.success(ClawHistoryResponse(messages=schemas))


# ---------------------------------------------------------------
# WebSocket: bidirectional channel for chat messages & events
# ---------------------------------------------------------------

HEARTBEAT_INTERVAL = 15
WS_AUTH_INVALID_CLOSE_CODE = 4001
WS_AUTH_EXPIRED_CLOSE_CODE = 4002


class _ClawInputLimitError(ValueError):
    """Raised when a client-controlled Claw chat input exceeds a hard cap."""


class _ClawAccessTokenExpired(ValueError):
    """A correctly signed internal access token reached its natural expiry."""

async def _resolve_ws_user(token: str | None) -> User:
    """Resolve User for a WebSocket connection.
    In dev mode (auth_provider=none) returns anonymous; otherwise validates token."""
    settings = get_settings()
    if settings.auth_provider.lower() == "none":
        return User(
            id="anonymous", fullname="anonymous",
            email="anonymous@localhost", role=UserRole.USER, is_active=True,
        )
    if not token:
        raise ValueError("Authentication required")
    from app.interfaces.dependencies import get_auth_service
    auth_service = get_auth_service()
    user = await auth_service.verify_token(token)
    if not user or not user.is_active:
        raise ValueError("Authentication failed")
    return user


def _ws_access_token_expiration(token: str | None) -> float | None:
    """Return the expiry epoch for an internal access JWT.

    Sub2API credentials may be opaque and are revalidated remotely before
    every chat instead.  Internal JWT WebSockets require both an ``access``
    token type and an explicit expiry so a long-lived connection cannot
    outlive the credential that opened it.
    """
    settings = get_settings()
    auth_provider = settings.auth_provider.lower()
    if auth_provider in {"none", "sub2api"}:
        return None
    if not token:
        raise ValueError("Authentication required")

    from app.interfaces.dependencies import get_auth_service
    token_service = get_auth_service().token_service
    token_settings = getattr(token_service, "settings", None)
    if token_settings is not None:
        # Verify the signature and token shape while deliberately inspecting
        # exp ourselves. This lets an already-expired but authentic token use
        # the refreshable 4002 close contract instead of looking revoked.
        payload = jwt.decode(
            token,
            token_settings.jwt_secret_key,
            algorithms=[token_settings.jwt_algorithm],
            options={"verify_exp": False},
        )
    else:
        # Lightweight unit-test doubles expose only the public verifier.
        payload = token_service.verify_token(token)
    if not payload or payload.get("type") != "access":
        raise ValueError("Access token required")
    expires_at = payload.get("exp")
    if not isinstance(expires_at, (int, float)):
        raise ValueError("Expiring access token required")
    if expires_at <= time.time():
        raise _ClawAccessTokenExpired("Authentication expired")
    return float(expires_at)


@router.websocket("/ws")
async def claw_ws(websocket: WebSocket):
    """Persistent WebSocket connection for Claw chat.

    Client → Server (JSON):
        {"type": "auth", "token": "..."}  (required first frame)
        {"type": "chat", "message": "...", "session_id": "default"}

    Server → Client (JSON):
        {"type": "auth_ack"}                     (authentication complete)
        {"type": "text", "content": "..."}
        {"type": "file", "file_id": "...", ...}
        {"type": "done", "stop_reason": "end_turn"}
        {"type": "error", "error": "..."}
        {"type": "catchup", "content": "..."}   (on reconnect while response in-progress)
        {"type": "heartbeat"}

    Authentication close codes:
        4001  revoked, disabled, malformed, or otherwise invalid (terminal)
        4002  naturally expired signed access token (client may refresh once)
    """
    await websocket.accept()
    token_expires_at = None
    try:
        # Browser WebSockets cannot set an Authorization header. Authenticate
        # in the first frame so the primary bearer token never appears in the
        # request target or Uvicorn/proxy access logs.
        auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
        if auth_message.get("type") != "auth":
            raise ValueError("Authentication frame required")
        raw_token = auth_message.get("token")
        token = raw_token if isinstance(raw_token, str) else None
        token_expires_at = _ws_access_token_expiration(token)
        user = await _resolve_ws_user(token)
        await websocket.send_json({"type": "auth_ack"})
    except _ClawAccessTokenExpired:
        await websocket.close(
            code=WS_AUTH_EXPIRED_CLOSE_CODE,
            reason="Authentication expired",
        )
        return
    except Exception:
        await websocket.close(
            code=WS_AUTH_INVALID_CLOSE_CODE,
            reason="Unauthorized",
        )
        return

    claw_service: ClawService = get_claw_service()
    settings = get_settings()
    queue = claw_service.event_bus.subscribe(user.id)

    async def _write_events():
        """Forward events from the bus to the WS client + periodic heartbeat."""
        try:
            # Catch-up for in-progress response
            pending = claw_service.get_pending_content(user.id, "default")
            pending_thinking = None
            if pending:
                await websocket.send_json({"type": "catchup", "content": pending})
            else:
                pending_thinking = claw_service.get_pending_thinking_content(
                    user.id, "default"
                )
                if pending_thinking:
                    await websocket.send_json(
                        {"type": "thinking", "content": pending_thinking}
                    )
            get_terminal_event = getattr(
                claw_service, "get_terminal_event", lambda *_: None
            )
            terminal_event = get_terminal_event(user.id, "default")
            if terminal_event is not None and queue.empty():
                # The response may have completed just before this subscriber
                # joined.  Its state-level terminal latch bridges the narrow
                # interval between local fanout and state removal.
                await websocket.send_json(terminal_event)
            elif not pending and not pending_thinking:
                # No active/latching state means the last turn is durably idle.
                # Reconcile a client that disconnected after its final frame.
                # If done is already queued, let that exact event win.
                if queue.empty():
                    is_processing = getattr(
                        claw_service, "is_processing", lambda *_: True
                    )
                    if not is_processing(user.id, "default"):
                        await websocket.send_json(
                            {"type": "done", "stop_reason": "idle"}
                        )

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL)
                    await websocket.send_json(event)
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "heartbeat"})
        except (WebSocketDisconnect, asyncio.CancelledError, Exception):
            pass

    file_service: FileService = get_file_service()

    async def _close_for_invalid_auth(
        reason: str = "Unauthorized",
        *,
        code: int = WS_AUTH_INVALID_CLOSE_CODE,
    ) -> None:
        try:
            await websocket.close(code=code, reason=reason)
        except Exception:
            pass

    async def _ensure_user_still_authorized() -> bool:
        """Recheck expiry, revocation, and current account state per turn."""
        try:
            current_user = await _resolve_ws_user(token)
        except Exception:
            if (
                token_expires_at is not None
                and time.time() >= token_expires_at
            ):
                await _close_for_invalid_auth(
                    "Authentication expired",
                    code=WS_AUTH_EXPIRED_CLOSE_CODE,
                )
            else:
                await _close_for_invalid_auth()
            return False
        if not current_user.is_active or current_user.id != user.id:
            await _close_for_invalid_auth()
            return False
        return True

    async def _close_when_token_expires() -> None:
        if token_expires_at is None:
            await asyncio.Future()
            return
        delay = max(0.0, token_expires_at - time.time())
        if delay:
            await asyncio.sleep(delay)
        await _close_for_invalid_auth(
            "Authentication expired",
            code=WS_AUTH_EXPIRED_CLOSE_CODE,
        )

    async def _process_files(file_ids: list[str], uid: str) -> tuple[str, list[ClawAttachment]]:
        """Download files from GridFS, push to Claw workspace, return
        (MANUS_FILE reference tags for the message, attachment metadata for history).

        Mirrors kimi-claw's file resolution: download → save to workspace → reference tag.
        """
        # A different API replica may not yet be attached to this user's
        # internal control bridge. Resolve and owner-verify it before the first
        # workspace request; never send attachments to a stale persisted IP.
        claw = await claw_service.validate_claw_for_chat(uid)
        claw_base_url = claw.http_base_url

        refs: list[str] = []
        attachments: list[ClawAttachment] = []
        max_file_bytes = max(1, int(settings.claw_chat_max_attachment_bytes))
        max_total_bytes = max(
            max_file_bytes,
            int(settings.claw_chat_max_total_attachment_bytes),
        )
        total_bytes = 0
        for fid in file_ids:
            try:
                stream, info = await file_service.download_file(fid, uid)
                ct = info.content_type or ""
                filename = str(info.filename or fid)[:255]
                try:
                    declared_size = int(info.size or 0)
                except (TypeError, ValueError) as exc:
                    raise _ClawInputLimitError(
                        "Attachment has an invalid size"
                    ) from exc
                if declared_size < 0:
                    raise _ClawInputLimitError("Attachment has an invalid size")
                if declared_size > max_file_bytes:
                    raise _ClawInputLimitError(
                        "Attachment exceeds the per-file size limit"
                    )
                if declared_size > max_total_bytes - total_bytes:
                    raise _ClawInputLimitError(
                        "Attachments exceed the total size limit"
                    )
                if not hasattr(stream, "read"):
                    raise ValueError("Attachment stream is unavailable")
                read_limit = min(
                    max_file_bytes,
                    max_total_bytes - total_bytes,
                )
                raw_buffer = bytearray()
                while len(raw_buffer) <= read_limit:
                    chunk = stream.read(
                        min(1024 * 1024, read_limit + 1 - len(raw_buffer))
                    )
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise ValueError("Attachment stream returned invalid data")
                    if not chunk:
                        break
                    raw_buffer.extend(chunk)
                if len(raw_buffer) > read_limit:
                    raise _ClawInputLimitError(
                        "Attachment exceeds the configured size limit"
                    )
                raw = bytes(raw_buffer)
                total_bytes += len(raw)

                attachments.append(ClawAttachment(
                    file_id=fid, filename=filename,
                    content_type=ct, size=info.size or 0,
                ))

                if not claw_base_url:
                    refs.append(f'<MANUS_FILE name="{filename}" id="{fid}" status="no_claw" />')
                    continue

                # Push file to Claw workspace
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{claw_base_url}/workspace",
                        params={"file_id": fid, "filename": filename},
                        content=raw,
                        headers={"Content-Type": "application/octet-stream"},
                    )
                    resp.raise_for_status()
                    result = resp.json()
                    local_path = result.get("path", "")

                refs.append(
                    f'<MANUS_FILE path="{local_path}" name="{filename}" '
                    f'id="{fid}" type="{ct}" size="{info.size}" />'
                )
                logger.info(
                    "[claw-ws] pushed attachment to workspace: file_id=%s",
                    fid,
                )

            except _ClawInputLimitError:
                raise
            except Exception as e:
                logger.warning(
                    "[claw-ws] failed to process file %s: %s",
                    fid,
                    safe_exception_summary(e),
                )
                refs.append(
                    f'<MANUS_FILE name="{fid}" id="{fid}" status="download_failed" '
                    'reason="file_unavailable" />'
                )

        return "\n".join(refs), attachments

    async def _read_messages():
        """Read messages from the WS client and dispatch them."""
        try:
            while True:
                data = await websocket.receive_json()
                if not isinstance(data, dict):
                    await websocket.send_json({
                        "type": "error",
                        "error": "Invalid Claw WebSocket message",
                    })
                    continue
                msg_type = data.get("type")
                if msg_type == "chat":
                    # A connection is not an authorization snapshot.  Logout,
                    # expiry, or disabling an account must take effect before
                    # any additional file or model work begins.
                    if not await _ensure_user_still_authorized():
                        return

                    raw_message = data.get("message", "")
                    if not isinstance(raw_message, str):
                        await websocket.send_json({
                            "type": "error",
                            "error": "Invalid Claw message",
                        })
                        continue
                    message = raw_message.strip()
                    max_message_bytes = max(
                        1, int(settings.claw_chat_max_message_bytes)
                    )
                    if len(message.encode("utf-8")) > max_message_bytes:
                        await websocket.send_json({
                            "type": "error",
                            "error": "Claw message exceeds the configured size limit",
                        })
                        continue
                    session_id = data.get("session_id", "default")
                    if session_id != "default":
                        await websocket.send_json({
                            "type": "error",
                            "error": "Only the default Claw conversation is supported",
                        })
                        continue
                    file_ids = data.get("file_ids", [])
                    max_attachments = max(
                        0, int(settings.claw_chat_max_attachments)
                    )
                    if (
                        not isinstance(file_ids, list)
                        or len(file_ids) > max_attachments
                        or any(
                            not isinstance(file_id, str)
                            or not file_id
                            or len(file_id) > 256
                            for file_id in file_ids
                        )
                    ):
                        await websocket.send_json({
                            "type": "error",
                            "error": "Invalid or excessive Claw attachments",
                        })
                        continue
                    user_attachments: list[ClawAttachment] = []

                    if file_ids:
                        try:
                            file_refs, user_attachments = await _process_files(
                                file_ids, user.id
                            )
                        except _ClawInputLimitError as exc:
                            await websocket.send_json({
                                "type": "error",
                                "error": str(exc),
                            })
                            continue
                        if file_refs:
                            message = f"{message}\n\n{file_refs}" if message else file_refs

                    if len(message.encode("utf-8")) > max_message_bytes:
                        await websocket.send_json({
                            "type": "error",
                            "error": "Claw message and attachment metadata exceed the configured size limit",
                        })
                        continue

                    if message:
                        # File transfer may take long enough for a token to be
                        # revoked or disabled, so recheck immediately before
                        # reserving and dispatching the chat turn as well.
                        if not await _ensure_user_still_authorized():
                            return
                        try:
                            await claw_service.send_message(user.id, message, session_id)
                            if user_attachments:
                                await claw_service.claw_repository.append_message(
                                    user.id, "attachments", "user", attachments=user_attachments,
                                )
                        except ConflictError:
                            await websocket.send_json({
                                "type": "error",
                                "error": "A Claw response is already in progress",
                            })
                        except ServiceUnavailableError:
                            await websocket.send_json({
                                "type": "error",
                                "error": "Claw chat is temporarily unavailable; please retry",
                            })
                        except Exception as e:
                            logger.error(
                                "[claw-ws] failed to dispatch chat for user=%s: %s",
                                user.id,
                                safe_exception_summary(e),
                            )
                            await websocket.send_json({
                                "type": "error",
                                "error": "Unable to start Claw response",
                            })
        except (WebSocketDisconnect, asyncio.CancelledError, Exception):
            pass

    write_task = asyncio.create_task(_write_events())
    read_task = asyncio.create_task(_read_messages())
    expiry_task = asyncio.create_task(_close_when_token_expires())

    try:
        done, pending = await asyncio.wait(
            [write_task, read_task, expiry_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        claw_service.event_bus.unsubscribe(user.id, queue)
