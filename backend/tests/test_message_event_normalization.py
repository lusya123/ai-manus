from app.domain.models.event import MessageEvent
from app.domain.utils.model_output import extract_model_thinking_text


class ContentBlock:
    def __init__(self, type: str, text: str):
        self.type = type
        self.text = text


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


def test_message_event_strips_plain_thinking_tags():
    event = MessageEvent(
        message="<think>private reasoning</think>\n\nvisible answer"
    )

    assert event.message == "visible answer"


def test_message_event_strips_split_like_open_thinking_tag():
    event = MessageEvent(message="visible prefix <thi")

    assert event.message == "visible prefix"


def test_message_event_extracts_visible_content_from_reasoning_json_string():
    event = MessageEvent(
        message='{"reasoning_content":"private","content":"visible answer"}'
    )

    assert event.message == "visible answer"


def test_message_event_preserves_user_thinking_tag_text():
    event = MessageEvent(
        role="user",
        message="请解释 <think> 标签是什么意思",
    )

    assert event.message == "请解释 <think> 标签是什么意思"


def test_message_event_normalizes_object_content_blocks():
    event = MessageEvent(
        message=[
            ContentBlock("thinking", "private reasoning"),
            ContentBlock("text", "visible answer"),
        ]
    )

    assert event.message == "visible answer"


def test_extract_model_thinking_text_from_split_ready_tag():
    assert extract_model_thinking_text("<think>private reasoning") == "private reasoning"
    assert (
        extract_model_thinking_text("<think>private reasoning</think>visible answer")
        == "private reasoning"
    )


def test_extract_model_thinking_text_from_reasoning_fence():
    assert (
        extract_model_thinking_text("```reasoning\nprivate reasoning\n```visible")
        == "private reasoning"
    )
