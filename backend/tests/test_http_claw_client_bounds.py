import pytest

from app.domain.external.claw import ClawResponseTooLargeError
from app.infrastructure.external.claw.http_claw_client import HttpClawClient


class _ByteResponse:
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def aiter_bytes(self, chunk_size: int):
        for chunk in self.chunks:
            for offset in range(0, len(chunk), chunk_size):
                yield chunk[offset:offset + chunk_size]


async def test_raw_sse_event_is_bounded_before_json_parse(monkeypatch):
    monkeypatch.setattr(
        "app.infrastructure.external.claw.http_claw_client.json.loads",
        lambda payload: (_ for _ in ()).throw(
            AssertionError("oversized raw input must not reach json.loads")
        ),
    )
    response = _ByteResponse([b"data:" + b"x" * 100])

    with pytest.raises(ClawResponseTooLargeError, match="event exceeded"):
        async for _ in HttpClawClient._iter_bounded_sse_events(
            response,
            event_limit=16,
            stream_limit=1024,
        ):
            pass


async def test_raw_sse_stream_counts_non_event_bytes():
    response = _ByteResponse([b": keepalive\n" * 10])

    with pytest.raises(ClawResponseTooLargeError, match="stream exceeded"):
        async for _ in HttpClawClient._iter_bounded_sse_events(
            response,
            event_limit=64,
            stream_limit=40,
        ):
            pass


async def test_bounded_sse_parser_handles_json_split_across_reads():
    response = _ByteResponse(
        [b'data: {"type":"te', b'xt","content":"ok"}\r\n']
    )

    events = [
        event
        async for event in HttpClawClient._iter_bounded_sse_events(
            response,
            event_limit=128,
            stream_limit=256,
        )
    ]

    assert events == [{"type": "text", "content": "ok"}]
