import asyncio
import io
from types import MethodType
from datetime import datetime, timedelta, UTC

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.domain.models.claw import Claw, ClawStatus
from app.domain.models.event import ErrorEvent, MessageEvent, ToolEvent, ToolStatus
from app.domain.models.file import FileInfo
from app.domain.models.memory import Memory
from app.domain.models.tool_result import ToolResult
from app.domain.services.claw_domain_service import ClawDomainService
from app.domain.services.agents.base import BaseAgent
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.prompts.runtime import build_runtime_environment_prompt
from app.domain.utils.robust_json_parser import RobustJsonParser
from app.infrastructure.external.claw.docker_claw_runtime import DockerClawRuntime
from app.interfaces.api.openai_routes import (
    _anthropic_stream_event_to_openai_chunks,
    _anthropic_messages_url,
    _anthropic_to_openai_response,
    _openai_chat_url,
    _openai_to_anthropic_request,
)


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


def test_openai_proxy_builds_provider_specific_target_urls():
    assert _openai_chat_url("https://api.openai.com") == "https://api.openai.com/v1/chat/completions"
    assert _openai_chat_url("http://mockserver:8090/v1") == "http://mockserver:8090/v1/chat/completions"
    assert _anthropic_messages_url("https://xuedingtoken.com") == "https://xuedingtoken.com/v1/messages"
    assert _anthropic_messages_url("https://xuedingtoken.com/v1") == "https://xuedingtoken.com/v1/messages"


def test_openai_proxy_converts_openai_chat_request_to_anthropic_messages():
    class FakeSettings:
        model_name = "claude-opus-4-6"
        max_tokens = 1024

    converted = _openai_to_anthropic_request({
        "model": "claude-opus-4-6",
        "stream": True,
        "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "你好"},
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Lookup data",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }],
    }, FakeSettings())

    assert converted["model"] == "claude-opus-4-6"
    assert converted["max_tokens"] == 1024
    assert converted["stream"] is True
    assert converted["system"] == "Be brief."
    assert converted["messages"] == [{"role": "user", "content": "你好"}]
    assert converted["tools"][0]["name"] == "lookup"
    assert converted["tools"][0]["input_schema"]["properties"]["q"]["type"] == "string"


def test_openai_proxy_converts_anthropic_response_to_openai_chat_completion():
    converted = _anthropic_to_openai_response({
        "id": "msg_123",
        "model": "claude-opus-4-6",
        "content": [{"type": "text", "text": "E2E_OK"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }, "claude-opus-4-6")

    assert converted["object"] == "chat.completion"
    assert converted["choices"][0]["message"]["role"] == "assistant"
    assert converted["choices"][0]["message"]["content"] == "E2E_OK"
    assert converted["choices"][0]["finish_reason"] == "stop"
    assert converted["usage"]["total_tokens"] == 7


def test_openai_proxy_streams_anthropic_tool_use_as_openai_tool_calls():
    state = {
        "completion_id": None,
        "model": "claude-opus-4-6",
        "tool_blocks": {},
        "next_tool_index": 0,
    }

    chunks = []
    for event in [
        {"type": "message_start", "message": {"id": "msg_123", "model": "claude-opus-4-6"}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "browser_open", "input": {}},
        },
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"url\":"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "\"https://example.com\"}"}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]:
        chunks.extend(_anthropic_stream_event_to_openai_chunks(event, state))

    tool_start = chunks[1]["choices"][0]["delta"]["tool_calls"][0]
    first_args = chunks[2]["choices"][0]["delta"]["tool_calls"][0]
    second_args = chunks[3]["choices"][0]["delta"]["tool_calls"][0]

    assert tool_start["id"] == "toolu_1"
    assert tool_start["function"]["name"] == "browser_open"
    assert first_args["index"] == 0
    assert first_args["function"]["arguments"] == "{\"url\":"
    assert second_args["function"]["arguments"] == "\"https://example.com\"}"
    assert chunks[4]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1] == "[DONE]"


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


def test_claw_http_base_url_supports_published_host_ports():
    assert (
        Claw(
            id="claw",
            user_id="user",
            api_key="key",
            container_ip="127.0.0.1:49152",
        ).http_base_url
        == "http://127.0.0.1:49152"
    )
    assert (
        Claw(
            id="claw",
            user_id="user",
            api_key="key",
            container_ip="http://localhost:18788",
        ).http_base_url
        == "http://localhost:18788"
    )


def test_docker_claw_runtime_prefers_sandbox_backend_url_for_host_backend():
    class FakeSettings:
        manus_api_base_url = "http://backend:8000"
        backend_sandbox_url = "http://host.docker.internal:8127"
        backend_internal_url = "http://backend:8000"
        backend_public_url = "http://127.0.0.1:8127"

    runtime = object.__new__(DockerClawRuntime)
    runtime.settings = FakeSettings()

    assert runtime._container_reachable_backend_url() == "http://host.docker.internal:8127"


async def test_stale_creating_claw_is_marked_error():
    class FakeRepository:
        def __init__(self):
            self.claw = Claw(
                id="claw",
                user_id="user",
                api_key="key",
                status=ClawStatus.CREATING,
                updated_at=datetime.now(UTC) - timedelta(seconds=120),
            )
            self.updated = None

        async def get_by_user_id(self, user_id):
            return self.claw

        async def update(self, claw):
            self.updated = claw
            self.claw = claw
            return claw

    class FakeRuntime:
        ready_timeout = 10

    repo = FakeRepository()
    service = ClawDomainService(repo, FakeRuntime(), claw_client=None)

    claw = await service.get_claw("user")

    assert claw.status == ClawStatus.ERROR
    assert "timed out" in claw.error_message
    assert repo.updated.status == ClawStatus.ERROR


async def test_running_claw_ignores_legacy_expiry_when_ttl_disabled():
    class FakeRepository:
        def __init__(self):
            self.claw = Claw(
                id="claw",
                user_id="user",
                api_key="key",
                status=ClawStatus.RUNNING,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
            self.deleted = False
            self.updated = None

        async def get_by_user_id(self, user_id):
            return self.claw

        async def update(self, claw):
            self.updated = claw
            self.claw = claw
            return claw

        async def delete_by_user_id(self, user_id):
            self.deleted = True
            return True

    class FakeRuntime:
        ready_timeout = 10

        def __init__(self):
            self.destroyed = []

        async def destroy(self, instance_name):
            self.destroyed.append(instance_name)

    repo = FakeRepository()
    runtime = FakeRuntime()
    service = ClawDomainService(repo, runtime, claw_client=None)
    old_ttl = service.settings.claw_ttl_seconds
    service.settings.claw_ttl_seconds = 0
    try:
        claw = await service.get_claw("user")

        assert claw.status == ClawStatus.RUNNING
        assert claw.expires_at is None
        assert repo.updated.expires_at is None
        assert repo.deleted is False
        assert runtime.destroyed == []
    finally:
        service.settings.claw_ttl_seconds = old_ttl


async def test_delete_claw_destroys_runtime_instance(monkeypatch):
    class FakeRepository:
        def __init__(self):
            self.deleted = False
            self.claw = Claw(
                id="claw",
                user_id="user",
                api_key="key",
                container_name="manus-claw-test",
                container_ip="127.0.0.1:49152",
                status=ClawStatus.RUNNING,
            )

        async def get_by_user_id(self, user_id):
            return self.claw

        async def delete_by_user_id(self, user_id):
            self.deleted = True
            return True

    class FakeRuntime:
        ready_timeout = 10

        def __init__(self):
            self.destroyed = []

        async def destroy(self, instance_name):
            self.destroyed.append(instance_name)

    repo = FakeRepository()
    runtime = FakeRuntime()
    service = ClawDomainService(repo, runtime, claw_client=None)

    assert await service.delete_claw("user") is True
    assert runtime.destroyed == ["manus-claw-test"]
    assert repo.deleted is True


async def test_claw_capacity_limit_rejects_new_user(monkeypatch):
    class FakeRepository:
        async def get_by_user_id(self, user_id):
            return None

        async def count_by_statuses(self, statuses):
            return 1

    class FakeRuntime:
        ready_timeout = 10

    service = ClawDomainService(FakeRepository(), FakeRuntime(), claw_client=None)
    old_limit = service.settings.claw_max_instances_total
    service.settings.claw_max_instances_total = 1
    try:
        try:
            await service.prepare_claw_for_creation("new-user")
        except RuntimeError as exc:
            assert "capacity" in str(exc)
        else:
            raise AssertionError("Expected capacity RuntimeError")
    finally:
        service.settings.claw_max_instances_total = old_limit


async def test_cleanup_removes_idle_running_claw(monkeypatch):
    class FakeRepository:
        def __init__(self):
            self.deleted = []
            self.claw = Claw(
                id="claw",
                user_id="user",
                api_key="key",
                container_name="manus-claw-idle",
                container_ip="127.0.0.1:49152",
                status=ClawStatus.RUNNING,
                last_activity_at=datetime.now(UTC) - timedelta(seconds=120),
            )

        async def list_by_statuses(self, statuses):
            return [self.claw]

        async def delete_by_user_id(self, user_id):
            self.deleted.append(user_id)
            return True

    class FakeRuntime:
        ready_timeout = 10

        def __init__(self):
            self.destroyed = []

        async def destroy(self, instance_name):
            self.destroyed.append(instance_name)

    repo = FakeRepository()
    runtime = FakeRuntime()
    service = ClawDomainService(repo, runtime, claw_client=None)
    old_timeout = service.settings.claw_idle_timeout_seconds
    service.settings.claw_idle_timeout_seconds = 30
    try:
        result = await service.cleanup_instances()

        assert result == {"removed": 1, "errored": 0}
        assert runtime.destroyed == ["manus-claw-idle"]
        assert repo.deleted == ["user"]
    finally:
        service.settings.claw_idle_timeout_seconds = old_timeout


async def test_cleanup_keeps_idle_running_claw_when_idle_timeout_disabled(monkeypatch):
    class FakeRepository:
        def __init__(self):
            self.deleted = []
            self.claw = Claw(
                id="claw",
                user_id="user",
                api_key="key",
                container_name="manus-claw-persistent",
                container_ip="127.0.0.1:49152",
                status=ClawStatus.RUNNING,
                last_activity_at=datetime.now(UTC) - timedelta(days=30),
            )

        async def list_by_statuses(self, statuses):
            return [self.claw]

        async def delete_by_user_id(self, user_id):
            self.deleted.append(user_id)
            return True

    class FakeRuntime:
        ready_timeout = 10

        def __init__(self):
            self.destroyed = []

        async def destroy(self, instance_name):
            self.destroyed.append(instance_name)

    repo = FakeRepository()
    runtime = FakeRuntime()
    service = ClawDomainService(repo, runtime, claw_client=None)
    old_timeout = service.settings.claw_idle_timeout_seconds
    service.settings.claw_idle_timeout_seconds = 0
    try:
        result = await service.cleanup_instances()

        assert result == {"removed": 0, "errored": 0}
        assert runtime.destroyed == []
        assert repo.deleted == []
    finally:
        service.settings.claw_idle_timeout_seconds = old_timeout


async def test_provision_leaves_running_claw_persistent_when_ttl_disabled(monkeypatch):
    class FakeRepository:
        def __init__(self):
            self.updated = None
            self.messages = []

        async def update(self, claw):
            self.updated = claw
            return claw

        async def append_message(self, user_id, role, content):
            self.messages.append((user_id, role, content))

    class FakeRuntimeInfo:
        instance_name = "manus-claw-persistent"
        address = "127.0.0.1:49152"

    class FakeRuntime:
        ready_timeout = 10

        async def create(self, claw_id, api_key):
            return FakeRuntimeInfo()

        async def wait_for_ready(self, base_url):
            return True

    repo = FakeRepository()
    service = ClawDomainService(repo, FakeRuntime(), claw_client=None)
    claw = Claw(
        id="claw",
        user_id="user",
        api_key="key",
        status=ClawStatus.CREATING,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    await service.provision_claw_instance(claw, ttl_seconds=0)

    assert repo.updated.status == ClawStatus.RUNNING
    assert repo.updated.expires_at is None
    assert repo.messages


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
