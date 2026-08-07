from app.application.services.claw_service import ClawService
from app.domain.models.claw import ClawMessage
from app.domain.services.claw_domain_service import ClawDomainService


class FakeEventBus:
    def __init__(self):
        self.events = []

    async def publish(self, user_id, event):
        self.events.append((user_id, event))


class FakeDomain:
    claw_repository = object()

    async def process_chat_stream(self, user_id, base_url, message, session_id):
        yield {"type": "text", "content": "<thi"}
        yield {"type": "text", "content": "nk>private"}
        yield {"type": "text", "content": "</think>visible"}
        yield {"type": "text", "content": " answer"}
        yield {"type": "done", "stop_reason": "end_turn"}


async def test_claw_stream_discards_private_thinking_and_sends_visible_text():
    service = ClawService(FakeDomain())
    event_bus = FakeEventBus()
    service.event_bus = event_bus

    await service._process_chat("user-1", "http://claw", "hello", "default")

    assert [event for _, event in event_bus.events] == [
        {"type": "text", "content": "visible"},
        {"type": "text", "content": " answer"},
        {"type": "done", "stop_reason": "end_turn"},
    ]


def test_claw_history_sanitizes_stored_thinking_messages():
    messages = [
        ClawMessage(role="assistant", content="<think>private</think>visible", timestamp=1),
        ClawMessage(role="assistant", content="<think>private only</think>", timestamp=2),
        ClawMessage(role="user", content="请解释 <think> 标签", timestamp=3),
    ]

    sanitized = [
        ClawDomainService._sanitize_message(message)
        for message in messages
    ]
    visible = [
        message for message in sanitized
        if message.role == "attachments" or message.content
    ]

    assert [message.content for message in visible] == [
        "visible",
        "请解释 <think> 标签",
    ]
