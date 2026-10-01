"""MCP tool names must route exactly and refresh when a user's config changes."""

from types import SimpleNamespace
import re

from app.domain.models.mcp_config import MCPConfig, MCPServerConfig, MCPTransport
from app.domain.services.tools.mcp import MCPClientManager, MCPToolkit


def _remote_tool(name):
    return SimpleNamespace(name=name, description=name, inputSchema={"type": "object"})


class _Session:
    def __init__(self, name):
        self.name = name
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(content=[SimpleNamespace(text=f"{self.name}:{name}")])


async def test_mcp_tool_route_uses_exact_generated_name_with_prefix_servers():
    manager = MCPClientManager(MCPConfig(mcpServers={}))
    manager._tools_cache = {
        "foo": [_remote_tool("read")],
        "foo_bar": [_remote_tool("read")],
    }
    foo = _Session("foo")
    foo_bar = _Session("foo_bar")
    manager._clients = {"foo": foo, "foo_bar": foo_bar}

    schemas = await manager.get_all_tools()
    assert {schema["function"]["name"] for schema in schemas} == {
        "mcp_foo_read", "mcp_foo_bar_read",
    }
    result = await manager.call_tool("mcp_foo_bar_read", {"query": "x"})
    assert result.success and result.data == "foo_bar:read"
    assert foo.calls == []
    assert foo_bar.calls == [("read", {"query": "x"})]


async def test_mcp_generated_name_collision_rejects_both_schemas_and_invocations():
    manager = MCPClientManager(MCPConfig(mcpServers={}))
    manager._tools_cache = {
        "foo_bar": [_remote_tool("baz")],
        "foo": [_remote_tool("bar_baz")],
    }
    first = _Session("foo_bar")
    second = _Session("foo")
    manager._clients = {"foo_bar": first, "foo": second}

    schemas = await manager.get_all_tools()
    assert "mcp_foo_bar_baz" not in {schema["function"]["name"] for schema in schemas}
    result = await manager.call_tool("mcp_foo_bar_baz", {})
    assert not result.success
    assert "ambiguous" in result.message
    assert first.calls == second.calls == []


async def test_mcp_toolkit_refreshes_same_instance_after_enable_disable(monkeypatch):
    managers = []

    class FakeManager:
        def __init__(self, config):
            self.config = config
            self.closed = False
            managers.append(self)

        async def initialize(self):
            pass

        async def get_all_tools(self):
            if not self.config.mcpServers["docs"].enabled:
                return []
            return [{"type": "function", "function": {
                "name": "mcp_docs_read", "description": "Read docs",
                "parameters": {"type": "object"},
            }}]

        async def cleanup(self):
            self.closed = True

    monkeypatch.setattr("app.domain.services.tools.mcp.MCPClientManager", FakeManager)
    toolkit = MCPToolkit()
    def config(enabled):
        return MCPConfig(mcpServers={
            "docs": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                url="https://mcp.example.com/mcp", enabled=enabled,
            )
        })

    await toolkit.initialized(config(True))
    assert {tool.name for tool in toolkit.tools} == {"mcp_docs_read"}
    await toolkit.initialized(config(True))
    assert len(managers) == 1

    await toolkit.initialized(config(False))
    assert managers[0].closed
    assert toolkit.tools == []
    await toolkit.initialized(config(True))
    assert managers[1].closed
    assert {tool.name for tool in toolkit.tools} == {"mcp_docs_read"}


async def test_long_unicode_mcp_name_uses_stable_legal_alias_and_routes_original():
    server_name = "知识库" + "very_long_server_name_" * 4
    original_name = "搜索资料📚" + "very_long_tool_name_" * 4
    manager = MCPClientManager(MCPConfig(mcpServers={}))
    manager._tools_cache = {server_name: [_remote_tool(original_name)]}
    session = _Session("unicode")
    manager._clients = {server_name: session}

    schemas = await manager.get_all_tools()
    assert len(schemas) == 1
    alias = schemas[0]["function"]["name"]
    assert len(alias) <= 64
    assert re.fullmatch(r"[A-Za-z0-9_-]+", alias)
    assert [s["function"]["name"] for s in await manager.get_all_tools()] == [alias]
    result = await manager.call_tool(alias, {"q": "hello"})
    assert result.success
    assert session.calls == [(original_name, {"q": "hello"})]
