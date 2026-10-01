"""Visible response streaming; partial tool arguments are never public events."""
import asyncio
import contextlib
import json
import re
from dataclasses import dataclass
from typing import Optional
from app.domain.models.message import LLMMessage
from app.domain.utils.model_output import sanitize_model_text, normalize_model_content

@dataclass
class LLMStreamChunk:
    text: str = ""
    message: Optional[LLMMessage] = None
    reset: bool = False


def visible_stream_text(text: str) -> str:
    # Hold incomplete wrappers until we can classify them. Otherwise a split
    # <thinking>, token marker or fenced reasoning label briefly leaks text.
    text = re.sub(r"<[^>]*$", "", text)
    last_fence = text.rfind("```")
    if last_fence >= 0 and "\n" not in text[last_fence:]:
        text = text[:last_fence]
    text = re.sub(r"`{1,3}(?:[a-zA-Z_]*)$", "", text)
    text = sanitize_model_text(text)
    if text.startswith(("{", "[")):
        try:
            return normalize_model_content(json.loads(text))
        except ValueError:
            return ""
    return text


def partial_message_argument(arguments: str) -> str:
    """Decode only the top-level message string from an incomplete JSON object."""
    decoder = json.JSONDecoder()
    pos = 0
    source = arguments.lstrip()
    if not source.startswith("{"):
        return ""
    pos = 1
    try:
        while pos < len(source):
            while pos < len(source) and source[pos] in " \r\n\t,":
                pos += 1
            key, end = decoder.raw_decode(source, pos)
            pos = end
            while pos < len(source) and source[pos].isspace():
                pos += 1
            if source[pos] != ":":
                return ""
            pos += 1
            while pos < len(source) and source[pos].isspace():
                pos += 1
            if key != "message":
                _, pos = decoder.raw_decode(source, pos)
                continue
            if source[pos] != '"':
                return ""
            start = pos
            pos += 1
            escaped = False
            while pos < len(source):
                char = source[pos]
                if char == '"' and not escaped:
                    return visible_stream_text(json.loads(source[start:pos + 1]))
                if char == "\\" and not escaped:
                    escaped = True
                else:
                    escaped = False
                pos += 1
            body = source[start:]
            # Remove an unfinished escape, including partial unicode/surrogate.
            body = re.sub(r"\\(?:u[0-9a-fA-F]{0,4})?$", "", body)
            try:
                result = json.loads(body + '"')
                result = result.encode("utf-8", "ignore").decode("utf-8")
                return visible_stream_text(result)
            except ValueError:
                return ""
    except (ValueError, IndexError, TypeError):
        return ""
    return ""


async def merge_stream_events(events, queue):
    """Multiplex an agent iterator with previews, closing both on cancellation."""
    iterator = events.__aiter__()
    next_event = asyncio.create_task(anext(iterator))
    next_preview = asyncio.create_task(queue.get())
    try:
        while True:
            ready, _ = await asyncio.wait({next_event, next_preview}, return_when=asyncio.FIRST_COMPLETED)
            if next_preview in ready:
                yield next_preview.result()
                next_preview = asyncio.create_task(queue.get())
            if next_event in ready:
                # Ensure queued deltas precede their canonical final event.
                if next_preview.done():
                    yield next_preview.result()
                    next_preview = asyncio.create_task(queue.get())
                while not queue.empty():
                    yield queue.get_nowait()
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    break
                yield event
                next_event = asyncio.create_task(anext(iterator))
    finally:
        for task in (next_event, next_preview):
            task.cancel()
        for task in (next_event, next_preview):
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await task
        await iterator.aclose()
