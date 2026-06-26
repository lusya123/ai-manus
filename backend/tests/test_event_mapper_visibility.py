from app.domain.models.event import ToolEvent, ToolStatus
from app.interfaces.schemas.event import EventMapper


async def test_event_mapper_filters_message_notify_user_tool_events():
    event = ToolEvent(
        tool_call_id="tool-1",
        tool_name="message",
        function_name="message_notify_user",
        function_args={"text": "internal progress text"},
        status=ToolStatus.CALLING,
    )

    assert await EventMapper.event_to_sse_event(event) is None
    assert await EventMapper.events_to_sse_events([event]) == []
