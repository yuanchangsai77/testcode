import json

import pytest

import testcode.app as app_module
import testcode.mcp.transport as transport_module
from testcode.app import create_app, create_mcp_client
from testcode.mcp.client import TransportBackedMCPClient
from testcode.mcp.config import MCPServerConfig
from testcode.mcp.transport import MCPProtocolError, StreamableHttpTransport


class FakeSocket:
    def __init__(self, timeout):
        self.timeout = timeout

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value


class FakeHTTPResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = body.encode("utf-8")
        self._headers = headers or {}

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._body)
        chunk = self._body[:size]
        self._body = self._body[size:]
        return chunk

    def readline(self, size=-1):
        if not self._body:
            return b""
        line, separator, remainder = self._body.partition(b"\n")
        candidate = line + separator
        if size is not None and size >= 0 and len(candidate) > size:
            candidate = self._body[:size]
            self._body = self._body[size:]
            return candidate
        self._body = remainder
        return candidate


class FakeHTTPConnection:
    responses = []
    calls = []
    instances = []

    def __init__(self, host, port=None, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = FakeSocket(timeout)
        self.closed = False
        self.instances.append(self)

    def connect(self):
        return None

    def request(self, method, path, body=None, headers=None):
        payload = json.loads(body.decode("utf-8"))
        self.calls.append(
            {
                "method": method,
                "path": path,
                "headers": dict(headers or {}),
                "payload": payload,
            }
        )

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def queue_fake_responses():
    FakeHTTPConnection.calls = []
    FakeHTTPConnection.instances = []
    FakeHTTPConnection.responses = [
        FakeHTTPResponse(
            200,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}, "resources": {}},
                        "serverInfo": {"name": "fake-mcp", "version": "1.0"},
                    },
                }
            ),
            headers={"Content-Type": "application/json", "Mcp-Session-Id": "session-123"},
        ),
        FakeHTTPResponse(202, "", headers={"Content-Type": "application/json"}),
        FakeHTTPResponse(
            200,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "tools": [
                            {
                                "name": "search",
                                "description": "Search remote data",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"query": {"type": "string"}},
                                    "required": ["query"],
                                    "additionalProperties": False,
                                },
                            }
                        ]
                    },
                }
            ),
            headers={"Content-Type": "application/json"},
        ),
        FakeHTTPResponse(
            200,
            "event: message\n"
            f"data: {json.dumps({'jsonrpc': '2.0', 'id': 3, 'result': {'content': [{'type': 'text', 'text': 'match one'}], 'structuredContent': {'count': 1}}})}\n\n",
            headers={"Content-Type": "Text/Event-Stream; Charset=UTF-8"},
        ),
        FakeHTTPResponse(
            200,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "result": {
                        "resources": [
                            {
                                "uri": "resource://docs/intro",
                                "name": "Intro",
                                "description": "Intro doc",
                                "mimeType": "text/plain",
                            }
                        ]
                    },
                }
            ),
            headers={"Content-Type": "application/json"},
        ),
        FakeHTTPResponse(
            200,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "result": {
                        "contents": [
                            {"uri": "resource://docs/intro", "text": "hello from resource"}
                        ]
                    },
                }
            ),
            headers={"Content-Type": "application/json"},
        ),
    ]


def test_streamable_http_client_supports_initialize_tools_and_resources(monkeypatch):
    queue_fake_responses()
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", FakeHTTPConnection)
    config = MCPServerConfig(
        name="remote",
        transport="streamable_http",
        url="http://example.test/mcp",
    )

    client = create_mcp_client(config)
    assert isinstance(client, TransportBackedMCPClient)

    client.initialize()
    tools = client.list_tools()
    result = client.call_tool("search", {"query": "alpha"})
    resources = client.list_resources()
    resource_text = client.read_resource("resource://docs/intro")
    client.close()

    assert client.server_info["name"] == "fake-mcp"
    assert len(tools) == 1
    assert tools[0].tool_name == "search"
    assert result.is_error is False
    assert result.content == "match one"
    assert result.structured_content == {"count": 1}
    assert len(resources) == 1
    assert resources[0].resource_id == "resource://docs/intro"
    assert resource_text == "hello from resource"

    methods = [call["payload"]["method"] for call in FakeHTTPConnection.calls]
    assert methods == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
        "resources/list",
        "resources/read",
    ]
    assert FakeHTTPConnection.calls[0]["headers"].get("Mcp-Session-Id") is None
    assert all(call["headers"].get("Mcp-Session-Id") == "session-123" for call in FakeHTTPConnection.calls[1:])
    assert len(FakeHTTPConnection.instances) == 2
    assert FakeHTTPConnection.instances[0].closed is True


def test_streamable_http_returns_matching_sse_response_without_waiting_for_stream_eof(monkeypatch):
    class OpenSSEStream(FakeHTTPResponse):
        def read(self, _size=-1):
            raise AssertionError("SSE responses must be consumed incrementally")

        def readline(self, size=-1):
            if self._body:
                return super().readline(size)
            raise AssertionError("transport waited for EOF after receiving the matching response")

    FakeHTTPConnection.calls = []
    FakeHTTPConnection.instances = []
    FakeHTTPConnection.responses = [
        OpenSSEStream(
            200,
            "event: message\n"
            f"data: {json.dumps({'jsonrpc': '2.0', 'method': 'notifications/progress'})}\n\n"
            "event: message\n"
            f"data: {json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'ok': True}})}\n\n",
            headers={"Content-Type": "text/event-stream"},
        )
    ]
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", FakeHTTPConnection)
    config = MCPServerConfig(
        name="remote",
        transport="streamable_http",
        url="http://example.test/mcp",
    )

    transport = StreamableHttpTransport(config)
    response = transport.request("ping")
    transport.close()

    assert response["id"] == 1
    assert response["result"] == {"ok": True}


def test_streamable_http_rejects_oversized_response_before_json_parsing(monkeypatch):
    monkeypatch.setattr(transport_module, "MAX_MCP_MESSAGE_BYTES", 64)
    FakeHTTPConnection.calls = []
    FakeHTTPConnection.instances = []
    FakeHTTPConnection.responses = [
        FakeHTTPResponse(
            200,
            "x" * 65,
            headers={"Content-Type": "application/json"},
        )
    ]
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", FakeHTTPConnection)
    transport = StreamableHttpTransport(
        MCPServerConfig(name="remote", transport="streamable_http", url="http://example.test/mcp")
    )

    with pytest.raises(MCPProtocolError, match="exceeded 64 bytes"):
        transport.request("ping", {})

    assert FakeHTTPConnection.instances[0].closed is True


def test_streamable_http_rejects_oversized_sse_event(monkeypatch):
    monkeypatch.setattr(transport_module, "MAX_MCP_MESSAGE_BYTES", 64)
    FakeHTTPConnection.calls = []
    FakeHTTPConnection.instances = []
    FakeHTTPConnection.responses = [
        FakeHTTPResponse(
            200,
            f"data: {'x' * 128}\n\n",
            headers={"Content-Type": "text/event-stream"},
        )
    ]
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", FakeHTTPConnection)
    transport = StreamableHttpTransport(
        MCPServerConfig(name="remote", transport="streamable_http", url="http://example.test/mcp")
    )

    with pytest.raises(MCPProtocolError, match="exceeded 64 bytes"):
        transport.request("ping", {})

    assert FakeHTTPConnection.instances[0].closed is True


def test_streamable_http_selects_matching_response_from_json_and_sse_batches(monkeypatch):
    FakeHTTPConnection.calls = []
    FakeHTTPConnection.instances = []
    FakeHTTPConnection.responses = [
        FakeHTTPResponse(
            200,
            json.dumps(
                [
                    {"jsonrpc": "2.0", "method": "notifications/progress"},
                    {"jsonrpc": "2.0", "id": 1, "result": {"kind": "json"}},
                ]
            ),
            headers={"Content-Type": "application/json"},
        ),
        FakeHTTPResponse(
            200,
            "event: message\n"
            f"data: {json.dumps([{'jsonrpc': '2.0', 'method': 'notifications/progress'}, {'jsonrpc': '2.0', 'id': 2, 'result': {'kind': 'sse'}}])}\n\n",
            headers={"Content-Type": "text/event-stream"},
        ),
    ]
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", FakeHTTPConnection)
    transport = StreamableHttpTransport(
        MCPServerConfig(name="remote", transport="streamable_http", url="http://example.test/mcp")
    )

    json_response = transport.request("first")
    sse_response = transport.request("second")
    transport.close()

    assert json_response["result"] == {"kind": "json"}
    assert sse_response["result"] == {"kind": "sse"}


def test_streamable_http_drops_connection_after_malformed_sse_event(monkeypatch):
    FakeHTTPConnection.calls = []
    FakeHTTPConnection.instances = []
    FakeHTTPConnection.responses = [
        FakeHTTPResponse(
            200,
            "event: message\ndata: {not-json}\n\n",
            headers={"Content-Type": "text/event-stream"},
        )
    ]
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", FakeHTTPConnection)
    transport = StreamableHttpTransport(
        MCPServerConfig(name="remote", transport="streamable_http", url="http://example.test/mcp")
    )

    with pytest.raises(MCPProtocolError):
        transport.request("broken")

    assert FakeHTTPConnection.instances[0].closed is True
    assert transport._connection is None


def test_streamable_http_client_maps_jsonrpc_tool_errors(monkeypatch):
    FakeHTTPConnection.calls = []
    FakeHTTPConnection.responses = [
        FakeHTTPResponse(
            200,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {"name": "fake-mcp", "version": "1.0"},
                    },
                }
            ),
            headers={"Content-Type": "application/json", "Mcp-Session-Id": "session-123"},
        ),
        FakeHTTPResponse(202, "", headers={"Content-Type": "application/json"}),
        FakeHTTPResponse(
            200,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "error": {"code": -32602, "message": "invalid arguments"},
                }
            ),
            headers={"Content-Type": "application/json"},
        ),
    ]
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", FakeHTTPConnection)
    config = MCPServerConfig(
        name="remote",
        transport="streamable_http",
        url="http://example.test/mcp",
    )

    client = create_mcp_client(config)
    result = client.call_tool("broken", {"query": "alpha"})
    client.close()

    assert result.is_error is True
    assert result.error_code == "mcp_invalid_schema"
    assert "invalid arguments" in result.content


def test_create_app_activates_streamable_http_mcp_tools_on_demand(tmp_path, monkeypatch):
    class FakeDiscoveryClient:
        def __init__(self, config):
            self.config = config

        def initialize(self):
            return None

        def list_tools(self):
            from testcode.mcp.types import MCPToolDescriptor

            return (
                MCPToolDescriptor(
                    server_name=self.config.name,
                    tool_name="search",
                    description="Search remote data",
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                ),
            )

        def call_tool(self, tool_name, arguments):
            raise AssertionError("not used in this test")

        def list_resources(self):
            return ()

        def read_resource(self, resource_id):
            raise AssertionError("not used in this test")

        def close(self):
            return None

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app_module, "create_mcp_client", lambda server: FakeDiscoveryClient(server))
    config_dir = tmp_path / ".testcode"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[[mcp.servers]]
name = "remote"
transport = "streamable_http"
url = "http://example.test/mcp"
        """.strip(),
        encoding="utf-8",
    )

    app = create_app()
    warehouse = app.engine.capability_warehouse
    assert app.engine.tools.definition_for("remote__search") is None
    warehouse.open_toolbox("mcp:remote")
    warehouse.activate(["mcp:remote:search"])
    definition = app.engine.tools.definition_for("remote__search")

    assert definition is not None
    assert definition.risk_level == "network"
