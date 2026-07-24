from datetime import UTC, datetime

from app.domain.models.event import (
    BrowserToolContent,
    FileToolContent,
    McpToolContent,
    MessageEvent,
    PlanEvent,
    PlanStatus,
    PreviewToolContent,
    SearchToolContent,
    ShellToolContent,
    StepEvent,
    StepStatus,
    ToolEvent,
    ToolStatus,
)
from app.domain.models.file import FileInfo
from app.domain.models.plan import Plan, Step
from app.domain.models.search import SearchResultItem
from app.interfaces.schemas.event import BaseEventData, EventMapper


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


def test_persisted_naive_mongo_event_timestamp_is_interpreted_as_utc():
    event = MessageEvent(
        message="persisted",
        timestamp=datetime(2026, 7, 16, 18, 30, 0),
    )

    mapped = BaseEventData.base_event_data(event)

    assert mapped["timestamp"] == int(
        datetime(2026, 7, 16, 18, 30, 0, tzinfo=UTC).timestamp()
    )


async def test_event_mapper_hides_plan_events_but_preserves_all_message_events():
    step = Step(
        id="1",
        description="Internal execution step",
        result="internal step result",
    )
    plan = Plan(
        message="internal planner message",
        steps=[step],
    )
    events = [
        MessageEvent(role="user", message="hello"),
        PlanEvent(status=PlanStatus.CREATED, plan=plan),
        StepEvent(status=StepStatus.STARTED, step=step),
        # A final answer is allowed to equal Plan.message. Content equality
        # cannot prove that a MessageEvent is internal, and filtering on it
        # made the live answer disappear after a page refresh.
        MessageEvent(message="internal planner message"),
        MessageEvent(message="internal step result"),
        MessageEvent(message="visible final answer"),
    ]

    mapped = await EventMapper.events_to_sse_events(events)

    assert [event.event for event in mapped] == ["message", "message", "message", "message"]
    assert mapped[0].data.content == "hello"
    assert mapped[1].data.content == "internal planner message"
    assert mapped[2].data.content == "internal step result"
    assert mapped[3].data.content == "visible final answer"


async def test_public_event_mapper_uses_share_scoped_attachment_urls_only():
    private_file = FileInfo(
        file_id="file-1",
        filename="private.txt",
        file_path="/home/ubuntu/private.txt",
        content_type="text/plain",
        user_id="owner-secret",
        metadata={"internal": "do-not-publish"},
    )

    class FileService:
        async def create_shared_session_signed_url(
            self, session_id, file_id, share_epoch
        ):
            return (
                f"/api/v1/sessions/{session_id}/share/files/{file_id}"
                f"?share_epoch={share_epoch}&signature=signed"
            )

    mapped = await EventMapper.events_to_shared_sse_events(
        [
            MessageEvent(
                role="assistant",
                message="download",
                attachments=[private_file],
            )
        ],
        session_id="session-1",
        share_epoch="epoch-1",
        shared_files={private_file.file_id: private_file},
        file_service=FileService(),
    )

    attachment = mapped[0].model_dump(exclude_none=True)["data"]["attachments"][0]
    assert attachment["file_url"].startswith(
        "/api/v1/sessions/session-1/share/files/file-1?share_epoch=epoch-1"
    )
    assert "metadata" not in attachment
    assert "user_id" not in attachment
    assert "file_path" not in attachment


async def test_public_event_mapper_replaces_browser_file_id_with_share_url():
    screenshot = FileInfo(file_id="shot-1", filename="screenshot.png")

    class FileService:
        async def create_shared_session_signed_url(
            self, session_id, file_id, share_epoch
        ):
            return f"/shared/{session_id}/{file_id}/{share_epoch}"

    event = ToolEvent(
        tool_call_id="browser-1",
        tool_name="browser",
        tool_content=BrowserToolContent(screenshot="shot-1"),
        function_name="browser_navigate",
        function_args={"url": "https://example.com"},
        status=ToolStatus.CALLED,
    )
    mapped = await EventMapper.event_to_shared_sse_event(
        event,
        session_id="session-1",
        share_epoch="epoch-1",
        shared_files={"shot-1": screenshot},
        file_service=FileService(),
    )

    assert mapped.data.content.screenshot == "/shared/session-1/shot-1/epoch-1"
    assert mapped.data.content.screenshot != "shot-1"
    assert mapped.data.args == {}


async def test_private_event_mapper_preserves_tool_args_for_authenticated_ui():
    raw_args = {
        "file": "/tmp/private.txt",
        "content": "private file body",
        "token": "private-token",
    }
    event = ToolEvent(
        tool_call_id="file-1",
        tool_name="file",
        function_name="file_write",
        function_args=raw_args,
        status=ToolStatus.CALLED,
    )

    mapped = await EventMapper.event_to_sse_event(event)

    assert mapped.data.args == raw_args


async def test_public_event_mapper_never_exposes_raw_tool_args():
    class FileService:
        async def create_shared_session_signed_url(
            self, session_id, file_id, share_epoch
        ):
            raise AssertionError("these tool events do not contain shared files")

    events = [
        ToolEvent(
            tool_call_id="file-1",
            tool_name="file",
            function_name="file_write",
            function_args={"file": "/tmp/a", "content": "file-secret"},
            status=ToolStatus.CALLED,
        ),
        ToolEvent(
            tool_call_id="browser-1",
            tool_name="browser",
            function_name="browser_input",
            function_args={"text": "browser-secret", "password": "hunter2"},
            status=ToolStatus.CALLING,
        ),
        ToolEvent(
            tool_call_id="shell-1",
            tool_name="shell",
            function_name="shell_write_to_process",
            function_args={"id": "terminal-1", "input": "shell-secret"},
            status=ToolStatus.CALLED,
        ),
        ToolEvent(
            tool_call_id="mcp-1",
            tool_name="mcp",
            function_name="mcp_external_service",
            function_args={
                "api_token": "mcp-secret",
                "nested": {"authorization": "Bearer nested-secret"},
            },
            status=ToolStatus.CALLED,
        ),
    ]

    mapped = await EventMapper.events_to_shared_sse_events(
        events,
        session_id="session-1",
        share_epoch="epoch-1",
        shared_files={},
        file_service=FileService(),
    )

    assert len(mapped) == len(events)
    assert all(public_event.data.args == {} for public_event in mapped)
    public_json = "".join(event.model_dump_json() for event in mapped)
    for secret in (
        "file-secret",
        "browser-secret",
        "hunter2",
        "shell-secret",
        "mcp-secret",
        "nested-secret",
    ):
        assert secret not in public_json


async def test_public_event_mapper_redacts_sensitive_tool_result_content():
    class FileService:
        async def create_shared_session_signed_url(
            self, session_id, file_id, share_epoch
        ):
            raise AssertionError("sensitive tool results are never shared files")

    events = [
        ToolEvent(
            tool_call_id="file-1",
            tool_name="file",
            tool_content=FileToolContent(content="file-output-secret"),
            function_name="file_write",
            function_args={},
            status=ToolStatus.CALLED,
        ),
        ToolEvent(
            tool_call_id="shell-1",
            tool_name="shell",
            tool_content=ShellToolContent(
                console={"stdout": "shell-output-secret"}
            ),
            function_name="shell_exec",
            function_args={},
            status=ToolStatus.CALLED,
        ),
        ToolEvent(
            tool_call_id="search-1",
            tool_name="search",
            tool_content=SearchToolContent(
                results=[
                    SearchResultItem(
                        title="search-output-secret",
                        link="https://example.test/?token=search-url-secret",
                        snippet="search-snippet-secret",
                    )
                ]
            ),
            function_name="search_web",
            function_args={},
            status=ToolStatus.CALLED,
        ),
        ToolEvent(
            tool_call_id="mcp-1",
            tool_name="mcp",
            tool_content=McpToolContent(
                result={"authorization": "Bearer mcp-output-secret"}
            ),
            function_name="mcp_external_service",
            function_args={},
            status=ToolStatus.CALLED,
        ),
    ]

    mapped = await EventMapper.events_to_shared_sse_events(
        events,
        session_id="session-1",
        share_epoch="epoch-1",
        shared_files={},
        file_service=FileService(),
    )

    assert len(mapped) == len(events)
    assert all(public_event.data.content is None for public_event in mapped)
    public_json = "".join(event.model_dump_json() for event in mapped)
    for secret in (
        "file-output-secret",
        "shell-output-secret",
        "search-output-secret",
        "search-url-secret",
        "search-snippet-secret",
        "mcp-output-secret",
    ):
        assert secret not in public_json


async def test_public_event_mapper_keeps_only_sanitized_local_preview_metadata():
    class FileService:
        async def create_shared_session_signed_url(
            self, session_id, file_id, share_epoch
        ):
            raise AssertionError("preview metadata is not a shared file")

    event = ToolEvent(
        tool_call_id="preview-1",
        tool_name="preview",
        tool_content=PreviewToolContent(
            url="http://localhost:4173/app",
            title="private-preview-title",
        ),
        function_name="preview_show",
        function_args={"url": "http://localhost:4173/app"},
        status=ToolStatus.CALLED,
    )

    mapped = await EventMapper.event_to_shared_sse_event(
        event,
        session_id="session-1",
        share_epoch="epoch-1",
        shared_files={},
        file_service=FileService(),
    )

    assert mapped.data.args == {}
    assert mapped.data.content == PreviewToolContent(
        url="http://localhost:4173/app",
        title=None,
    )
    assert "private-preview-title" not in mapped.model_dump_json()


async def test_public_event_mapper_strips_secrets_from_local_preview_urls():
    class FileService:
        async def create_shared_session_signed_url(
            self, session_id, file_id, share_epoch
        ):
            raise AssertionError("preview metadata is not a shared file")

    private_urls = [
        (
            "http://preview-user:credential-secret@localhost:4173/app",
            "http://localhost:4173/app",
        ),
        (
            "http://localhost:4173/app?token=query-secret",
            "http://localhost:4173/app",
        ),
        (
            "http://localhost:4173/app#fragment-secret",
            "http://localhost:4173/app",
        ),
    ]
    events = [
        ToolEvent(
            tool_call_id=f"preview-{index}",
            tool_name="preview",
            tool_content=PreviewToolContent(
                url=url,
                title="preview-title-secret",
            ),
            function_name="preview_show",
            function_args={"url": url},
            status=ToolStatus.CALLED,
        )
        for index, (url, _expected_url) in enumerate(private_urls)
    ]

    mapped = await EventMapper.events_to_shared_sse_events(
        events,
        session_id="session-1",
        share_epoch="epoch-1",
        shared_files={},
        file_service=FileService(),
    )

    assert all(public_event.data.args == {} for public_event in mapped)
    assert [public_event.data.content.url for public_event in mapped] == [
        expected_url for _private_url, expected_url in private_urls
    ]
    assert all(public_event.data.content.title is None for public_event in mapped)
    public_json = "".join(event.model_dump_json() for event in mapped)
    for secret in (
        "credential-secret",
        "query-secret",
        "fragment-secret",
        "preview-title-secret",
    ):
        assert secret not in public_json


async def test_public_event_mapper_drops_non_local_preview_urls():
    class FileService:
        async def create_shared_session_signed_url(
            self, session_id, file_id, share_epoch
        ):
            raise AssertionError("preview metadata is not a shared file")

    event = ToolEvent(
        tool_call_id="preview-external",
        tool_name="preview",
        tool_content=PreviewToolContent(
            url="https://external-secret.example.test/app?token=url-secret",
            title="preview-title-secret",
        ),
        function_name="preview_show",
        function_args={
            "url": "https://external-secret.example.test/app?token=url-secret"
        },
        status=ToolStatus.CALLED,
    )

    mapped = await EventMapper.event_to_shared_sse_event(
        event,
        session_id="session-1",
        share_epoch="epoch-1",
        shared_files={},
        file_service=FileService(),
    )

    assert mapped.data.args == {}
    assert mapped.data.content is None
    public_json = mapped.model_dump_json()
    for secret in ("external-secret", "url-secret", "preview-title-secret"):
        assert secret not in public_json
