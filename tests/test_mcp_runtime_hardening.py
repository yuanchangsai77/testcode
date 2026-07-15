from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from testcode.mcp.adapter import (
    MAX_MCP_TOOL_RESULT_CHARS,
    MCPToolAdapter,
    build_stable_resource_id,
    build_stable_tool_name,
    map_mcp_tool_risk,
)
from testcode.mcp.client import (
    MCPClientError,
    TransportBackedMCPClient,
    _extract_result,
    _flatten_content_blocks,
)
from testcode.mcp.config import MCPServerConfig, _build_server_config
from testcode.mcp.discovery import (
    MAX_MCP_DESCRIPTOR_CHARS,
    MAX_MCP_TOOLS_PER_SERVER,
    MCPDiscoveryService,
)
from testcode.mcp.manager import MCPManager
from testcode.mcp.provider import MAX_MCP_RESOURCE_CHARS, MCPResourceProvider
from testcode.mcp.transport import MCPHTTPError
from testcode.mcp.types import (
    MCPDiscoverySnapshot,
    MCPResourceDescriptor,
    MCPToolCallResult,
    MCPToolDescriptor,
)
from testcode.observability.logger import InMemoryLogger
from testcode.safety.policy import DefaultPolicy
from testcode.safety.redaction import redact_text
from testcode.tools.base import SimpleTool
from testcode.tools.registry import ToolRegistry
from testcode.types import ToolAction, ToolDefinition, ToolResult, UserRequest


def test_unknown_mcp_tools_require_confirmation_and_untrusted_annotations_cannot_lower_risk():
    server = MCPServerConfig(name="remote", transport="stdio", command="server")
    unknown = MCPToolDescriptor(server_name="remote", tool_name="ping")
    read_only = MCPToolDescriptor(
        server_name="remote",
        tool_name="lookup",
        annotations={"readOnlyHint": True},
    )

    assert map_mcp_tool_risk(server, unknown) == "confirm"
    assert map_mcp_tool_risk(server, read_only) == "confirm"
    trusted_override = MCPServerConfig(
        name="remote",
        transport="stdio",
        command="server",
        risk_overrides={"lookup": "read"},
    )
    assert map_mcp_tool_risk(trusted_override, read_only) == "read"
    decision = DefaultPolicy(mode="confirm").evaluate(
        ToolAction(name="remote__ping"),
        ToolDefinition(name="remote__ping", description="ping", risk_level="confirm"),
    )
    assert decision.requires_confirmation is True


@pytest.mark.parametrize(
    "response",
    [
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": -1, "message": "bad"}},
    ],
)
def test_client_rejects_responses_without_exactly_one_result_or_error(response):
    with pytest.raises(MCPClientError) as error:
        _extract_result(response)

    assert error.value.error_code == "mcp_protocol_error"


@pytest.mark.parametrize("jsonrpc", [None, "1.0"])
def test_client_rejects_non_jsonrpc_2_responses(jsonrpc):
    response = {"id": 1, "result": {}}
    if jsonrpc is not None:
        response["jsonrpc"] = jsonrpc

    with pytest.raises(MCPClientError) as error:
        _extract_result(response)

    assert error.value.error_code == "mcp_protocol_error"


def test_mcp_tool_names_are_safe_for_model_function_tools_and_stable():
    server = MCPServerConfig(name="remote.server", transport="stdio", command="server")

    dotted = build_stable_tool_name(server, "admin.tools.list")
    similar = build_stable_tool_name(server, "admin_tools_list")
    long_name = build_stable_tool_name(server, "x" * 100)

    assert dotted == build_stable_tool_name(server, "admin.tools.list")
    assert dotted != similar
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", dotted)
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", long_name)


@pytest.mark.parametrize(
    "raw",
    [
        {"name": "bad", "transport": "websocket", "url": "https://example.test"},
        {"name": "bad", "transport": "stdio"},
        {"name": "bad", "transport": "sse"},
        {
            "name": "bad",
            "transport": "stdio",
            "command": "server",
            "risk_overrides": {"tool": "unsafe"},
        },
    ],
)
def test_invalid_mcp_configs_are_rejected(raw):
    with pytest.raises(ValueError):
        _build_server_config(raw)


def test_resource_discovery_failure_does_not_discard_tools():
    logger = InMemoryLogger()
    config = MCPServerConfig(name="tools-only", transport="stdio", command="server")

    class Client:
        def initialize(self):
            pass

        def list_tools(self):
            return (MCPToolDescriptor(server_name="tools-only", tool_name="ping"),)

        def list_resources(self):
            raise RuntimeError("resources unsupported")

        def close(self):
            pass

    manager = MCPManager({"tools-only": config}, lambda _config: Client(), logger=logger)
    snapshot = MCPDiscoveryService(manager, logger=logger).refresh("tools-only")

    assert [tool.tool_name for tool in snapshot.tools] == ["ping"]
    assert snapshot.resources == ()
    assert "resources unsupported" in snapshot.error_message
    assert any(event.name == "mcp.resource.error" for event in logger.events)


def test_discovery_invalidates_failed_client_and_recovers_on_refresh():
    config = MCPServerConfig(name="recovering", transport="stdio", command="server")
    created = []

    class Client:
        def __init__(self, should_fail):
            self.should_fail = should_fail
            self.closed = False

        def initialize(self):
            pass

        def list_tools(self):
            if self.should_fail:
                raise RuntimeError("server crashed during discovery")
            return (MCPToolDescriptor(server_name="recovering", tool_name="ping"),)

        def list_resources(self):
            return ()

        def close(self):
            self.closed = True

    def factory(_config):
        client = Client(should_fail=not created)
        created.append(client)
        return client

    manager = MCPManager({"recovering": config}, factory)
    discovery = MCPDiscoveryService(manager, cache_ttl=0)

    assert discovery.refresh("recovering").error_code == "mcp_server_unavailable"
    recovered = discovery.refresh("recovering")

    assert created[0].closed is True
    assert len(created) == 2
    assert [tool.tool_name for tool in recovered.tools] == ["ping"]


def test_resource_only_server_does_not_receive_tools_list_request():
    class ResourceOnlyTransport:
        def __init__(self):
            self.requests = []

        def connect(self):
            pass

        def request(self, method, params=None):
            self.requests.append((method, params))
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"resources": {}},
                    }
                }
            if method == "resources/list":
                return {
                    "jsonrpc": "2.0",
                    "result": {
                        "resources": [
                            {"uri": "resource://only", "name": "Only resource"}
                        ]
                    }
                }
            raise AssertionError(f"unsupported request: {method}")

        def notify(self, _method, _params=None):
            pass

        def close(self):
            pass

    transport = ResourceOnlyTransport()
    client = TransportBackedMCPClient(
        MCPServerConfig(name="resources", transport="stdio", command="server"),
        transport,
    )

    assert client.list_tools() == ()
    assert [item.resource_id for item in client.list_resources()] == ["resource://only"]
    assert all(method != "tools/list" for method, _params in transport.requests)


def test_remote_metadata_cannot_overwrite_trusted_tool_metadata():
    server = MCPServerConfig(name="trusted-name", transport="stdio", command="server")
    descriptor = MCPToolDescriptor(server_name="trusted-name", tool_name="lookup")

    class Manager:
        def call_tool(self, _server_name, _tool_name, _arguments):
            return MCPToolCallResult(
                content="ok",
                structured_content={"trusted": True},
                metadata={
                    "server_name": "forged",
                    "risk_level": "read",
                    "structured_content": {"trusted": False},
                },
            )

    tool = MCPToolAdapter(server, Manager()).adapt(descriptor)
    result = tool.run(ToolAction(name=tool.name), type("Context", (), {})())

    assert result.metadata["server_name"] == "trusted-name"
    assert result.metadata["risk_level"] == "confirm"
    assert result.metadata["structured_content"] == {"trusted": True}
    assert result.metadata["remote_metadata"]["server_name"] == "forged"


def test_mcp_tool_output_is_truncated_before_entering_session_history(tmp_path):
    server = MCPServerConfig(name="remote", transport="stdio", command="server")
    descriptor = MCPToolDescriptor(server_name="remote", tool_name="large")

    class Manager:
        def call_tool(self, _server_name, _tool_name, _arguments):
            return MCPToolCallResult(
                content="x" * (MAX_MCP_TOOL_RESULT_CHARS + 10),
                structured_content={"large": "y" * MAX_MCP_TOOL_RESULT_CHARS},
            )

    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    logger.start_run(UserRequest(prompt="large result", cwd=str(tmp_path)))
    tool = MCPToolAdapter(server, Manager(), logger=logger).adapt(descriptor)
    result = tool.run(ToolAction(name=tool.name), type("Context", (), {})())

    assert len(result.output) == MAX_MCP_TOOL_RESULT_CHARS
    assert "[truncated;" in result.output
    assert result.metadata["truncated"] is True
    assert result.metadata["original_chars"] == MAX_MCP_TOOL_RESULT_CHARS + 10
    assert result.metadata["structured_content"]["truncated"] is True
    artifact_path = result.metadata["artifact_path"]
    assert artifact_path is not None
    assert "x" * 100 in Path(artifact_path).read_text(encoding="utf-8")


def test_discovery_bounds_tool_count_and_descriptor_size():
    logger = InMemoryLogger()
    config = MCPServerConfig(name="bounded", transport="stdio", command="server")
    normal_tools = tuple(
        MCPToolDescriptor(server_name="bounded", tool_name=f"tool-{index}")
        for index in range(MAX_MCP_TOOLS_PER_SERVER + 1)
    )
    oversized = MCPToolDescriptor(
        server_name="bounded",
        tool_name="oversized",
        description="x" * (MAX_MCP_DESCRIPTOR_CHARS + 1),
    )

    class Client:
        def initialize(self):
            pass

        def list_tools(self):
            return (*normal_tools, oversized)

        def list_resources(self):
            return ()

        def close(self):
            pass

    manager = MCPManager({"bounded": config}, lambda _config: Client())
    snapshot = MCPDiscoveryService(manager, logger=logger).refresh("bounded")

    assert len(snapshot.tools) == MAX_MCP_TOOLS_PER_SERVER
    assert all(tool.tool_name != "oversized" for tool in snapshot.tools)
    assert any(event.name == "mcp.tools.truncated" for event in logger.events)


def test_discovery_cache_is_reused_without_connecting(tmp_path):
    config = MCPServerConfig(name="cached", transport="stdio", command="server")

    class Client:
        def initialize(self):
            pass

        def list_tools(self):
            return (MCPToolDescriptor(server_name="cached", tool_name="ping"),)

        def list_resources(self):
            return ()

        def close(self):
            pass

    cache_path = tmp_path / "mcp-cache.json"
    first_manager = MCPManager({"cached": config}, lambda _config: Client())
    first = MCPDiscoveryService(first_manager, cache_path=cache_path)
    assert first.get_snapshot("cached").tools[0].tool_name == "ping"

    def must_not_connect(_config):
        raise AssertionError("fresh cache should avoid connection")

    second_manager = MCPManager({"cached": config}, must_not_connect)
    second = MCPDiscoveryService(second_manager, cache_path=cache_path)
    assert second.get_snapshot("cached").tools[0].tool_name == "ping"


def test_invalid_discovery_cache_shape_is_ignored(tmp_path):
    cache_path = tmp_path / "mcp-cache.json"
    cache_path.write_text("[]", encoding="utf-8")
    config = MCPServerConfig(name="cached", transport="stdio", command="server")
    manager = MCPManager({"cached": config}, lambda _config: None)

    discovery = MCPDiscoveryService(manager, cache_path=cache_path)

    assert discovery._snapshots == {}


def test_discovery_cache_reapplies_descriptor_bounds(tmp_path):
    cache_path = tmp_path / "mcp-cache.json"
    tools = [
        {
            "server_name": "cached",
            "tool_name": "oversized",
            "description": "x" * (MAX_MCP_DESCRIPTOR_CHARS + 1),
        },
        *[
            {"server_name": "cached", "tool_name": f"tool-{index}"}
            for index in range(MAX_MCP_TOOLS_PER_SERVER)
        ],
    ]
    cache_path.write_text(
        json.dumps(
            {
                "cached": {
                    "refreshed_at": time.time(),
                    "tools": tools,
                    "resources": [],
                }
            }
        ),
        encoding="utf-8",
    )
    config = MCPServerConfig(name="cached", transport="stdio", command="server")
    logger = InMemoryLogger()

    discovery = MCPDiscoveryService(
        MCPManager({"cached": config}, lambda _config: None),
        logger=logger,
        cache_path=cache_path,
    )

    assert len(discovery._snapshots["cached"].tools) == MAX_MCP_TOOLS_PER_SERVER
    assert all(tool.tool_name != "oversized" for tool in discovery._snapshots["cached"].tools)
    assert any(event.name == "mcp.tools.truncated" for event in logger.events)


def test_oversized_discovery_cache_is_ignored(tmp_path, monkeypatch):
    cache_path = tmp_path / "mcp-cache.json"
    cache_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("testcode.mcp.discovery.MAX_MCP_DISCOVERY_CACHE_BYTES", 1)
    config = MCPServerConfig(name="cached", transport="stdio", command="server")

    discovery = MCPDiscoveryService(
        MCPManager({"cached": config}, lambda _config: None),
        cache_path=cache_path,
    )

    assert discovery._snapshots == {}


@pytest.mark.parametrize("error_code", ["mcp_transport_closed", "mcp_protocol_error"])
def test_manager_does_not_replay_tool_after_ambiguous_client_failure(error_code):
    config = MCPServerConfig(name="remote", transport="stdio", command="server")
    clients = []

    class Client:
        def __init__(self, succeeds):
            self.succeeds = succeeds
            self.closed = False

        def initialize(self):
            pass

        def call_tool(self, _name, _arguments):
            if self.succeeds:
                return MCPToolCallResult(content="ok")
            return MCPToolCallResult(
                content="failed", is_error=True, error_code=error_code
            )

        def close(self):
            self.closed = True

    def factory(_config):
        client = Client(succeeds=bool(clients))
        clients.append(client)
        return client

    manager = MCPManager({"remote": config}, factory)
    result = manager.call_tool("remote", "ping", {})

    assert result.is_error is True
    assert result.error_code == error_code
    assert len(clients) == 1
    assert clients[0].closed is True

    recovered = manager.call_tool("remote", "ping", {})

    assert recovered.content == "ok"
    assert len(clients) == 2


@pytest.mark.parametrize("error_code", ["mcp_transport_closed", "mcp_protocol_error"])
def test_manager_invalidates_client_after_resource_client_failure(error_code):
    config = MCPServerConfig(name="remote", transport="stdio", command="server")
    clients = []

    class ResourceFailure(RuntimeError):
        pass

    ResourceFailure.error_code = error_code

    class Client:
        def __init__(self, succeeds):
            self.succeeds = succeeds
            self.closed = False

        def initialize(self):
            pass

        def read_resource(self, _resource_id):
            if not self.succeeds:
                raise ResourceFailure("closed")
            return "recovered"

        def close(self):
            self.closed = True

    def factory(_config):
        client = Client(succeeds=bool(clients))
        clients.append(client)
        return client

    manager = MCPManager({"remote": config}, factory)

    with pytest.raises(ResourceFailure):
        manager.read_resource("remote", "resource://docs")

    assert clients[0].closed is True
    assert manager.read_resource("remote", "resource://docs") == "recovered"
    assert len(clients) == 2


def test_client_follows_tool_and_resource_pagination():
    class PaginatedTransport:
        def __init__(self):
            self.requests = []

        def connect(self):
            pass

        def request(self, method, params=None):
            params = params or {}
            self.requests.append((method, dict(params)))
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}, "resources": {}},
                    }
                }
            if method == "tools/list":
                if params.get("cursor") is None:
                    return {"jsonrpc": "2.0", "result": {"tools": [{"name": "first"}], "nextCursor": "tools-2"}}
                return {"jsonrpc": "2.0", "result": {"tools": [{"name": "second"}]}}
            if method == "resources/list":
                if params.get("cursor") is None:
                    return {
                        "jsonrpc": "2.0",
                        "result": {
                            "resources": [{"uri": "resource://first", "name": "First"}],
                            "nextCursor": "resources-2",
                        }
                    }
                return {"jsonrpc": "2.0", "result": {"resources": [{"uri": "resource://second", "name": "Second"}]}}
            raise AssertionError(method)

        def notify(self, _method, _params=None):
            pass

        def close(self):
            pass

    transport = PaginatedTransport()
    client = TransportBackedMCPClient(
        config=MCPServerConfig(name="paged", transport="stdio", command="server"),
        transport=transport,
    )

    assert [tool.tool_name for tool in client.list_tools()] == ["first", "second"]
    assert [resource.resource_id for resource in client.list_resources()] == [
        "resource://first",
        "resource://second",
    ]
    assert ("tools/list", {"cursor": "tools-2"}) in transport.requests
    assert ("resources/list", {"cursor": "resources-2"}) in transport.requests


def test_client_stops_paginating_after_tool_limit_overflow():
    class Transport:
        def __init__(self):
            self.tool_pages = 0

        def connect(self):
            pass

        def request(self, method, params=None):
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                    },
                }
            if method == "tools/list":
                self.tool_pages += 1
                start = (self.tool_pages - 1) * 200
                return {
                    "jsonrpc": "2.0",
                    "result": {
                        "tools": [
                            {"name": f"tool-{index}"}
                            for index in range(start, start + 200)
                        ],
                        "nextCursor": f"page-{self.tool_pages + 1}",
                    },
                }
            raise AssertionError(method)

        def notify(self, _method, _params=None):
            pass

        def close(self):
            pass

    transport = Transport()
    client = TransportBackedMCPClient(
        MCPServerConfig(name="bounded", transport="stdio", command="server"),
        transport,
    )

    tools = client.list_tools()

    assert len(tools) == MAX_MCP_TOOLS_PER_SERVER + 1
    assert transport.tool_pages == 2


def test_client_rejects_endless_empty_pagination(monkeypatch):
    monkeypatch.setattr("testcode.mcp.client.MAX_MCP_DISCOVERY_PAGES", 3)

    class Transport:
        def connect(self):
            pass

        def request(self, method, params=None):
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                    },
                }
            return {
                "jsonrpc": "2.0",
                "result": {"tools": [], "nextCursor": str(params or {})},
            }

        def notify(self, _method, _params=None):
            pass

        def close(self):
            pass

    client = TransportBackedMCPClient(
        MCPServerConfig(name="endless", transport="stdio", command="server"),
        Transport(),
    )

    with pytest.raises(MCPClientError, match="pagination limit"):
        client.list_tools()


def test_client_does_not_replay_tool_when_streamable_http_session_expires():
    class ExpiringTransport:
        def __init__(self):
            self.initialize_count = 0
            self.tool_call_count = 0
            self.close_count = 0

        def connect(self):
            pass

        def request(self, method, _params=None):
            if method == "initialize":
                self.initialize_count += 1
                return {
                    "jsonrpc": "2.0",
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                    }
                }
            if method == "tools/call":
                self.tool_call_count += 1
                if self.tool_call_count == 1:
                    raise MCPHTTPError("expired", status=404)
                return {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "recovered"}]}}
            raise AssertionError(method)

        def notify(self, _method, _params=None):
            pass

        def close(self):
            self.close_count += 1

    transport = ExpiringTransport()
    client = TransportBackedMCPClient(
        config=MCPServerConfig(
            name="remote",
            transport="streamable_http",
            url="http://example.test/mcp",
        ),
        transport=transport,
    )

    result = client.call_tool("search", {})

    assert result.is_error is True
    assert result.error_code == "mcp_http_error"
    assert transport.initialize_count == 1
    assert transport.tool_call_count == 1
    assert transport.close_count == 1

    recovered = client.call_tool("search", {})

    assert recovered.is_error is False
    assert recovered.content == "recovered"
    assert transport.initialize_count == 2
    assert transport.tool_call_count == 2


@pytest.mark.parametrize("reported_version", [None, "2099-01-01"])
def test_client_rejects_missing_or_unsupported_protocol_versions(reported_version):
    class Transport:
        def __init__(self):
            self.closed = False
            self.notifications = []

        def connect(self):
            pass

        def request(self, _method, _params=None):
            result = {"capabilities": {"tools": {}}}
            if reported_version is not None:
                result["protocolVersion"] = reported_version
            return {"jsonrpc": "2.0", "result": result}

        def notify(self, method, params=None):
            self.notifications.append((method, params))

        def close(self):
            self.closed = True

    transport = Transport()
    client = TransportBackedMCPClient(
        MCPServerConfig(name="remote", transport="stdio", command="server"),
        transport,
    )

    with pytest.raises(MCPClientError) as exc_info:
        client.initialize()

    assert exc_info.value.error_code == "mcp_protocol_error"
    assert transport.closed is True
    assert transport.notifications == []


def test_registry_rejects_duplicate_tool_names_without_overwriting():
    logger = InMemoryLogger()
    registry = ToolRegistry(logger)

    def tool(output):
        return SimpleTool(
            name="same",
            description="same",
            arguments={},
            handler=lambda action, context: ToolResult(name=action.name, success=True, output=output),
        )

    assert registry.register(tool("first")) is True
    assert registry.register(tool("second")) is False
    assert registry.execute(ToolAction(name="same")).output == "first"
    assert any(event.name == "tool.registration_conflict" for event in logger.events)


def test_registry_refresh_replaces_and_removes_provider_owned_tools():
    logger = InMemoryLogger()
    registry = ToolRegistry(logger)

    class Provider:
        tools = []

        def get_tools(self):
            return list(self.tools)

    def tool(name, risk_level):
        return SimpleTool(
            name=name,
            description=name,
            arguments={},
            risk_level=risk_level,
            handler=lambda action, context: ToolResult(name=action.name, success=True, output=risk_level),
        )

    provider = Provider()
    provider.tools = [tool("remote__read", "read"), tool("remote__removed", "read")]
    registry.attach_provider(provider)

    assert {item.name for item in registry.definitions()} == {"remote__read", "remote__removed"}

    provider.tools = [tool("remote__read", "destructive")]
    definitions = registry.definitions()

    assert [(item.name, item.risk_level) for item in definitions] == [
        ("remote__read", "destructive")
    ]
    assert registry.definition_for("remote__removed") is None


def test_registry_provider_refresh_does_not_replace_builtin_tool():
    logger = InMemoryLogger()
    registry = ToolRegistry(logger)

    builtin = SimpleTool(
        name="same",
        description="builtin",
        arguments={},
        handler=lambda action, context: ToolResult(name=action.name, success=True, output="builtin"),
    )

    class Provider:
        def get_tools(self):
            return [
                SimpleTool(
                    name="same",
                    description="remote",
                    arguments={},
                    handler=lambda action, context: ToolResult(name=action.name, success=True, output="remote"),
                )
            ]

    registry.register(builtin)
    registry.attach_provider(Provider())

    registry.definitions()

    assert registry.execute(ToolAction(name="same")).output == "builtin"


def test_resource_provider_redacts_and_truncates_remote_content():
    config = MCPServerConfig(name="remote", transport="stdio", command="server")
    descriptor = MCPResourceDescriptor(
        server_name="remote", resource_id="resource://secret", name="secret"
    )

    class Discovery:
        def get_snapshot(self, _server_name):
            return MCPDiscoverySnapshot(
                server_name="remote",
                resources=(descriptor,),
                refreshed_at=time.time(),
            )

    class Client:
        def read_resource(self, _resource_id):
            return "token=abcdefghijklmnop " + ("x" * MAX_MCP_RESOURCE_CHARS)

    class Manager:
        def read_resource(self, _server_name, resource_id):
            return Client().read_resource(resource_id)

    provider = MCPResourceProvider((config,), Discovery(), Manager())
    stable_id = build_stable_resource_id("remote", "resource://secret")
    content = provider.read_resource(stable_id)

    assert "abcdefghijklmnop" not in content.text
    assert content.id == stable_id
    assert len(content.text) == MAX_MCP_RESOURCE_CHARS
    assert content.metadata["truncated"] is True


def test_resource_provider_namespaces_identical_resource_ids_by_server():
    configs = (
        MCPServerConfig(name="first", transport="stdio", command="server"),
        MCPServerConfig(name="second", transport="stdio", command="server"),
    )
    descriptors = {
        name: MCPResourceDescriptor(server_name=name, resource_id="file:///README.md", name="README")
        for name in ("first", "second")
    }

    class Discovery:
        def get_snapshot(self, server_name):
            return MCPDiscoverySnapshot(
                server_name=server_name,
                resources=(descriptors[server_name],),
                refreshed_at=time.time(),
            )

    class Client:
        def __init__(self, server_name):
            self.server_name = server_name

        def read_resource(self, resource_id):
            assert resource_id == "file:///README.md"
            return f"content from {self.server_name}"

    class Manager:
        def read_resource(self, server_name, resource_id):
            return Client(server_name).read_resource(resource_id)

    provider = MCPResourceProvider(configs, Discovery(), Manager())
    resources = provider.list_resources()

    assert len({resource.id for resource in resources}) == 2
    assert provider.read_resource(resources[0].id).text == "content from first"
    assert provider.read_resource(resources[1].id).text == "content from second"


def test_mcp_url_key_query_is_redacted_without_removing_other_parameters():
    redacted = redact_text("https://example.test/mcp?key=topsecret&mode=read")

    assert "topsecret" not in redacted
    assert "key=[REDACTED]" in redacted
    assert "mode=read" in redacted


def test_non_text_mcp_content_is_not_silently_dropped():
    content = _flatten_content_blocks(
        [
            {"type": "image", "mimeType": "image/png", "data": "YWJj"},
            {"type": "resource", "resource": {"uri": "file:///note.txt", "text": "embedded note"}},
            {"type": "custom"},
        ]
    )

    assert "[image content: image/png, base64 chars=4]" in content
    assert "embedded note" in content
    assert "[unsupported MCP content type: custom]" in content
