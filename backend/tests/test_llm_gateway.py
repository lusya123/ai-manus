"""Unit tests for the LangChain LLM gateway message translation.

Ensures the infrastructure gateway correctly converts between domain
:class:`LLMMessage` objects and LangChain message objects in both directions,
which is the boundary that keeps LangChain out of the domain.
"""
from langchain.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage

from app.core.config import Settings
from app.domain.models.message import LLMMessage, Role, ToolCall
from app.infrastructure.external.llm.langchain_llm import (
    ORCAROUTER_API_BASE,
    LangchainLLM,
)


def _gateway() -> LangchainLLM:
    # init_chat_model only constructs the client; no network call is made here.
    # Explicit Settings so the test never depends on host env vars / dotenv
    # (API_KEY, API_BASE). api_base=None beats pydantic env_file=".env".
    return LangchainLLM(settings=Settings(api_key="test", api_base=None))


class TestToLangChain:
    def test_all_roles_converted(self):
        gw = _gateway()
        msgs = [
            LLMMessage.system("sys"),
            LLMMessage.user("hi"),
            LLMMessage.assistant("", tool_calls=[ToolCall(id="c1", name="shell_exec", args={"cmd": "ls"})]),
            LLMMessage.tool(tool_call_id="c1", name="shell_exec", content="{}"),
        ]
        lc = gw._to_langchain(msgs)
        assert isinstance(lc[0], SystemMessage)
        assert isinstance(lc[1], HumanMessage)
        assert isinstance(lc[2], AIMessage)
        assert lc[2].tool_calls[0]["name"] == "shell_exec"
        assert lc[2].tool_calls[0]["args"] == {"cmd": "ls"}
        assert isinstance(lc[3], ToolMessage)
        assert lc[3].tool_call_id == "c1"


class TestFromLangChain:
    def test_ai_message_with_tool_calls(self):
        gw = _gateway()
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "file_read", "args": {"file": "/a"}, "id": "c2", "type": "tool_call"}],
        )
        m = gw._from_langchain(ai)
        assert m.role == Role.ASSISTANT
        assert m.tool_calls[0].name == "file_read"
        assert m.tool_calls[0].args == {"file": "/a"}
        assert m.tool_calls[0].id == "c2"

    def test_plain_ai_message(self):
        gw = _gateway()
        m = gw._from_langchain(AIMessage(content="hello"))
        assert m.role == Role.ASSISTANT and m.content == "hello" and m.tool_calls == []


class TestRoundTrip:
    def test_domain_to_lc_to_domain_preserves_tool_calls(self):
        gw = _gateway()
        original = LLMMessage.assistant(
            "text", tool_calls=[ToolCall(id="c3", name="info_search_web", args={"query": "x"})]
        )
        lc = gw._to_langchain([original])[0]
        back = gw._from_langchain(lc)
        assert back.content == "text"
        assert back.tool_calls[0].name == "info_search_web"
        assert back.tool_calls[0].args == {"query": "x"}
        assert back.tool_calls[0].id == "c3"


class TestOrcaRouterProvider:
    """OrcaRouter is wired as a named provider backed by ChatOpenAI."""

    def test_orcarouter_defaults_to_orca_base_url(self):
        gw = LangchainLLM(
            settings=Settings(
                api_key="test",
                model_provider="orcarouter",
                model_name="anthropic/claude-sonnet-4.5",
                api_base=None,
            )
        )
        assert gw._model.openai_api_base == ORCAROUTER_API_BASE
        assert gw._model.model_name == "anthropic/claude-sonnet-4.5"

    def test_orcarouter_respects_explicit_api_base(self):
        gw = LangchainLLM(
            settings=Settings(
                api_key="test",
                model_provider="orcarouter",
                model_name="anthropic/claude-sonnet-4.5",
                api_base="https://selfhost.example.com/v1",
            )
        )
        assert gw._model.openai_api_base == "https://selfhost.example.com/v1"


class _StreamingModel:
    def __init__(self, chunks):
        self.chunks = chunks

    def bind(self, **kwargs):
        return self

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


def _streaming_gateway(chunks):
    gateway = object.__new__(LangchainLLM)
    gateway._model_provider = "openai"
    gateway._model = _StreamingModel(chunks)
    return gateway


async def test_streaming_gateway_hides_thinking_before_visible_text():
    gateway = _streaming_gateway([
        AIMessageChunk(content="<thinking>private reasoning</thinking>Hello"),
        AIMessageChunk(content=" world"),
    ])

    chunks = [chunk async for chunk in gateway.ask_stream([LLMMessage.user("Hi")])]

    assert [chunk.text for chunk in chunks if chunk.message is None] == ["Hello", "Hello world"]
    assert all("private reasoning" not in chunk.text for chunk in chunks)
    assert chunks[-1].message.content == "Hello world"


async def test_stream_failure_resets_preview_before_retry_result():
    gateway = _streaming_gateway([
        AIMessageChunk(content="Partial answer"),
        RuntimeError("test stream disconnect"),
    ])
    async def retry(messages, tools=None, response_format=None, tool_choice=None):
        return LLMMessage.assistant("Final answer")
    gateway.ask = retry

    chunks = [chunk async for chunk in gateway.ask_stream([LLMMessage.user("Hi")])]

    assert chunks[0].text == "Partial answer"
    assert chunks[1].reset is True
    assert chunks[2].message.content == "Final answer"
