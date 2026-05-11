import asyncio
import io
from types import MethodType

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.domain.models.event import ErrorEvent, MessageEvent, ToolEvent, ToolStatus
from app.domain.models.file import FileInfo
from app.domain.models.memory import Memory
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.prompts.runtime import build_runtime_environment_prompt
from app.domain.utils.robust_json_parser import RobustJsonParser


class PassthroughParser:
    async def ainvoke(self, message, config=None, **kwargs):
        return message


class FakeChain:
    def __init__(self, model, parser):
        self.model = model
        self.parser = parser

    async def ainvoke(self, context):
        self.model.contexts.append(context)
        return await self.parser.ainvoke(self.model.responses.pop(0))


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.bound_kwargs = []
        self.contexts = []
        self.bind_tools_calls = 0

    def bind(self, **kwargs):
        self.bound_kwargs.append(kwargs)
        return self

    def bind_tools(self, tools):
        self.bound_tools = tools
        self.bind_tools_calls += 1
        return self

    def __or__(self, parser):
        return FakeChain(self, parser)


def make_agent(monkeypatch, responses):
    monkeypatch.setattr(
        "app.domain.services.agents.base.RobustJsonParser.from_llm",
        lambda _: PassthroughParser(),
    )

    agent = object.__new__(BaseAgent)
    agent._model = FakeModel(responses)
    agent._model_provider = "anthropic"
    agent.tool_choice = None
    agent.max_retries = 3
    agent.max_iterations = 10
    agent.system_prompt = ""
    agent.toolkits = []
    agent.memory = Memory()
    agent._tool_call_timeout_seconds = 0.01

    async def add_to_memory(self, messages):
        self.memory.add_messages(messages)

    agent._add_to_memory = MethodType(add_to_memory, agent)
    return agent


async def test_empty_anthropic_tool_use_response_retries_internally(monkeypatch):
    empty_tool_use = AIMessage(
        content=[],
        response_metadata={"stop_reason": "tool_use"},
    )
    final_message = AIMessage(
        content="done",
        response_metadata={"stop_reason": "end_turn"},
    )
    agent = make_agent(monkeypatch, [empty_tool_use, final_message])

    result = await agent.ask_with_messages([HumanMessage(content="run a tool")])

    assert result.content == "done"
    assert len(agent._model.contexts) == 2
    assert "valid tool call payload" in agent._model.contexts[1][-1].content
    assert "tool_choice" not in agent._model.bound_kwargs[0]


async def test_execute_does_not_emit_user_retry_error_for_empty_response(monkeypatch):
    empty_tool_use = AIMessage(
        content=[],
        response_metadata={"stop_reason": "tool_use"},
    )
    final_message = AIMessage(
        content="final answer",
        response_metadata={"stop_reason": "end_turn"},
    )
    agent = make_agent(monkeypatch, [empty_tool_use, final_message])

    events = [event async for event in agent.execute("run a tool")]

    assert any(isinstance(event, MessageEvent) and event.message == "final answer" for event in events)
    assert not any(
        isinstance(event, ErrorEvent)
        and "Model returned an empty response. Please retry the request." in event.error
        for event in events
    )


async def test_repeated_empty_tool_use_falls_back_to_no_tool_call(monkeypatch):
    empty_tool_use = AIMessage(
        content=[],
        response_metadata={"stop_reason": "tool_use"},
    )
    fallback_message = AIMessage(
        content="fallback answer",
        response_metadata={"stop_reason": "end_turn"},
    )
    agent = make_agent(
        monkeypatch,
        [empty_tool_use, empty_tool_use, empty_tool_use, fallback_message],
    )

    result = await agent.ask_with_messages([HumanMessage(content="run a tool")])

    assert result.content == "fallback answer"
    assert len(agent._model.contexts) == 4
    assert "Do not call tools now" in agent._model.contexts[-1][-1].content
    assert agent._model.bind_tools_calls == 1


async def test_promotes_anthropic_tool_use_content_blocks():
    parser = object.__new__(RobustJsonParser)
    message = AIMessage(
        content=[
            {
                "id": "tooluse_1",
                "type": "tool_use",
                "name": "shell_exec",
                "input": {"command": "echo ok"},
            }
        ]
    )

    promoted = await parser._repair_invalid_tool_calls(message)

    assert promoted.tool_calls[0]["name"] == "shell_exec"
    assert promoted.tool_calls[0]["args"] == {"command": "echo ok"}
    assert promoted.content == []


async def test_promotes_anthropic_tool_use_string_input_blocks():
    parser = object.__new__(RobustJsonParser)
    message = AIMessage(
        content=[
            {
                "id": "tooluse_1",
                "type": "tool_use",
                "name": "shell_exec",
                "input": '{"command": "echo ok"}',
            }
        ]
    )

    promoted = await parser._repair_invalid_tool_calls(message)

    assert promoted.tool_calls[0]["args"] == {"command": "echo ok"}


async def test_tool_timeout_returns_tool_message(monkeypatch):
    agent = make_agent(monkeypatch, [])

    class HangingTool:
        name = "browser_navigate"

        async def ainvoke(self, tool_call):
            await asyncio.sleep(1)

    result = await agent.invoke_tool(
        HangingTool(),
        {"id": "tooluse_timeout", "name": "browser_navigate", "args": {}},
    )

    assert result.tool_call_id == "tooluse_timeout"
    assert "timed out" in result.content
    assert result.artifact.success is False


def test_runtime_environment_prompt_exposes_configured_ports():
    class FakeSandbox:
        id = "sandbox-1"
        base_url = "http://127.0.0.1:18080"
        cdp_url = "http://127.0.0.1:19222"
        vnc_url = "ws://127.0.0.1:15901"

    prompt = build_runtime_environment_prompt(FakeSandbox())

    assert "<runtime_environment>" in prompt
    assert "http://127.0.0.1:18080" in prompt
    assert "19222" in prompt
    assert "15901" in prompt
    assert "Do not assume `localhost`" in prompt


def test_base_agent_refreshes_stale_system_prompt():
    agent = object.__new__(BaseAgent)
    agent.system_prompt = "new runtime prompt"
    agent.memory = Memory(
        messages=[
            SystemMessage(content="old runtime prompt"),
            HumanMessage(content="hello"),
        ]
    )

    agent._ensure_current_system_prompt()

    assert agent.memory.messages[0].content == "new runtime prompt"
    assert agent.memory.messages[1].content == "hello"


async def test_missing_markdown_attachment_is_materialized_from_final_message():
    class FakeSandbox:
        def __init__(self):
            self.files = {}
            self.writes = []

        async def file_download(self, path):
            if path not in self.files:
                raise FileNotFoundError(path)
            return io.BytesIO(self.files[path].encode())

        async def file_find(self, path, glob_pattern):
            return type("Result", (), {"data": {"files": []}})()

        async def file_write(self, file, content, **kwargs):
            self.files[file] = content
            self.writes.append((file, content))
            return type("Result", (), {"success": True})()

    class FakeSessionRepository:
        def __init__(self):
            self.files = []

        async def get_file_by_path(self, session_id, file_path):
            return None

        async def add_file(self, session_id, file_info):
            self.files.append(file_info)

    class FakeFileStorage:
        async def upload_file(self, file_data, file_name, user_id):
            return FileInfo(
                file_id="stored_file",
                filename=file_name,
                size=len(file_data.read()),
            )

    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._session_id = "session"
    runner._user_id = "user"
    runner._sandbox = FakeSandbox()
    runner._session_repository = FakeSessionRepository()
    runner._file_storage = FakeFileStorage()
    runner._generated_artifacts = {}
    runner._synced_artifacts = {}

    event = MessageEvent(
        message="# 乔布斯演讲资料全集\n\n正文内容",
        attachments=[FileInfo(file_path="/home/ubuntu/乔布斯演讲资料全集.md")],
    )

    await runner._sync_message_attachments_to_storage(event)

    assert runner._sandbox.writes[0][0] == "/home/ubuntu/乔布斯演讲资料全集.md"
    assert "乔布斯演讲资料全集" in runner._sandbox.writes[0][1]
    assert event.attachments[0].file_id == "stored_file"
    assert event.attachments[0].filename == "乔布斯演讲资料全集.md"


async def test_generated_artifact_is_auto_attached_without_keyword_intent_matching():
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._generated_artifacts = {
        "/home/ubuntu/upload/report.md": FileInfo(
            file_id="stored_report",
            filename="report.md",
            file_path="/home/ubuntu/upload/report.md",
        )
    }
    runner._synced_artifacts = {}

    event = MessageEvent(message="处理完成。", attachments=[])

    await runner._sync_message_attachments_to_storage(event)

    assert len(event.attachments) == 1
    assert event.attachments[0].file_id == "stored_report"
    assert event.attachments[0].file_path == "/home/ubuntu/upload/report.md"


async def test_generated_artifact_outside_deliverable_root_is_not_auto_attached():
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._generated_artifacts = {}
    runner._synced_artifacts = {}

    runner._remember_generated_artifact(
        FileInfo(
            file_id="stored_temp",
            filename="temp.md",
            file_path="/tmp/temp.md",
        )
    )

    event = MessageEvent(message="处理完成。", attachments=[])

    await runner._sync_message_attachments_to_storage(event)

    assert event.attachments == []


async def test_relative_attachment_path_resolves_to_upload_directory():
    class FakeSandbox:
        def __init__(self):
            self.files = {"/home/ubuntu/upload/report.md": b"content"}

        async def file_download(self, path):
            if path not in self.files:
                raise FileNotFoundError(path)
            return io.BytesIO(self.files[path])

        async def file_find(self, path, glob_pattern):
            return type("Result", (), {"data": {"files": []}})()

    class FakeSessionRepository:
        def __init__(self):
            self.files = []

        async def get_file_by_path(self, session_id, file_path):
            return None

        async def remove_file(self, session_id, file_id):
            pass

        async def add_file(self, session_id, file_info):
            self.files.append(file_info)

    class FakeFileStorage:
        async def upload_file(self, file_data, file_name, user_id):
            return FileInfo(
                file_id="stored_relative",
                filename=file_name,
                size=len(file_data.read()),
            )

    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._session_id = "session"
    runner._user_id = "user"
    runner._sandbox = FakeSandbox()
    runner._session_repository = FakeSessionRepository()
    runner._file_storage = FakeFileStorage()
    runner._generated_artifacts = {}
    runner._synced_artifacts = {}

    event = MessageEvent(
        message="报告已完成。",
        attachments=[FileInfo(file_path="report.md")],
    )

    await runner._sync_message_attachments_to_storage(event)

    assert event.attachments[0].file_id == "stored_relative"
    assert event.attachments[0].filename == "report.md"
    assert event.attachments[0].file_path == "/home/ubuntu/upload/report.md"


async def test_synced_artifact_path_is_reused_without_duplicate_uploads():
    class FakeSandbox:
        async def file_download(self, path):
            if path != "/home/ubuntu/upload/report.md":
                raise FileNotFoundError(path)
            return io.BytesIO(b"content")

        async def file_find(self, path, glob_pattern):
            return type("Result", (), {"data": {"files": []}})()

    class FakeSessionRepository:
        def __init__(self):
            self.files = []
            self.removed = []

        async def get_file_by_path(self, session_id, file_path):
            return None

        async def remove_file(self, session_id, file_id):
            self.removed.append(file_id)

        async def add_file(self, session_id, file_info):
            self.files.append(file_info)

    class FakeFileStorage:
        def __init__(self):
            self.uploads = 0

        async def upload_file(self, file_data, file_name, user_id):
            self.uploads += 1
            return FileInfo(
                file_id=f"stored_{self.uploads}",
                filename=file_name,
                size=len(file_data.read()),
            )

    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._session_id = "session"
    runner._user_id = "user"
    runner._sandbox = FakeSandbox()
    runner._session_repository = FakeSessionRepository()
    runner._file_storage = FakeFileStorage()
    runner._generated_artifacts = {}
    runner._synced_artifacts = {}

    first = await runner._sync_file_to_storage(
        "/home/ubuntu/upload/report.md",
        generated=True,
    )
    second = await runner._sync_file_to_storage("/home/ubuntu/upload/report.md")

    assert first.file_id == "stored_1"
    assert second.file_id == "stored_1"
    assert runner._file_storage.uploads == 1


async def test_shell_created_artifact_is_tracked_for_delivery():
    class FakeSandbox:
        async def view_shell(self, session_id, console=False):
            return ToolResult(
                success=True,
                data={
                    "console": [
                        {
                            "command": "printf hi > /home/ubuntu/upload/shell-artifact.md",
                            "output": "done",
                        }
                    ]
                },
            )

        async def file_download(self, path):
            if path != "/home/ubuntu/upload/shell-artifact.md":
                raise FileNotFoundError(path)
            return io.BytesIO(b"hi")

        async def file_find(self, path, glob_pattern):
            return type("Result", (), {"data": {"files": []}})()

    class FakeSessionRepository:
        async def get_file_by_path(self, session_id, file_path):
            return None

        async def remove_file(self, session_id, file_id):
            pass

        async def add_file(self, session_id, file_info):
            pass

    class FakeFileStorage:
        async def upload_file(self, file_data, file_name, user_id):
            return FileInfo(
                file_id="stored_shell",
                filename=file_name,
                size=len(file_data.read()),
            )

    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._session_id = "session"
    runner._user_id = "user"
    runner._sandbox = FakeSandbox()
    runner._session_repository = FakeSessionRepository()
    runner._file_storage = FakeFileStorage()
    runner._generated_artifacts = {}
    runner._synced_artifacts = {}

    event = ToolEvent(
        tool_call_id="tool-1",
        tool_name="shell",
        function_name="shell_exec",
        function_args={
            "id": "shell-1",
            "exec_dir": "/home/ubuntu",
            "command": "printf hi > /home/ubuntu/upload/shell-artifact.md",
        },
        status=ToolStatus.CALLED,
    )

    await runner._handle_tool_event(event)

    assert "/home/ubuntu/upload/shell-artifact.md" in runner._generated_artifacts
    assert runner._generated_artifacts["/home/ubuntu/upload/shell-artifact.md"].file_id == "stored_shell"
