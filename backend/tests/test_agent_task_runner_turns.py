import io
from types import SimpleNamespace

from pydantic import TypeAdapter

from app.domain.models.event import (
    AgentEvent,
    DoneEvent,
    MessageEvent,
    TitleEvent,
)
from app.domain.models.file import FileInfo
from app.domain.models.session import SessionStatus
from app.domain.services.agent_task_runner import AgentTaskRunner


async def test_browser_screenshot_bytes_are_wrapped_for_file_storage():
    class Browser:
        async def screenshot(self):
            return b"\x89PNG\r\n\x1a\nimage"

    class FileStorage:
        async def upload_file(
            self,
            stream,
            filename,
            user_id,
            content_type=None,
            metadata=None,
        ):
            assert isinstance(stream, io.BytesIO)
            assert stream.read() == b"\x89PNG\r\n\x1a\nimage"
            assert filename == "screenshot.png"
            assert user_id == "owner"
            assert content_type == "image/png"
            assert metadata == {
                "manus_auto_artifact_session_id": "session-1",
                "manus_auto_artifact_kind": "browser_screenshot",
            }
            return FileInfo(
                file_id="screenshot-file",
                filename="screenshot.png",
                user_id="owner",
                content_type="image/png",
            )

    class SessionRepository:
        def __init__(self):
            self.operations = []

        async def remove_file(self, session_id, file_id):
            self.operations.append(("remove", session_id, file_id))

        async def add_file(self, session_id, file_info):
            self.operations.append(("add", session_id, file_info))

    runner = object.__new__(AgentTaskRunner)
    runner._browser = Browser()
    runner._file_storage = FileStorage()
    runner._user_id = "owner"
    runner._session_id = "session-1"
    runner._session_repository = SessionRepository()

    assert await runner._get_browser_screenshot() == "screenshot-file"
    assert runner._session_repository.operations == [
        (
            "add",
            "session-1",
            FileInfo(
                file_id="screenshot-file",
                filename="screenshot.png",
                user_id="owner",
                content_type="image/png",
            ),
        ),
    ]


async def test_runner_finishes_current_turn_before_processing_queued_message():
    """A queued message must not preempt the flow already being streamed."""

    class InputStream:
        def __init__(self):
            self.items = [
                ("1-0", MessageEvent(role="user", message="first").model_dump_json()),
                ("2-0", MessageEvent(role="user", message="second").model_dump_json()),
            ]

        async def is_empty(self):
            return not self.items

        async def pop(self):
            return self.items.pop(0)

    class OutputStream:
        def __init__(self):
            self.items = []

        async def put(self, value):
            event_id = f"{len(self.items) + 1}-0"
            self.items.append((event_id, value))
            return event_id

    class Repository:
        def __init__(self):
            self.events = []
            self.statuses = []

        async def add_event(self, session_id, event):
            self.events.append(event.model_copy(deep=True))

        async def update_title(self, session_id, title):
            return None

        async def update_latest_message(self, session_id, message, timestamp):
            return None

        async def increment_unread_message_count(self, session_id):
            return None

        async def update_status(self, session_id, status):
            self.statuses.append(status)

    class Sandbox:
        async def ensure_sandbox(self):
            return None

    class MCPTool:
        async def initialized(self, config):
            return None

    class MCPRepository:
        async def get_mcp_config(self):
            return {}

    input_stream = InputStream()
    output_stream = OutputStream()
    task = SimpleNamespace(
        input_stream=input_stream,
        output_stream=output_stream,
    )
    repository = Repository()
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._sandbox = Sandbox()
    runner._mcp_tool = MCPTool()
    runner._mcp_repository = MCPRepository()
    runner._session_repository = repository
    runner._turn_submission_repository = None

    async def sync_attachments(event):
        event.attachments = []

    async def run_flow(message):
        yield TitleEvent(title=f"title:{message.message}")
        yield MessageEvent(message=f"answer:{message.message}")
        yield DoneEvent()

    runner._sync_message_attachments_to_sandbox = sync_attachments
    runner._run_flow = run_flow

    await runner.run(task)

    events = [
        TypeAdapter(AgentEvent).validate_json(value)
        for _, value in output_stream.items
    ]
    assert [event.type for event in events] == [
        "title",
        "message",
        "done",
        "title",
        "message",
        "done",
    ]
    assert [event.turn_id for event in events[:3]] == ["1-0"] * 3
    assert [event.turn_id for event in events[3:]] == ["2-0"] * 3
    assert [
        event.message for event in events if isinstance(event, MessageEvent)
    ] == ["answer:first", "answer:second"]
    assert [event.turn_id for event in repository.events] == [
        "1-0",
        "1-0",
        "1-0",
        "2-0",
        "2-0",
        "2-0",
    ]
    assert repository.statuses[-1] == SessionStatus.COMPLETED
