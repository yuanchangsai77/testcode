import json

import pytest

import testcode.app as app_module
import testcode.mcp.transport as transport_module
from testcode.app import create_app, create_mcp_client
from testcode.mcp.client import TransportBackedMCPClient
from testcode.mcp.config import MCPServerConfig
from testcode.mcp.manager import MCPManager
from testcode.mcp.transport import MCPProtocolError, MCPTransportClosed, SSETransport


class FakeSSEBody:
    def __init__(self, text: str):
        self._lines = iter(text.splitlines(keepends=True))

    def readline(self, _size=-1):
        return next(self._lines, b"")


class FakeSSEStreamResponse:
    def __init__(self, body: str):
        self.status = 200
        self.fp = FakeSSEBody(body)

    def getheader(self, name, default=None):
        if name.lower() == "content-type":
            return "text/event-stream"
        return default

    def read(self, _size=-1):
        return b""

    def readline(self, size=-1):
        return self.fp.readline(size)


class DecodingSSEStreamResponse(FakeSSEStreamResponse):
    """Models HTTPResponse decoding wire framing before exposing SSE lines."""

    def __init__(self, body: str):
        super().__init__(body)
        self.fp = None
        self._decoded_body = FakeSSEBody(body)

    def readline(self, size=-1):
        return self._decoded_body.readline(size)


class FakePostResponse:
    def __init__(self, status=202, body="", headers=None):
        self.status = status
        self._body = body.encode("utf-8")
        self._headers = headers or {"Content-Type": "application/json"}

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._body)
        chunk = self._body[:size]
        self._body = self._body[size:]
        return chunk


class FakeSSEConnection:
    stream_response = None
    post_responses = []
    calls = []
    instances = []

    def __init__(self, host, port=None, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.mode = None
        self.sock = FakeSocket(timeout)
        self.instances.append(self)

    def connect(self):
        return None

    def request(self, method, path, body=None, headers=None):
        self.mode = method
        payload = None
        if body:
            payload = json.loads(body.decode("utf-8"))
        self.calls.append({"method": method, "path": path, "headers": dict(headers or {}), "payload": payload})

    def getresponse(self):
        if self.mode == "GET":
            return self.stream_response
        return self.post_responses.pop(0)

    def close(self):
        return None


class FakeSocket:
    def __init__(self, timeout):
        self.timeout = timeout
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeout = timeout
        self.timeouts.append(timeout)


def queue_sse_fixture():
    FakeSSEConnection.calls = []
    FakeSSEConnection.instances = []
    FakeSSEConnection.stream_response = FakeSSEStreamResponse(
        "event: endpoint\n"
        "data: /messages\n\n"
        f"data: {json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}, 'resources': {}}, 'serverInfo': {'name': 'sse-mcp', 'version': '1.0'}}})}\n\n"
        f"data: {json.dumps({'jsonrpc': '2.0', 'id': 2, 'result': {'tools': [{'name': 'search', 'description': 'Search over SSE', 'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query'], 'additionalProperties': False}}]}})}\n\n"
        f"data: {json.dumps({'jsonrpc': '2.0', 'id': 3, 'result': {'content': [{'type': 'text', 'text': 'sse match'}], 'structuredContent': {'count': 1}}})}\n\n"
        f"data: {json.dumps({'jsonrpc': '2.0', 'id': 4, 'result': {'resources': [{'uri': 'resource://sse/intro', 'name': 'SSE Intro', 'mimeType': 'text/plain'}]}})}\n\n"
        f"data: {json.dumps({'jsonrpc': '2.0', 'id': 5, 'result': {'contents': [{'uri': 'resource://sse/intro', 'text': 'hello from sse'}]}})}\n\n"
    )
    FakeSSEConnection.post_responses = [
        FakePostResponse(),
        FakePostResponse(),
        FakePostResponse(),
        FakePostResponse(),
        FakePostResponse(),
        FakePostResponse(),
    ]


def test_sse_client_supports_initialize_tools_and_resources(monkeypatch):
    queue_sse_fixture()
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", FakeSSEConnection)
    config = MCPServerConfig(name="remote", transport="sse", url="http://example.test/sse")

    client = create_mcp_client(config)
    assert isinstance(client, TransportBackedMCPClient)

    client.initialize()
    tools = client.list_tools()
    result = client.call_tool("search", {"query": "alpha"})
    resources = client.list_resources()
    resource_text = client.read_resource("resource://sse/intro")
    client.close()

    assert client.server_info["name"] == "sse-mcp"
    assert len(tools) == 1
    assert tools[0].tool_name == "search"
    assert result.content == "sse match"
    assert result.structured_content == {"count": 1}
    assert resources[0].resource_id == "resource://sse/intro"
    assert resource_text == "hello from sse"

    methods = [call["method"] for call in FakeSSEConnection.calls]
    assert methods == ["GET", "POST", "POST", "POST", "POST", "POST", "POST"]
    assert FakeSSEConnection.calls[1]["path"] == "/messages"


def test_sse_reader_uses_http_response_decoding_layer(monkeypatch):
    FakeSSEConnection.calls = []
    FakeSSEConnection.instances = []
    FakeSSEConnection.stream_response = DecodingSSEStreamResponse(
        "event: endpoint\n"
        "data: /messages\n\n"
        f"data: {json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {}}})}\n\n"
    )
    FakeSSEConnection.post_responses = [FakePostResponse(), FakePostResponse()]
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", FakeSSEConnection)
    config = MCPServerConfig(name="remote", transport="sse", url="http://example.test/sse")

    client = create_mcp_client(config)
    client.initialize()
    client.close()

    assert client.initialized is False
    assert FakeSSEConnection.calls[1]["path"] == "/messages"


def test_sse_malformed_post_response_invalidates_cached_client(monkeypatch):
    FakeSSEConnection.calls = []
    FakeSSEConnection.instances = []
    FakeSSEConnection.stream_response = FakeSSEStreamResponse(
        "event: endpoint\n"
        "data: /messages\n\n"
        f"data: {json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}, 'serverInfo': {}}})}\n\n"
    )
    FakeSSEConnection.post_responses = [
        FakePostResponse(),
        FakePostResponse(),
        FakePostResponse(status=200, body="not-json"),
    ]
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", FakeSSEConnection)
    config = MCPServerConfig(name="remote", transport="sse", url="http://example.test/sse")
    manager = MCPManager(
        configs={config.name: config},
        client_factory=lambda server: TransportBackedMCPClient(
            config=server,
            transport=SSETransport(config=server),
        ),
    )

    manager.get_client("remote")
    result = manager.call_tool("remote", "search", {"query": "alpha"})

    assert result.is_error is True
    assert result.error_code == "mcp_protocol_error"
    assert "remote" not in manager._clients


def test_sse_stream_switches_from_connect_timeout_to_read_timeout(monkeypatch):
    queue_sse_fixture()
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", FakeSSEConnection)
    transport = SSETransport(
        MCPServerConfig(
            name="remote",
            transport="sse",
            url="http://example.test/sse",
            timeout=2,
            read_timeout=45,
        )
    )

    transport.connect()

    stream_connection = FakeSSEConnection.instances[0]
    assert stream_connection.timeout == 2
    assert stream_connection.sock.timeouts == [45]
    transport.close()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:8080/private",
        "http://example.test:8080/private",
        "https://example.test/private",
    ],
)
def test_sse_rejects_cross_origin_message_endpoint(monkeypatch, endpoint):
    FakeSSEConnection.calls = []
    FakeSSEConnection.stream_response = FakeSSEStreamResponse(
        "event: endpoint\n"
        f"data: {endpoint}\n\n"
    )
    FakeSSEConnection.post_responses = []
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", FakeSSEConnection)
    transport = SSETransport(
        MCPServerConfig(name="remote", transport="sse", url="http://example.test/sse", timeout=0.2)
    )

    with pytest.raises(MCPProtocolError):
        transport.connect()

    assert [call["method"] for call in FakeSSEConnection.calls] == ["GET"]


def test_sse_rejects_oversized_event(monkeypatch):
    monkeypatch.setattr(transport_module, "MAX_MCP_MESSAGE_BYTES", 32)
    FakeSSEConnection.calls = []
    FakeSSEConnection.instances = []
    FakeSSEConnection.stream_response = FakeSSEStreamResponse(
        "event: endpoint\n"
        f"data: /{'x' * 64}\n\n"
    )
    FakeSSEConnection.post_responses = []
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", FakeSSEConnection)
    transport = SSETransport(
        MCPServerConfig(name="remote", transport="sse", url="http://example.test/sse", timeout=0.2)
    )

    with pytest.raises(MCPProtocolError, match="exceeded 32 bytes"):
        transport.connect()


def test_sse_accepts_relative_and_same_origin_absolute_message_endpoints():
    transport = SSETransport(
        MCPServerConfig(name="remote", transport="sse", url="http://example.test:80/sse")
    )

    assert transport._resolve_endpoint("/messages") == "http://example.test:80/messages"
    assert transport._resolve_endpoint("http://example.test/messages") == "http://example.test/messages"


def test_sse_eof_unblocks_pending_request(monkeypatch):
    FakeSSEConnection.calls = []
    FakeSSEConnection.stream_response = FakeSSEStreamResponse(
        "event: endpoint\n"
        "data: /messages\n\n"
    )
    FakeSSEConnection.post_responses = [FakePostResponse()]
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", FakeSSEConnection)
    transport = SSETransport(
        MCPServerConfig(
            name="remote",
            transport="sse",
            url="http://example.test/sse",
            read_timeout=2,
        )
    )

    with pytest.raises(MCPTransportClosed):
        transport.request("ping", {})

    transport.close()


def test_create_app_registers_sse_mcp_tools(tmp_path, monkeypatch):
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
                    description="Search over SSE",
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
transport = "sse"
url = "http://example.test/sse"
        """.strip(),
        encoding="utf-8",
    )

    app = create_app()
    app.engine.tools.definitions()
    definition = app.engine.tools.definition_for("remote__search")

    assert definition is not None
    assert definition.risk_level == "network"
