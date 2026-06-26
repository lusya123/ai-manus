"""
OpenAI-compatible API proxy for manus-claw.
All LLM requests from OpenClaw containers go through this endpoint,
authenticated using per-user API keys.
"""
import logging
import json
import time
from typing import Optional, AsyncIterator, Any
from fastapi import APIRouter, Request, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse
import httpx

from app.application.services.claw_service import ClawService
from app.core.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["openai-proxy"])


def _extract_bearer_token(request: Request) -> Optional[str]:
    """Extract Bearer token from Authorization header"""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


async def _get_claw_service() -> ClawService:
    from app.interfaces.dependencies import get_claw_service
    return get_claw_service()


def _openai_chat_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _anthropic_messages_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif "content" in item:
                    parts.append(str(item.get("content") or ""))
        return "\n".join(part for part in parts if part)
    return "" if content is None else str(content)


def _openai_content_to_anthropic(content: Any) -> str | list[dict]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)

    blocks: list[dict] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            blocks.append({"type": "text", "text": str(item.get("text") or "")})
        elif item_type == "image_url":
            image_url = item.get("image_url") or {}
            url = image_url.get("url") if isinstance(image_url, dict) else image_url
            blocks.append({"type": "text", "text": f"[image: {url}]"})
    return blocks or ""


def _openai_tools_to_anthropic(tools: Any) -> list[dict] | None:
    if not isinstance(tools, list):
        return None
    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function, dict) or not function.get("name"):
            continue
        converted.append({
            "name": function["name"],
            "description": function.get("description") or "",
            "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
        })
    return converted or None


def _openai_messages_to_anthropic(messages: Any) -> tuple[list[dict], str | None]:
    anthropic_messages: list[dict] = []
    system_parts: list[str] = []

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")

        if role == "system":
            text = _text_from_content(content)
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            anthropic_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id") or "tool_result",
                    "content": _text_from_content(content),
                }],
            })
            continue

        if role not in ("user", "assistant"):
            role = "user"

        if role == "assistant" and message.get("tool_calls"):
            blocks = []
            text = _text_from_content(content)
            if text:
                blocks.append({"type": "text", "text": text})
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                raw_arguments = function.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                except json.JSONDecodeError:
                    arguments = {"input": raw_arguments}
                blocks.append({
                    "type": "tool_use",
                    "id": tool_call.get("id") or function.get("name") or "tool_use",
                    "name": function.get("name") or "tool",
                    "input": arguments,
                })
            anthropic_messages.append({"role": "assistant", "content": blocks or ""})
            continue

        anthropic_messages.append({
            "role": role,
            "content": _openai_content_to_anthropic(content),
        })

    return anthropic_messages, "\n\n".join(system_parts) or None


def _openai_to_anthropic_request(request_body: dict, settings) -> dict:
    messages, system_prompt = _openai_messages_to_anthropic(request_body.get("messages") or [])
    body: dict[str, Any] = {
        "model": request_body.get("model") or settings.model_name,
        "max_tokens": request_body.get("max_tokens") or settings.max_tokens,
        "messages": messages,
    }
    if system_prompt:
        body["system"] = system_prompt
    if "temperature" in request_body:
        body["temperature"] = request_body["temperature"]
    if request_body.get("stream"):
        body["stream"] = True
    tools = _openai_tools_to_anthropic(request_body.get("tools"))
    if tools:
        body["tools"] = tools
    return body


def _anthropic_headers(settings) -> dict:
    return {
        "Content-Type": "application/json",
        "x-api-key": settings.api_key or "",
        "anthropic-version": "2023-06-01",
        **(settings.extra_headers or {}),
    }


def _openai_headers(settings) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.api_key}",
        **(settings.extra_headers or {}),
    }


def _anthropic_stop_reason(stop_reason: str | None) -> str | None:
    if stop_reason == "end_turn":
        return "stop"
    if stop_reason == "max_tokens":
        return "length"
    if stop_reason == "tool_use":
        return "tool_calls"
    return stop_reason


def _anthropic_to_openai_response(result: dict, model: str) -> dict:
    content = result.get("content") or []
    text_parts: list[str] = []
    tool_calls = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(block.get("text") or "")
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id"),
                "type": "function",
                "function": {
                    "name": block.get("name"),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                },
            })

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage = result.get("usage") or {}
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    return {
        "id": result.get("id"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.get("model") or model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": _anthropic_stop_reason(result.get("stop_reason")),
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _sse(data: dict | str) -> bytes:
    if data == "[DONE]":
        return b"data: [DONE]\n\n"
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _openai_stream_chunk(
    completion_id: str | None,
    model: str | None,
    delta: dict,
    finish_reason: str | None = None,
) -> dict:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }


def _anthropic_stream_event_to_openai_chunks(event: dict, state: dict) -> list[dict | str]:
    """Translate one Anthropic SSE event into OpenAI-compatible stream chunks."""
    event_type = event.get("type")
    if event_type == "message_start":
        message = event.get("message") or {}
        state["completion_id"] = message.get("id")
        state["model"] = message.get("model") or state.get("model")
        return [_openai_stream_chunk(
            state.get("completion_id"),
            state.get("model"),
            {"role": "assistant"},
        )]

    if event_type == "content_block_start":
        block = event.get("content_block") or {}
        if block.get("type") != "tool_use":
            return []
        block_index = event.get("index")
        tool_index = state.setdefault("next_tool_index", 0)
        state["next_tool_index"] = tool_index + 1
        state.setdefault("tool_blocks", {})[block_index] = tool_index
        return [_openai_stream_chunk(
            state.get("completion_id"),
            state.get("model"),
            {
                "tool_calls": [{
                    "index": tool_index,
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": "",
                    },
                }],
            },
        )]

    if event_type == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            return [_openai_stream_chunk(
                state.get("completion_id"),
                state.get("model"),
                {"content": delta["text"]},
            )]
        if delta.get("type") == "input_json_delta":
            block_index = event.get("index")
            tool_index = state.setdefault("tool_blocks", {}).get(block_index)
            if tool_index is None:
                tool_index = state.setdefault("next_tool_index", 0)
                state["next_tool_index"] = tool_index + 1
                state["tool_blocks"][block_index] = tool_index
            return [_openai_stream_chunk(
                state.get("completion_id"),
                state.get("model"),
                {
                    "tool_calls": [{
                        "index": tool_index,
                        "function": {
                            "arguments": delta.get("partial_json") or "",
                        },
                    }],
                },
            )]
        return []

    if event_type == "message_delta":
        delta = event.get("delta") or {}
        stop_reason = _anthropic_stop_reason(delta.get("stop_reason"))
        if stop_reason:
            return [_openai_stream_chunk(
                state.get("completion_id"),
                state.get("model"),
                {},
                stop_reason,
            )]
        return []

    if event_type == "message_stop":
        return ["[DONE]"]

    return []


async def _stream_llm_response(
    request_body: dict,
    settings,
) -> AsyncIterator[bytes]:
    """Stream LLM response from the configured backend"""
    api_base = settings.api_base or "https://api.openai.com"

    is_anthropic = settings.model_provider == "anthropic"
    headers = _anthropic_headers(settings) if is_anthropic else _openai_headers(settings)
    target_url = _anthropic_messages_url(api_base) if is_anthropic else _openai_chat_url(api_base)
    outgoing_body = _openai_to_anthropic_request(request_body, settings) if is_anthropic else request_body

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            target_url,
            json=outgoing_body,
            headers=headers,
        ) as resp:
            if not resp.is_success:
                error_body = await resp.aread()
                error_msg = error_body.decode("utf-8", errors="replace")
                sse_error = (
                    f'data: {json.dumps({"error": {"message": f"LLM backend error: {error_msg}", "type": "api_error"}})}\n\n'
                    f"data: [DONE]\n\n"
                )
                yield sse_error.encode("utf-8")
                return

            if is_anthropic:
                state: dict[str, Any] = {
                    "completion_id": None,
                    "model": outgoing_body.get("model"),
                    "tool_blocks": {},
                    "next_tool_index": 0,
                }
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line.removeprefix("data:").strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    for chunk in _anthropic_stream_event_to_openai_chunks(event, state):
                        yield _sse(chunk)
                        if chunk == "[DONE]":
                            return
                yield _sse("[DONE]")
                return

            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk


async def _get_llm_response(
    request_body: dict,
    settings,
) -> dict:
    """Get non-streaming LLM response"""
    api_base = settings.api_base or "https://api.openai.com"
    is_anthropic = settings.model_provider == "anthropic"
    headers = _anthropic_headers(settings) if is_anthropic else _openai_headers(settings)
    target_url = _anthropic_messages_url(api_base) if is_anthropic else _openai_chat_url(api_base)
    outgoing_body = _openai_to_anthropic_request(request_body, settings) if is_anthropic else request_body

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(target_url, json=outgoing_body, headers=headers)
        resp.raise_for_status()
        result = resp.json()
        if is_anthropic:
            return _anthropic_to_openai_response(result, outgoing_body.get("model"))
        return result


def _openai_error_response(status_code: int, message: str, error_type: str) -> JSONResponse:
    """Return an OpenAI-compatible error JSON response directly, bypassing the global handler."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type}},
    )


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI-compatible chat completions proxy.
    Authenticates using per-user manus API keys and forwards to the configured LLM backend.
    """
    api_key = _extract_bearer_token(request)
    if not api_key:
        return _openai_error_response(status.HTTP_401_UNAUTHORIZED, "Missing API key", "auth_error")

    # Verify API key and get user
    claw_service = await _get_claw_service()
    user_id = await claw_service.verify_api_key(api_key)
    if not user_id:
        return _openai_error_response(status.HTTP_401_UNAUTHORIZED, "Invalid API key", "auth_error")

    try:
        body = await request.json()
    except Exception:
        return _openai_error_response(status.HTTP_400_BAD_REQUEST, "Invalid request body", "invalid_request_error")

    settings = get_settings()

    # Override model with configured model name
    if settings.model_name and body.get("model") in ("default", "manus-proxy/default", None):
        body = {**body, "model": settings.model_name}

    is_stream = body.get("stream", False)

    logger.info(f"[openai-proxy] user={user_id} model={body.get('model')} stream={is_stream}")

    try:
        if is_stream:
            return StreamingResponse(
                _stream_llm_response(body, settings),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            result = await _get_llm_response(body, settings)
            return JSONResponse(content=result)

    except httpx.HTTPStatusError as e:
        logger.error(f"[openai-proxy] LLM backend error: {e.response.status_code} {e.response.text}")
        return _openai_error_response(e.response.status_code, f"LLM backend error: {e.response.text}", "api_error")
    except Exception as e:
        logger.error(f"[openai-proxy] Unexpected error: {str(e)}")
        return _openai_error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e), "api_error")
