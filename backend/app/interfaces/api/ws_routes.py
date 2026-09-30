"""WebSocket routes for realtime session list and chat."""

from __future__ import annotations

import asyncio
import json
import websockets
import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from pydantic import ValidationError as PydanticValidationError

from app.application.errors.exceptions import (
    BadRequestError,
    ConflictError,
    ServiceUnavailableError,
    TooManyRequestsError,
)
from app.core.config import get_settings
from app.domain.models.file import FileInfo
from app.domain.models.turn_submission import TurnSubmission
from app.domain.utils.error_reporting import safe_exception_summary
from app.interfaces.dependencies import (
    resolve_ws_user,
    get_agent_service,
)
from app.interfaces.schemas.event import EventMapper
from app.interfaces.schemas.session import ChatRequest, ListSessionItem
from app.infrastructure.storage.redis import get_redis
from app.infrastructure.external.session_list import (
    channel_for_user,
    parse_notify_payload,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ws", tags=["ws"])

SESSION_LIST_KEEPALIVE_SECONDS = 20.0
CHAT_WS_PING_SECONDS = 20.0
WS_AUTH_WATCHDOG_SECONDS = 20.0
WS_JSON_MAX_DEPTH = 64

# Chat WS protocol (aligned with official Manus control-plane contract).
CHAT_WS_PROTOCOL_VERSION = 2
ERR_BAD_VERSION = 4000
ERR_BAD_REQUEST = 4002
ERR_NOT_FOUND = 4004
ERR_NOT_JOINED = 4009
ERR_INTERNAL = 5000
CHAT_WS_REQUEST_ID_MAX_CHARS = 128
CHAT_WS_SESSION_ID_MAX_CHARS = 128
CHAT_WS_EVENT_ID_MAX_CHARS = 128


class _WsFrameTooLargeError(ValueError):
    """Raised before JSON decoding when a WebSocket frame is too large."""


async def _receive_bounded_json(websocket: WebSocket, max_bytes: int) -> Any:
    """Receive one bounded JSON frame without parsing an unbounded payload."""
    message = await websocket.receive()
    message_type = message.get("type")
    if message_type == "websocket.disconnect":
        raise WebSocketDisconnect(
            code=message.get("code", 1000),
            reason=message.get("reason", ""),
        )
    if message_type != "websocket.receive":
        raise ValueError("Invalid WebSocket frame")

    text = message.get("text")
    raw_bytes = message.get("bytes")
    if text is not None:
        encoded = text.encode("utf-8")
    elif raw_bytes is not None:
        encoded = bytes(raw_bytes)
    else:
        raise ValueError("Invalid WebSocket frame")

    if len(encoded) > max_bytes:
        raise _WsFrameTooLargeError("WebSocket frame is too large")
    depth = 0
    in_string = False
    escaped = False
    for byte in encoded:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ or {
            depth += 1
            if depth > WS_JSON_MAX_DEPTH:
                raise ValueError("JSON nesting is too deep")
        elif byte in (0x5D, 0x7D):  # ] or }
            depth = max(0, depth - 1)
    try:
        return json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid JSON") from exc


async def _enforce_ws_origin(websocket: WebSocket) -> bool:
    """Reject browser WebSockets outside the exact configured origin set."""
    settings = get_settings()
    headers = getattr(websocket, "headers", {}) or {}
    cookies = getattr(websocket, "cookies", {}) or {}
    origin = headers.get("origin") or headers.get("Origin")
    authorization = headers.get("authorization") or headers.get("Authorization")
    has_bearer = bool(
        isinstance(authorization, str)
        and authorization.lower().startswith("bearer ")
        and authorization[7:].strip()
    )
    cookie_name = getattr(settings, "session_cookie_name", "session_id")
    has_session_cookie = bool(cookies.get(cookie_name))

    allowed = True
    if origin is not None:
        get_origins = getattr(settings, "get_cors_allowed_origins", None)
        allowed_origins = get_origins() if get_origins else []
        allowed = origin in allowed_origins
    elif has_session_cookie and not has_bearer:
        # Native clients use Authorization Bearer and may omit Origin. A
        # Cookie-authenticated browser handshake must always prove its origin.
        allowed = False

    if allowed:
        return True
    try:
        await websocket.close(code=4003, reason="Forbidden")
    except Exception:
        pass
    return False


async def _ws_user_still_authorized(
    websocket: WebSocket,
    expected_user_id: str,
) -> bool:
    """Re-resolve an opaque session and require the same active user."""
    try:
        current_user = await resolve_ws_user(websocket)
    except Exception:
        return False
    return bool(
        getattr(current_user, "is_active", False)
        and current_user.id == expected_user_id
    )


async def _enforce_ws_authorization(
    websocket: WebSocket,
    expected_user_id: str,
) -> bool:
    if await _ws_user_still_authorized(websocket, expected_user_id):
        return True
    try:
        await websocket.close(code=4001, reason="Unauthorized")
    except Exception:
        pass
    return False


async def _ws_auth_watchdog(
    websocket: WebSocket,
    expected_user_id: str,
) -> None:
    """Close a long-lived socket promptly after auth session revocation."""
    while True:
        await asyncio.sleep(WS_AUTH_WATCHDOG_SECONDS)
        if not await _enforce_ws_authorization(websocket, expected_user_id):
            return


def _session_status_value(status: Any) -> str:
    return getattr(status, "value", status) if status is not None else "pending"


def _agent_status_from_session(status: Any) -> str:
    """Map SessionStatus → wire agent_status (pending|running|waiting|completed|error)."""
    value = _session_status_value(status)
    if value in ("pending", "running", "waiting", "completed"):
        return value
    return "completed"


def _final_agent_status(
    session_status: Any,
    *,
    saw_wait: bool = False,
    saw_error: bool = False,
) -> str:
    if saw_wait or _session_status_value(session_status) == "waiting":
        return "waiting"
    if saw_error:
        return "error"
    return _agent_status_from_session(session_status)


def _events_after(events: list[Any], last_event_id: Optional[str]) -> list[Any]:
    """Return domain events strictly after last_event_id (Mongo catch-up)."""
    if not events:
        return []
    if not last_event_id:
        return list(events)
    idx = next((i for i, e in enumerate(events) if getattr(e, "id", None) == last_event_id), None)
    if idx is None:
        return list(events)
    return list(events[idx + 1 :])


def _chat_submission_id(
    request_id: Any,
    *,
    user_id: str,
    session_id: str,
) -> str:
    """Return a stable UUID for one durable WebSocket chat submission.

    Protocol-v2 clients use ``id`` to correlate control frames, but older
    clients generate short non-UUID IDs while the durable turn store requires
    UUID submission IDs. Reuse a client UUID verbatim when possible; map a
    legacy ID deterministically so retransmitting the same frame remains
    idempotent. Frames without an ID receive one fresh UUID for both ACK and
    execution.
    """
    value = str(request_id or "").strip()
    if not value:
        return str(uuid.uuid4())
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"ai-manus:chat:{user_id}:{session_id}:{value}",
            )
        )
    return str(parsed)


@router.websocket("/sessions")
async def sessions_list_ws(websocket: WebSocket):
    """User session list channel.

    Auth: Cookie (browser) or Authorization Bearer (App). No ?token=.

    Server → Client JSON:
      {"op":"snapshot","sessions":[...]}
      {"op":"upsert","session":{...}}
      {"op":"remove","session_id":"..."}
      {"op":"ping"}
    """
    if not await _enforce_ws_origin(websocket):
        return
    try:
        user = await resolve_ws_user(websocket)
    except Exception:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    agent_service = get_agent_service()

    redis = get_redis()
    await redis.initialize()
    channel = channel_for_user(user.id)
    pubsub = redis.client.pubsub()
    await pubsub.subscribe(channel)

    try:
        summaries = await agent_service.get_all_sessions(user.id)
        await websocket.send_json({
            "op": "snapshot",
            "sessions": [
                ListSessionItem.from_domain(s).model_dump(mode="json")
                for s in summaries
            ],
        })

        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=min(
                    SESSION_LIST_KEEPALIVE_SECONDS,
                    WS_AUTH_WATCHDOG_SECONDS,
                ),
            )
            if not await _enforce_ws_authorization(websocket, user.id):
                return
            if message is None:
                await websocket.send_json({"op": "ping"})
                continue
            if message.get("type") != "message":
                continue
            raw = message.get("data", "")
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="replace")
            payload = parse_notify_payload(raw)
            if not payload:
                continue
            op = payload["op"]
            session_id = payload["session_id"]
            if op == "remove":
                await websocket.send_json({"op": "remove", "session_id": session_id})
                continue
            summary = await agent_service.get_session_summary(session_id, user.id)
            if summary:
                await websocket.send_json({
                    "op": "upsert",
                    "session": ListSessionItem.from_domain(summary).model_dump(mode="json"),
                })
    except WebSocketDisconnect:
        logger.debug("Session list WS disconnected for user %s", user.id)
    except Exception:
        logger.exception("Session list WS error for user %s", user.id)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        except Exception:
            logger.debug("Failed to close session list pubsub", exc_info=True)


@router.websocket("/chat")
async def chat_ws(websocket: WebSocket):
    """Chat channel — one connection per tab; switch sessions via join/leave.

    Auth: Cookie (browser) or Authorization Bearer (App). No ?token=.
    Protocol version: client frames must include ``version: 2``.

    Client → Server (envelope):
      {
        "id": "...", "timestamp": 1710000000, "version": 2,
        "type": "join_session|leave_session|chat|stop_session",
        "session_id": "...",
        "last_event_id": "...?", "message": "...?", "attachments": [],
        "conn_id": "...?"
      }

    Server → Client:
      {"type":"joined|left|stopped","session_id":"...","request_id":"...?"}
      {"type":"ack","request_id":"...","submission_id":"...","op":"chat","session_id":"...","ok":true}
      {"type":"event","session_id":"...","event":"message|status_update|...","data":{...}}
      {"type":"stream_end","session_id":"..."}
      {"type":"error","error":"...","code":4000,"session_id":"...?","request_id":"...?"}
      {"type":"ping"}
    """
    if not await _enforce_ws_origin(websocket):
        return
    try:
        user = await resolve_ws_user(websocket)
    except Exception:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    agent_service = get_agent_service()

    joined_session_id: Optional[str] = None
    stream_task: Optional[asyncio.Task] = None
    send_lock = asyncio.Lock()

    async def safe_send(payload: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def send_error(
        error: str,
        *,
        code: int = ERR_INTERNAL,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        payload: dict[str, Any] = {"type": "error", "error": error, "code": code}
        if session_id:
            payload["session_id"] = session_id
        if request_id:
            payload["request_id"] = request_id
        await safe_send(payload)

    async def send_status_update(session_id: str, agent_status: str) -> None:
        await safe_send({
            "type": "event",
            "session_id": session_id,
            "event": "status_update",
            "data": {
                "event_id": f"status-{session_id}-{agent_status}-{int(datetime.now().timestamp())}",
                "timestamp": int(datetime.now().timestamp()),
                "agent_status": agent_status,
            },
        })

    async def send_agent_event(session_id: str, event: Any) -> None:
        stream_event = await EventMapper.event_to_stream_event(event)
        data = stream_event.data.model_dump(mode="json") if stream_event.data else {}
        await safe_send({
            "type": "event",
            "session_id": session_id,
            "event": stream_event.event,
            "data": data,
        })

    async def cancel_stream() -> None:
        nonlocal stream_task
        if stream_task and not stream_task.done():
            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Chat stream task ended with error", exc_info=True)
        stream_task = None

    async def stream_session(
        session_id: str,
        message: Optional[str] = None,
        last_event_id: Optional[str] = None,
        attachments: Optional[list[FileInfo]] = None,
        timestamp: Optional[datetime] = None,
        submission_id: Optional[str] = None,
        accepted_submission: Optional[TurnSubmission] = None,
    ) -> None:
        saw_error = False
        saw_wait = False
        try:
            async for event in agent_service.chat(
                session_id=session_id,
                user_id=user.id,
                message=message,
                timestamp=timestamp,
                event_id=last_event_id,
                attachments=attachments,
                submission_id=submission_id,
                accepted_submission=accepted_submission,
            ):
                if joined_session_id != session_id:
                    break
                if getattr(event, "type", None) == "error":
                    saw_error = True
                await send_agent_event(session_id, event)
                # Mid-stream phase: WaitEvent means Mongo is already WAITING — tell
                # clients immediately so phase UI does not depend on domain→phase fallbacks.
                if getattr(event, "type", None) == "wait":
                    saw_wait = True
                    await send_status_update(session_id, "waiting")
            if joined_session_id == session_id:
                session = await agent_service.get_session(session_id, user.id)
                final_status = _session_status_value(session.status) if session else "completed"
                # Prefer authoritative session status (e.g. waiting after message_ask_user)
                # over saw_error — early tool errors can coexist with a later WaitEvent.
                # Send status_update BEFORE stream_end: clients often clear handlers on
                # stream_end, which would drop a trailing status_update.
                if saw_wait or final_status == "waiting":
                    await send_status_update(session_id, "waiting")
                elif saw_error:
                    await send_status_update(session_id, "error")
                else:
                    await send_status_update(
                        session_id,
                        _agent_status_from_session(
                            session.status if session else "completed"
                        ),
                    )
                await safe_send({"type": "stream_end", "session_id": session_id})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Chat stream failed for session %s", session_id)
            try:
                await send_error(
                    "Chat stream failed",
                    code=ERR_INTERNAL,
                    session_id=session_id,
                )
                if joined_session_id == session_id:
                    await send_status_update(session_id, "error")
            except Exception:
                pass

    def start_stream(**kwargs: Any) -> None:
        nonlocal stream_task

        async def _run() -> None:
            nonlocal stream_task
            try:
                await stream_session(**kwargs)
            finally:
                stream_task = None

        stream_task = asyncio.create_task(_run())

    try:
        settings = get_settings()
        while True:
            try:
                raw = await asyncio.wait_for(
                    _receive_bounded_json(websocket, settings.chat_max_body_bytes),
                    timeout=CHAT_WS_PING_SECONDS,
                )
            except asyncio.TimeoutError:
                if not await _enforce_ws_authorization(websocket, user.id):
                    return
                await safe_send({"type": "ping"})
                continue
            except _WsFrameTooLargeError:
                await websocket.close(code=1009, reason="Message too large")
                return
            except ValueError:
                await send_error("Invalid JSON", code=ERR_BAD_REQUEST)
                continue

            if not isinstance(raw, dict):
                await send_error("Invalid message", code=ERR_BAD_REQUEST)
                continue

            msg_type = raw.get("type")
            session_id = raw.get("session_id")
            request_id = raw.get("id")

            if request_id is not None:
                if not isinstance(request_id, str):
                    await send_error("Invalid request id", code=ERR_BAD_REQUEST)
                    continue
                request_id = request_id.strip()
                if not request_id or len(request_id) > CHAT_WS_REQUEST_ID_MAX_CHARS:
                    await send_error("Invalid request id", code=ERR_BAD_REQUEST)
                    continue
            if session_id is not None and (
                not isinstance(session_id, str)
                or not session_id
                or len(session_id) > CHAT_WS_SESSION_ID_MAX_CHARS
            ):
                await send_error(
                    "Invalid session id",
                    code=ERR_BAD_REQUEST,
                    request_id=request_id,
                )
                continue
            conn_id = raw.get("conn_id")
            if conn_id is not None and (
                not isinstance(conn_id, str)
                or len(conn_id) > CHAT_WS_REQUEST_ID_MAX_CHARS
            ):
                await send_error(
                    "Invalid connection id",
                    code=ERR_BAD_REQUEST,
                    session_id=session_id,
                    request_id=request_id,
                )
                continue

            # leave_session: still accept missing version (tab-close races), but
            # clients should send version: 2 like other control frames.
            if msg_type != "leave_session" and raw.get("version") != CHAT_WS_PROTOCOL_VERSION:
                await send_error(
                    f"Unsupported protocol version (require {CHAT_WS_PROTOCOL_VERSION})",
                    code=ERR_BAD_VERSION,
                    session_id=session_id,
                    request_id=request_id,
                )
                continue

            # A long-lived socket must not outlive its opaque Redis session.
            # Re-check every client command (including joins that can disclose
            # history), while the keepalive path above bounds idle revocation
            # latency.
            if not await _enforce_ws_authorization(websocket, user.id):
                return

            if msg_type == "join_session":
                if not session_id:
                    await send_error(
                        "session_id required",
                        code=ERR_BAD_REQUEST,
                        request_id=request_id,
                    )
                    continue
                last_event_id = raw.get("last_event_id")
                if last_event_id is not None and (
                    not isinstance(last_event_id, str)
                    or len(last_event_id) > CHAT_WS_EVENT_ID_MAX_CHARS
                ):
                    await send_error(
                        "Invalid event id",
                        code=ERR_BAD_REQUEST,
                        session_id=session_id,
                        request_id=request_id,
                    )
                    continue
                session = await agent_service.get_session(session_id, user.id)
                if not session:
                    await send_error(
                        "Session not found",
                        code=ERR_NOT_FOUND,
                        session_id=session_id,
                        request_id=request_id,
                    )
                    continue

                if joined_session_id and joined_session_id != session_id:
                    await cancel_stream()
                    prev = joined_session_id
                    joined_session_id = None
                    await safe_send({"type": "left", "session_id": prev})

                joined_session_id = session_id
                joined_payload: dict[str, Any] = {
                    "type": "joined",
                    "session_id": session_id,
                }
                if request_id:
                    joined_payload["request_id"] = request_id
                await safe_send(joined_payload)

                status = _session_status_value(session.status)
                agent_status = _agent_status_from_session(session.status)

                # Idle/pending/completed/waiting: Mongo catch-up after cursor,
                # then authoritative status_update. Do NOT send stream_end —
                # a follow-up chat would otherwise clear client "thinking".
                if status == "running":
                    await send_status_update(session_id, "running")
                    await cancel_stream()
                    start_stream(
                        session_id=session_id,
                        message=None,
                        last_event_id=last_event_id,
                    )
                else:
                    if last_event_id:
                        for event in _events_after(session.events or [], last_event_id):
                            if joined_session_id != session_id:
                                break
                            await send_agent_event(session_id, event)
                    await send_status_update(session_id, agent_status)

            elif msg_type == "leave_session":
                target = session_id or joined_session_id
                if not target:
                    continue
                if joined_session_id == target:
                    await cancel_stream()
                    joined_session_id = None
                    left_payload: dict[str, Any] = {
                        "type": "left",
                        "session_id": target,
                    }
                    if request_id:
                        left_payload["request_id"] = request_id
                    await safe_send(left_payload)

            elif msg_type == "chat":
                if not session_id:
                    await send_error(
                        "session_id required",
                        code=ERR_BAD_REQUEST,
                        request_id=request_id,
                    )
                    continue
                if joined_session_id != session_id:
                    await send_error(
                        "Not joined to this session",
                        code=ERR_NOT_JOINED,
                        session_id=session_id,
                        request_id=request_id,
                    )
                    continue

                submission_id = _chat_submission_id(
                    request_id,
                    user_id=user.id,
                    session_id=session_id,
                )
                try:
                    chat_request = ChatRequest(
                        timestamp=raw.get("timestamp"),
                        message=raw.get("message"),
                        attachments=raw.get("attachments"),
                        required_skills=raw.get("required_skills"),
                        event_id=raw.get("last_event_id") or raw.get("event_id"),
                        submission_id=submission_id,
                    )
                    timestamp = (
                        datetime.fromtimestamp(chat_request.timestamp)
                        if chat_request.timestamp is not None
                        else None
                    )
                except (PydanticValidationError, ValueError, OSError, OverflowError):
                    await send_error(
                        "Invalid chat request",
                        code=ERR_BAD_REQUEST,
                        session_id=session_id,
                        request_id=request_id,
                    )
                    continue

                attachments = [
                    attachment.to_domain()
                    for attachment in (chat_request.attachments or [])
                ]
                try:
                    accepted_submission = await agent_service.accept_chat_submission(
                        session_id=session_id,
                        user_id=user.id,
                        submission_id=submission_id,
                        message=chat_request.message or "",
                        timestamp=timestamp,
                        attachments=attachments or None,
                        required_skills=chat_request.required_skills,
                    )
                except BadRequestError:
                    await send_error(
                        "Invalid chat request",
                        code=ERR_BAD_REQUEST,
                        session_id=session_id,
                        request_id=request_id,
                    )
                    continue
                except ConflictError:
                    await send_error(
                        "Chat submission conflicts with an existing request",
                        code=409,
                        session_id=session_id,
                        request_id=request_id,
                    )
                    continue
                except TooManyRequestsError:
                    await send_error(
                        "Too many active chat submissions",
                        code=429,
                        session_id=session_id,
                        request_id=request_id,
                    )
                    continue
                except ServiceUnavailableError:
                    await send_error(
                        "Chat submission is temporarily unavailable",
                        code=503,
                        session_id=session_id,
                        request_id=request_id,
                    )
                    continue
                except Exception:
                    logger.exception(
                        "Failed to durably accept chat submission for session %s",
                        session_id,
                    )
                    await send_error(
                        "Chat submission could not be accepted",
                        code=ERR_INTERNAL,
                        session_id=session_id,
                        request_id=request_id,
                    )
                    continue

                # Preserve the protocol correlation ID for existing clients.
                # If none was supplied, the generated durable UUID serves both
                # purposes. ``submission_id`` lets clients persist the durable
                # retry key even when their legacy request ID is not a UUID.
                ack_request_id = str(request_id or "").strip() or submission_id
                await safe_send({
                    "type": "ack",
                    "request_id": ack_request_id,
                    "submission_id": submission_id,
                    "op": "chat",
                    "session_id": session_id,
                    "ok": True,
                })
                await send_status_update(session_id, "running")
                await cancel_stream()
                start_stream(
                    session_id=session_id,
                    message=chat_request.message or None,
                    last_event_id=chat_request.event_id,
                    attachments=attachments or None,
                    timestamp=timestamp,
                    submission_id=submission_id,
                    accepted_submission=accepted_submission,
                )

            elif msg_type == "stop_session":
                if not session_id:
                    await send_error(
                        "session_id required",
                        code=ERR_BAD_REQUEST,
                        request_id=request_id,
                    )
                    continue
                try:
                    await agent_service.stop_session(session_id, user.id)
                    stopped_payload: dict[str, Any] = {
                        "type": "stopped",
                        "session_id": session_id,
                    }
                    if request_id:
                        stopped_payload["request_id"] = request_id
                    await safe_send(stopped_payload)
                    await send_status_update(session_id, "completed")
                except Exception:
                    logger.exception("Failed to stop chat session %s", session_id)
                    await send_error(
                        "Session could not be stopped",
                        code=ERR_INTERNAL,
                        session_id=session_id,
                        request_id=request_id,
                    )
            else:
                await send_error(
                    f"Unknown type: {msg_type}",
                    code=ERR_BAD_REQUEST,
                    request_id=request_id,
                )

    except WebSocketDisconnect:
        logger.debug("Chat WS disconnected for user %s", user.id)
    except Exception:
        logger.exception("Chat WS error for user %s", user.id)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        await cancel_stream()

@router.websocket("/vnc/{session_id}")
async def vnc_ws(websocket: WebSocket, session_id: str):
    """Sandbox VNC proxy (binary) — Cookie / Bearer auth, no signed URL.

    Client should negotiate subprotocol ``binary`` (NoVNC default).
    """
    if not await _enforce_ws_origin(websocket):
        return
    try:
        user = await resolve_ws_user(websocket)
    except Exception:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    agent_service = get_agent_service()
    session = await agent_service.get_session(session_id, user.id)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return

    await websocket.accept(subprotocol="binary")
    logger.info("Accepted VNC WS for session %s user %s", session_id, user.id)

    try:
        sandbox_ws_url = await agent_service.get_vnc_url(session_id)
        logger.info("Connecting to sandbox VNC for session %s", session_id)

        async with websockets.connect(sandbox_ws_url) as sandbox_ws:
            async def forward_to_sandbox() -> None:
                try:
                    while True:
                        data = await websocket.receive_bytes()
                        await sandbox_ws.send(data)
                except WebSocketDisconnect:
                    logger.info("Web -> VNC connection closed")
                except Exception as exc:
                    logger.error(
                        "Error forwarding data to sandbox: %s",
                        safe_exception_summary(exc),
                    )

            async def forward_from_sandbox() -> None:
                try:
                    while True:
                        data = await sandbox_ws.recv()
                        await websocket.send_bytes(data)
                except websockets.exceptions.ConnectionClosed:
                    logger.info("VNC -> Web connection closed")
                except Exception as exc:
                    logger.error(
                        "Error forwarding data from sandbox: %s",
                        safe_exception_summary(exc),
                    )

            forward_task1 = asyncio.create_task(forward_to_sandbox())
            forward_task2 = asyncio.create_task(forward_from_sandbox())
            auth_task = asyncio.create_task(
                _ws_auth_watchdog(websocket, user.id)
            )
            proxy_tasks = [forward_task1, forward_task2, auth_task]
            try:
                await asyncio.wait(
                    proxy_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                pending = [task for task in proxy_tasks if not task.done()]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
    except ConnectionError as exc:
        logger.error(
            "Unable to connect to sandbox environment: %s",
            safe_exception_summary(exc),
        )
        try:
            await websocket.close(
                code=1011,
                reason="Unable to connect to sandbox environment",
            )
        except Exception:
            pass
    except Exception as exc:
        logger.error(
            "VNC WebSocket error: %s", safe_exception_summary(exc)
        )
        try:
            await websocket.close(code=1011, reason="WebSocket proxy error")
        except Exception:
            pass
