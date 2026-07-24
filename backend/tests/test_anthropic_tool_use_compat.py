import asyncio
import io
from types import MethodType
from datetime import datetime, timedelta, UTC

from langchain_core.messages import AIMessage

from app.domain.models.claw import Claw, ClawStatus
from app.domain.models.event import ErrorEvent, MessageEvent, ToolEvent, ToolStatus
from app.domain.models.file import FileInfo
from app.domain.models.memory import Memory
from app.domain.models.message import LLMMessage, ToolCall
from app.domain.models.tool_result import ToolResult
from app.domain.services.claw_domain_service import ClawDomainService
from app.domain.services.agents.base import BaseAgent
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.prompts.runtime import build_runtime_environment_prompt
from app.infrastructure.external.llm.langchain_llm import LangchainLLM
from app.infrastructure.external.llm.robust_json_parser import RobustJsonParser
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
        self.bind_tools_kwargs = []

    def bind(self, **kwargs):
        self.bound_kwargs.append(kwargs)
        return self

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = tools
        self.bind_tools_calls += 1
        self.bind_tools_kwargs.append(kwargs)
        return self

    def __or__(self, parser):
        return FakeChain(self, parser)


def make_llm(monkeypatch, responses):
    monkeypatch.setattr(
        "app.infrastructure.external.llm.langchain_llm.RobustJsonParser.from_llm",
        lambda _: PassthroughParser(),
    )

    llm = object.__new__(LangchainLLM)
    llm._model = FakeModel(responses)
    llm._model_provider = "anthropic"
    llm._max_retries = 3
    return llm


def make_agent(monkeypatch, responses):
    llm = make_llm(monkeypatch, responses)

    agent = object.__new__(BaseAgent)
    agent._llm = llm
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
    llm = make_llm(monkeypatch, [empty_tool_use, final_message])

    result = await llm.ask(
        [LLMMessage.user("run a tool")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "shell_exec",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    assert result.content == "done"
    assert len(llm._model.contexts) == 2
    assert "valid tool call payload" in llm._model.contexts[1][-1].content
    assert "tool_choice" not in llm._model.bind_tools_kwargs[0]


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
    llm = make_llm(
        monkeypatch,
        [empty_tool_use, empty_tool_use, empty_tool_use, fallback_message],
    )

    result = await llm.ask(
        [LLMMessage.user("run a tool")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "shell_exec",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    assert result.content == "fallback answer"
    assert len(llm._model.contexts) == 4
    assert "Do not call tools now" in llm._model.contexts[-1][-1].content
    assert llm._model.bind_tools_calls == 1


async def test_required_tool_choice_is_removed_from_no_tools_fallback(monkeypatch):
    empty_tool_use = AIMessage(
        content=[],
        response_metadata={"stop_reason": "tool_use"},
    )
    fallback_message = AIMessage(
        content="fallback answer",
        response_metadata={"stop_reason": "end_turn"},
    )
    llm = make_llm(
        monkeypatch,
        [empty_tool_use, empty_tool_use, empty_tool_use, fallback_message],
    )

    result = await llm.ask(
        [LLMMessage.user("submit structured output")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "create_plan",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_choice="required",
    )

    assert result.content == "fallback answer"
    assert llm._model.bind_tools_kwargs[0]["tool_choice"] == "any"
    assert "tool_choice" not in llm._model.bound_kwargs[-1]


async def test_required_tool_choice_is_bound_atomically_with_tools(monkeypatch):
    llm = make_llm(
        monkeypatch,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "plan_1",
                        "name": "create_plan",
                        "args": {"steps": []},
                        "type": "tool_call",
                    }
                ],
            )
        ],
    )
    llm._model_provider = "openai"

    result = await llm.ask(
        [LLMMessage.user("submit a plan")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "create_plan",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_choice="required",
    )

    assert result.tool_calls[0].name == "create_plan"
    assert llm._model.bind_tools_kwargs == [{"tool_choice": "required"}]
    assert llm._model.bound_kwargs == []


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

        async def invoke(self, args):
            await asyncio.sleep(1)

    result = await agent.invoke_tool(
        HangingTool(),
        ToolCall(id="tooluse_timeout", name="browser_navigate", args={}),
    )

    assert result.tool_call_id == "tooluse_timeout"
    assert "timed out" in result.content
    assert result.artifact.success is False


async def test_tool_exception_does_not_return_secret_bearing_diagnostic(monkeypatch):
    agent = make_agent(monkeypatch, [])
    agent.max_retries = 0

    class FailingTool:
        name = "external_lookup"

        async def invoke(self, args):
            raise RuntimeError(
                "upstream rejected api_key=secret-key at "
                "https://provider.example/path?token=signed-secret"
            )

    result = await agent.invoke_tool(
        FailingTool(),
        ToolCall(id="tooluse_failure", name="external_lookup", args={}),
    )

    assert result.tool_call_id == "tooluse_failure"
    assert result.content == "Tool execution failed: RuntimeError"
    assert "secret-key" not in result.content
    assert "signed-secret" not in result.content


async def test_side_effect_tool_transport_failure_is_never_replayed(monkeypatch):
    agent = make_agent(monkeypatch, [])
    agent.retry_interval = 0
    calls = 0

    class CommittedThenDisconnectedTool:
        name = "side_effect"
        retryable = False

        async def invoke(self, args):
            nonlocal calls
            calls += 1
            raise ConnectionError("response lost after commit")

    result = await agent.invoke_tool(
        CommittedThenDisconnectedTool(),
        ToolCall(id="side-effect-1", name="side_effect", args={}),
    )

    assert calls == 1
    assert result.content == "Tool execution failed: ConnectionError"


async def test_explicitly_retryable_read_tool_keeps_bounded_retries(monkeypatch):
    agent = make_agent(monkeypatch, [])
    agent.retry_interval = 0
    agent.max_retries = 2
    calls = 0

    class RetryableReadTool:
        name = "read_only"
        retryable = True

        async def invoke(self, args):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ConnectionError("temporary read failure")
            return ToolResult(success=True, data="ok")

    result = await agent.invoke_tool(
        RetryableReadTool(),
        ToolCall(id="read-1", name="read_only", args={}),
    )

    assert calls == 3
    assert result.artifact.success is True


def test_runtime_environment_prompt_exposes_ports_but_not_gateway_capabilities():
    class FakeSandbox:
        id = "sandbox-secret-id"
        base_url = "https://gateway.example/api?token=secret-token"
        cdp_url = "wss://gateway.example/cdp?token=secret-token"
        vnc_url = "wss://gateway.example/vnc?token=secret-token"

    prompt = build_runtime_environment_prompt(FakeSandbox())

    assert "<runtime_environment>" in prompt
    assert "Sandbox API port" in prompt
    assert "Sandbox Chrome CDP port" in prompt
    assert "Sandbox VNC port" in prompt
    assert "secret-token" not in prompt
    assert "gateway.example" not in prompt
    assert "sandbox-secret-id" not in prompt
    assert "Do not assume `localhost`" in prompt


def test_base_agent_refreshes_stale_system_prompt():
    agent = object.__new__(BaseAgent)
    agent.system_prompt = "new runtime prompt"
    agent.memory = Memory(
        messages=[
            LLMMessage.system("old runtime prompt"),
            LLMMessage.user("hello"),
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


def test_docker_claw_runtime_uses_container_ip_on_claw_network(monkeypatch):
    runtime = object.__new__(DockerClawRuntime)
    runtime.settings = type("Settings", (), {"claw_network": "manus-network"})()
    monkeypatch.setattr(runtime, "_is_running_in_container", lambda: False)

    assert (
        runtime._select_runtime_address("172.20.0.8", "127.0.0.1:49152")
        == "172.20.0.8"
    )


def test_docker_claw_runtime_uses_published_port_for_host_backend(monkeypatch):
    runtime = object.__new__(DockerClawRuntime)
    runtime.settings = type("Settings", (), {"claw_network": None})()
    monkeypatch.setattr(runtime, "_is_running_in_container", lambda: False)

    assert (
        runtime._select_runtime_address("172.17.0.8", "127.0.0.1:49152")
        == "127.0.0.1:49152"
    )


def test_docker_claw_runtime_never_uses_host_loopback_inside_backend_container(
    monkeypatch,
):
    runtime = object.__new__(DockerClawRuntime)
    runtime.settings = type("Settings", (), {"claw_network": None})()
    monkeypatch.setattr(runtime, "_is_running_in_container", lambda: True)

    assert (
        runtime._select_runtime_address("172.17.0.8", "127.0.0.1:49152")
        == "172.17.0.8"
    )


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
            files = [
                file_path
                for file_path in self.files
                if file_path.rsplit("/", 1)[0] == path
            ]
            return type("Result", (), {"data": {"files": files}})()

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
        async def upload_file(
            self, file_data, file_name, user_id, metadata=None
        ):
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
            files = [
                file_path
                for file_path in self.files
                if file_path.rsplit("/", 1)[0] == path
            ]
            return type("Result", (), {"data": {"files": files}})()

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
        async def upload_file(
            self, file_data, file_name, user_id, metadata=None
        ):
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
        def __init__(self):
            self.downloads = 0

        async def file_download(self, path):
            if path != "/home/ubuntu/upload/report.md":
                raise FileNotFoundError(path)
            self.downloads += 1
            return io.BytesIO(b"content")

        async def file_find(self, path, glob_pattern):
            files = (
                ["/home/ubuntu/upload/report.md"]
                if path == "/home/ubuntu/upload"
                else []
            )
            return type("Result", (), {"data": {"files": files}})()

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

        async def upload_file(
            self, file_data, file_name, user_id, metadata=None
        ):
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
    assert runner._sandbox.downloads == 1


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
            files = (
                ["/home/ubuntu/upload/shell-artifact.md"]
                if path == "/home/ubuntu/upload"
                else []
            )
            return type("Result", (), {"data": {"files": files}})()

    class FakeSessionRepository:
        async def get_file_by_path(self, session_id, file_path):
            return None

        async def remove_file(self, session_id, file_id):
            pass

        async def add_file(self, session_id, file_info):
            pass

    class FakeFileStorage:
        async def upload_file(
            self, file_data, file_name, user_id, metadata=None
        ):
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


def test_shell_artifact_extraction_does_not_promote_relative_paths_to_root():
    runner = object.__new__(AgentTaskRunner)

    paths = runner._extract_artifact_paths(
        "\n".join(
            [
                "./PLAN.md",
                "../draft.md",
                "assets/index.html",
                "https://example.com/download/report.pdf",
                "https://example.com/?file=/PLAN.md",
                "Saved:/home/ubuntu/upload/saved.pdf",
                "C:/Windows/not-an-artifact.txt",
                "/home/ubuntu/upload/result.md",
                "~/notes.md",
            ]
        )
    )

    assert paths == [
        "/home/ubuntu/upload/saved.pdf",
        "/home/ubuntu/upload/result.md",
        "/home/ubuntu/notes.md",
    ]


async def test_missing_root_artifact_never_triggers_recursive_root_search():
    class FakeSandbox:
        def __init__(self):
            self.searches = []

        async def file_download(self, path):
            raise FileNotFoundError(path)

        async def file_find(self, path, glob_pattern):
            self.searches.append((path, glob_pattern))
            return type("Result", (), {"data": {"files": []}})()

    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._sandbox = FakeSandbox()

    resolved = await runner._resolve_existing_sandbox_file("/PLAN.md")

    assert resolved is None
    assert runner._sandbox.searches == [
        ("/", "PLAN.md"),
        ("/home/ubuntu", "**/PLAN.md"),
        ("/home/ubuntu/upload", "**/PLAN.md"),
        ("/tmp", "**/PLAN.md"),
    ]
    assert ("/", "**/PLAN.md") not in runner._sandbox.searches


async def test_missing_artifact_escapes_glob_metacharacters_in_basename():
    class FakeSandbox:
        def __init__(self):
            self.patterns = []

        async def file_download(self, path):
            raise FileNotFoundError(path)

        async def file_find(self, path, glob_pattern):
            self.patterns.append(glob_pattern)
            return type("Result", (), {"data": {"files": []}})()

    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._sandbox = FakeSandbox()

    resolved = await runner._resolve_existing_sandbox_file(
        "/home/ubuntu/upload/report[1]*?.md"
    )

    assert resolved is None
    assert runner._sandbox.patterns == [
        "report[[]1][*][?].md",
        "**/report[[]1][*][?].md",
        "**/report[[]1][*][?].md",
        "**/report[[]1][*][?].md",
    ]


def test_artifact_extraction_bounds_input_text_and_result_count():
    runner = object.__new__(AgentTaskRunner)
    runner._MAX_ARTIFACT_DISCOVERY_TEXT_CHARS = 80

    paths = runner._extract_artifact_paths(
        " ".join(
            f"/home/ubuntu/upload/report-{index}.md"
            for index in range(100)
        ),
        max_paths=2,
    )

    assert paths == [
        "/home/ubuntu/upload/report-0.md",
        "/home/ubuntu/upload/report-1.md",
    ]


async def test_shell_artifact_sync_timeout_is_fail_open():
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._ARTIFACT_SYNC_TIMEOUT_SECONDS = 0.01
    release = asyncio.Event()
    finished = asyncio.Event()

    async def never_finishes(self, path, fallback_content=None, generated=False):
        try:
            await release.wait()
        finally:
            finished.set()

    runner._sync_file_to_storage = MethodType(never_finishes, runner)
    event = ToolEvent(
        tool_call_id="tool-timeout",
        tool_name="shell",
        function_name="shell_exec",
        function_args={
            "id": "shell-timeout",
            "command": "touch /home/ubuntu/upload/report.md",
        },
        status=ToolStatus.CALLED,
    )

    await asyncio.wait_for(runner._sync_shell_artifacts(event, None), timeout=0.2)

    await asyncio.wait_for(finished.wait(), timeout=0.1)


async def test_shell_artifact_sync_uses_one_total_timeout_budget():
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._ARTIFACT_SYNC_TIMEOUT_SECONDS = 0.025
    runner._MAX_AUTO_ARTIFACT_CANDIDATES = 32
    attempted = []

    async def slow_sync(self, path, fallback_content=None, generated=False):
        attempted.append(path)
        await asyncio.sleep(0.02)

    runner._sync_file_to_storage = MethodType(slow_sync, runner)
    event = ToolEvent(
        tool_call_id="tool-total-timeout",
        tool_name="shell",
        function_name="shell_exec",
        function_args={
            "id": "shell-total-timeout",
            "command": " ".join(
                f"/home/ubuntu/upload/report-{index}.md"
                for index in range(5)
            ),
        },
        status=ToolStatus.CALLED,
    )

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(runner._sync_shell_artifacts(event, None), timeout=0.1)
    elapsed = asyncio.get_running_loop().time() - started

    assert 1 <= len(attempted) < 5
    assert elapsed < 0.1


async def test_shell_artifact_sync_caps_candidate_count():
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._ARTIFACT_SYNC_TIMEOUT_SECONDS = 1
    runner._MAX_AUTO_ARTIFACT_CANDIDATES = 2
    attempted = []

    async def record_sync(self, path, fallback_content=None, generated=False):
        attempted.append(path)

    runner._sync_file_to_storage = MethodType(record_sync, runner)
    event = ToolEvent(
        tool_call_id="tool-candidate-limit",
        tool_name="shell",
        function_name="shell_exec",
        function_args={
            "id": "shell-candidate-limit",
            "command": " ".join(
                f"/home/ubuntu/upload/report-{index}.md"
                for index in range(5)
            ),
        },
        status=ToolStatus.CALLED,
    )

    await runner._sync_shell_artifacts(event, None)

    assert attempted == [
        "/home/ubuntu/upload/report-0.md",
        "/home/ubuntu/upload/report-1.md",
    ]


async def test_message_artifact_sync_timeout_is_fail_open_and_keeps_generated_files():
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._ARTIFACT_SYNC_TIMEOUT_SECONDS = 0.01
    runner._MAX_AUTO_ARTIFACT_CANDIDATES = 32
    runner._synced_artifacts = {}
    runner._generated_artifacts = {
        "/home/ubuntu/upload/already-synced.md": FileInfo(
            file_id="stored-generated",
            filename="already-synced.md",
            file_path="/home/ubuntu/upload/already-synced.md",
        )
    }
    release = asyncio.Event()
    finished = asyncio.Event()

    async def never_finishes(self, path, fallback_content=None, generated=False):
        try:
            await release.wait()
        finally:
            finished.set()

    runner._sync_file_to_storage = MethodType(never_finishes, runner)
    event = MessageEvent(
        message="See /home/ubuntu/upload/missing.md",
        attachments=[],
    )

    await asyncio.wait_for(
        runner._sync_message_attachments_to_storage(event),
        timeout=0.2,
    )

    await asyncio.wait_for(finished.wait(), timeout=0.1)
    assert [attachment.file_id for attachment in event.attachments] == [
        "stored-generated"
    ]


async def test_message_artifact_sync_caps_candidate_count():
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._ARTIFACT_SYNC_TIMEOUT_SECONDS = 1
    runner._MAX_AUTO_ARTIFACT_CANDIDATES = 2
    runner._synced_artifacts = {}
    runner._generated_artifacts = {}
    attempted = []

    async def record_sync(self, path, fallback_content=None, generated=False):
        attempted.append(path)
        return None

    runner._sync_file_to_storage = MethodType(record_sync, runner)
    event = MessageEvent(
        message=" ".join(
            f"/home/ubuntu/upload/message-{index}.md"
            for index in range(5)
        ),
        attachments=[],
    )

    await runner._sync_message_attachments_to_storage(event)

    assert attempted == [
        "/home/ubuntu/upload/message-0.md",
        "/home/ubuntu/upload/message-1.md",
    ]


async def test_shell_tool_preview_timeout_is_fail_open():
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._ARTIFACT_SYNC_TIMEOUT_SECONDS = 0.01
    cancelled = asyncio.Event()

    class Sandbox:
        async def view_shell(self, session_id, console=False):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    runner._sandbox = Sandbox()
    event = ToolEvent(
        tool_call_id="shell-preview-timeout",
        tool_name="shell",
        function_name="shell_exec",
        function_args={"id": "shell-1", "command": "echo done"},
        status=ToolStatus.CALLED,
    )

    await asyncio.wait_for(runner._handle_tool_event(event), timeout=0.1)

    await asyncio.wait_for(cancelled.wait(), timeout=0.1)
    assert event.tool_content.console == "(Console preview timed out)"


async def test_browser_screenshot_timeout_is_fail_open():
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._ARTIFACT_SYNC_TIMEOUT_SECONDS = 0.01
    cancelled = asyncio.Event()

    async def slow_screenshot(self):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runner._get_browser_screenshot = MethodType(slow_screenshot, runner)
    event = ToolEvent(
        tool_call_id="browser-preview-timeout",
        tool_name="browser",
        function_name="browser_view",
        function_args={},
        status=ToolStatus.CALLED,
    )

    await asyncio.wait_for(runner._handle_tool_event(event), timeout=0.1)

    await asyncio.wait_for(cancelled.wait(), timeout=0.1)
    assert event.tool_content.screenshot == ""


async def test_read_only_file_tool_does_not_upload_artifact_again():
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._ARTIFACT_SYNC_TIMEOUT_SECONDS = 0.05

    class Sandbox:
        async def file_read(self, path):
            raise AssertionError("function result content should be reused")

    async def must_not_sync(*args, **kwargs):
        raise AssertionError("read-only file tools must not upload artifacts")

    runner._sandbox = Sandbox()
    runner._sync_file_to_storage = must_not_sync
    event = ToolEvent(
        tool_call_id="file-read",
        tool_name="file",
        function_name="file_read",
        function_args={"file": "/home/ubuntu/readme.md"},
        function_result=ToolResult(
            success=True,
            message="ok",
            data={"content": "already returned"},
        ),
        status=ToolStatus.CALLED,
    )

    await runner._handle_tool_event(event)

    assert event.tool_content.content == "already returned"


async def test_artifact_deadline_does_not_cancel_inflight_atomic_publish():
    publish_started = asyncio.Event()
    release_publish = asyncio.Event()

    class Sandbox:
        async def file_find(self, path, glob_pattern):
            return type(
                "Result",
                (),
                {"data": {"files": [f"{path}/report.md"]}},
            )()

        async def file_download(self, path):
            return io.BytesIO(b"report")

    class SessionRepository:
        current = None
        publish_cancelled = False

        async def get_file_by_path(self, session_id, file_path):
            return None

        async def upsert_file_by_path(self, session_id, file_info):
            publish_started.set()
            try:
                await release_publish.wait()
            except asyncio.CancelledError:
                self.publish_cancelled = True
                raise
            self.current = file_info
            return None

    class FileStorage:
        async def upload_file(
            self,
            file_data,
            file_name,
            user_id,
            metadata=None,
        ):
            return FileInfo(
                file_id="new-file",
                filename=file_name,
                metadata=metadata,
            )

    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._session_id = "session"
    runner._user_id = "user"
    runner._sandbox = Sandbox()
    runner._session_repository = SessionRepository()
    runner._file_storage = FileStorage()
    runner._generated_artifacts = {}
    runner._synced_artifacts = {}
    runner._artifact_cleanup_tasks = set()

    deadline = asyncio.get_running_loop().time() + 0.01
    file_info, timed_out = await runner._sync_auto_artifact_before_deadline(
        "/home/ubuntu/upload/report.md",
        deadline,
        source="test",
        generated=True,
    )

    assert (file_info, timed_out) == (None, True)
    await asyncio.wait_for(publish_started.wait(), timeout=0.1)
    assert runner._session_repository.publish_cancelled is False
    release_publish.set()
    for _ in range(100):
        if not runner._artifact_cleanup_tasks:
            break
        await asyncio.sleep(0.01)

    assert runner._session_repository.current.file_id == "new-file"
    assert runner._artifact_cleanup_tasks == set()


async def test_browser_deadline_does_not_cancel_inflight_session_publish():
    publish_started = asyncio.Event()
    release_publish = asyncio.Event()

    class Browser:
        async def screenshot(self):
            return b"png"

    class FileStorage:
        async def upload_file(self, *args, **kwargs):
            return FileInfo(file_id="screenshot-file", filename="screenshot.png")

    class SessionRepository:
        published = None
        publish_cancelled = False

        async def add_file(self, session_id, file_info):
            publish_started.set()
            try:
                await release_publish.wait()
            except asyncio.CancelledError:
                self.publish_cancelled = True
                raise
            self.published = file_info

    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._session_id = "session"
    runner._user_id = "user"
    runner._browser = Browser()
    runner._file_storage = FileStorage()
    runner._session_repository = SessionRepository()
    runner._artifact_cleanup_tasks = set()
    runner._ARTIFACT_SYNC_TIMEOUT_SECONDS = 0.01
    event = ToolEvent(
        tool_call_id="browser-publish",
        tool_name="browser",
        function_name="browser_view",
        function_args={},
        status=ToolStatus.CALLED,
    )

    await asyncio.wait_for(runner._handle_tool_event(event), timeout=0.1)

    await asyncio.wait_for(publish_started.wait(), timeout=0.1)
    assert runner._session_repository.publish_cancelled is False
    release_publish.set()
    for _ in range(100):
        if not runner._artifact_cleanup_tasks:
            break
        await asyncio.sleep(0.01)

    assert runner._session_repository.published.file_id == "screenshot-file"
    assert runner._artifact_cleanup_tasks == set()
