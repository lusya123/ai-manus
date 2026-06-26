from app.domain.models.event import MessageEvent


def test_message_event_ignores_thinking_blocks():
    event = MessageEvent(
        message=[
            {"type": "thinking", "thinking": "private reasoning"},
            {"type": "reasoning", "text": "private reasoning text"},
            {"type": "text", "text": "visible answer"},
        ]
    )

    assert event.message == "visible answer"


def test_message_event_ignores_tool_use_blocks():
    event = MessageEvent(
        message=[
            {"type": "tool_use", "text": "internal tool payload"},
            {"type": "text", "text": "final answer"},
        ]
    )

    assert event.message == "final answer"


def test_message_event_ignores_unknown_typed_blocks_and_keeps_nested_text():
    event = MessageEvent(
        message=[
            {"type": "server_tool_use", "text": "internal payload"},
            {"content": [{"type": "text", "text": "nested answer"}]},
        ]
    )

    assert event.message == "nested answer"
