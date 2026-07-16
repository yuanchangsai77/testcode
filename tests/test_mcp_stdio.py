import sys
import time
from pathlib import Path

import pytest

import testcode.mcp.transport as transport_module
from testcode.app import create_app, create_mcp_client
from testcode.mcp.client import TransportBackedMCPClient
from testcode.mcp.config import MCPServerConfig
from testcode.mcp.transport import MCPTransportClosed, StdioTransport


SERVER_SCRIPT = r"""
import json
import sys


def read_message():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


def send_message(payload):
    sys.stdout.buffer.write(json.dumps(payload).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    if method == "initialize":
        send_message(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": "stdio-mcp", "version": "1.0"},
                },
            }
        )
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send_message(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "tools": [
                        {
                            "name": "ping",
                            "description": "Ping the local process",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"value": {"type": "string"}},
                                "required": ["value"],
                                "additionalProperties": False,
                            },
                        }
                    ]
                },
            }
        )
    elif method == "tools/call":
        value = message.get("params", {}).get("arguments", {}).get("value", "")
        send_message(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "content": [{"type": "text", "text": f"pong:{value}"}],
                    "structuredContent": {"echo": value},
                },
            }
        )
    elif method == "resources/list":
        send_message(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "resources": [
                        {
                            "uri": "resource://local/ping",
                            "name": "Ping Resource",
                            "mimeType": "text/plain",
                        }
                    ]
                },
            }
        )
    elif method == "resources/read":
        send_message(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "contents": [{"uri": "resource://local/ping", "text": "local resource body"}]
                },
            }
        )
    else:
        send_message(
            {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32601, "message": f"unknown method: {method}"},
            }
        )
"""


def write_stdio_server(tmp_path: Path) -> Path:
    script_path = tmp_path / "fake_stdio_mcp.py"
    script_path.write_text(SERVER_SCRIPT, encoding="utf-8")
    return script_path


def test_stdio_client_supports_initialize_tools_and_resources(tmp_path):
    script_path = write_stdio_server(tmp_path)
    config = MCPServerConfig(
        name="local",
        transport="stdio",
        command=sys.executable,
        args=("-u", str(script_path)),
    )

    client = create_mcp_client(config)
    assert isinstance(client, TransportBackedMCPClient)

    client.initialize()
    tools = client.list_tools()
    result = client.call_tool("ping", {"value": "alpha"})
    resources = client.list_resources()
    resource_text = client.read_resource("resource://local/ping")
    client.close()

    assert client.server_info["name"] == "stdio-mcp"
    assert len(tools) == 1
    assert tools[0].tool_name == "ping"
    assert result.is_error is False
    assert result.content == "pong:alpha"
    assert result.structured_content == {"echo": "alpha"}
    assert len(resources) == 1
    assert resources[0].resource_id == "resource://local/ping"
    assert resource_text == "local resource body"


def test_stdio_eof_unblocks_pending_request(tmp_path):
    script_path = tmp_path / "exit_without_response.py"
    script_path.write_text(
        "import sys\nsys.stdin.buffer.readline()\n",
        encoding="utf-8",
    )
    transport = StdioTransport(
        MCPServerConfig(
            name="local",
            transport="stdio",
            command=sys.executable,
            args=("-u", str(script_path)),
            read_timeout=2,
        )
    )

    started = time.monotonic()
    with pytest.raises(MCPTransportClosed):
        transport.request("ping", {})

    assert time.monotonic() - started < 1
    transport.close()


def test_stdio_rejects_oversized_message(monkeypatch):
    monkeypatch.setattr(transport_module, "MAX_MCP_MESSAGE_BYTES", 64)
    script = (
        "import json,sys; "
        "sys.stdin.buffer.readline(); "
        "sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':1,'result':{'text':'x'*128}})+'\\n'); "
        "sys.stdout.flush()"
    )
    transport = StdioTransport(
        MCPServerConfig(
            name="oversized",
            transport="stdio",
            command=sys.executable,
            args=("-c", script),
            read_timeout=1,
        )
    )

    with pytest.raises(MCPTransportClosed, match="exceeded 64 bytes"):
        transport.request("ping", {})

    transport.close()


def test_stdio_accepts_jsonrpc_batch_response(tmp_path):
    script_path = tmp_path / "batch_response.py"
    script_path.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.buffer.readline())\n"
        "response = ["
        "{'jsonrpc': '2.0', 'method': 'notifications/progress'}, "
        "{'jsonrpc': '2.0', 'id': request['id'], 'result': {'ok': True}}"
        "]\n"
        "sys.stdout.write(json.dumps(response) + '\\n')\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    transport = StdioTransport(
        MCPServerConfig(
            name="batch",
            transport="stdio",
            command=sys.executable,
            args=("-u", str(script_path)),
            read_timeout=2,
        )
    )

    response = transport.request("ping")
    transport.close()

    assert response["result"] == {"ok": True}


def test_stdio_drops_idle_notifications_and_bounds_stderr(tmp_path):
    script_path = tmp_path / "noisy_server.py"
    script_path.write_text(
        "import json, sys, time\n"
        "for index in range(200):\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/progress', 'params': {'index': index}}), flush=True)\n"
        f"sys.stderr.write('x' * {transport_module.MAX_MCP_STDERR_LINE_BYTES * 2})\n"
        "sys.stderr.flush()\n"
        "time.sleep(2)\n",
        encoding="utf-8",
    )
    transport = StdioTransport(
        MCPServerConfig(
            name="noisy",
            transport="stdio",
            command=sys.executable,
            args=("-u", str(script_path)),
        )
    )

    transport.connect()
    deadline = time.monotonic() + 1
    while not transport._stderr_tail and time.monotonic() < deadline:
        time.sleep(0.01)

    assert transport._responses.qsize() == 0
    assert transport._stderr_tail
    assert all(
        len(line) <= transport_module.MAX_MCP_STDERR_LINE_BYTES + len(" [truncated]")
        for line in transport._stderr_tail
    )
    transport.close()


def test_stdio_bounds_unsolicited_response_queue(tmp_path):
    script_path = tmp_path / "response_flood.py"
    script_path.write_text(
        "import json, sys, time\n"
        "for index in range(100):\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': index, 'result': {'index': index}}), flush=True)\n"
        "print('done', file=sys.stderr, flush=True)\n"
        "time.sleep(2)\n",
        encoding="utf-8",
    )
    transport = StdioTransport(
        MCPServerConfig(
            name="flooding",
            transport="stdio",
            command=sys.executable,
            args=("-u", str(script_path)),
        )
    )

    transport.connect()
    deadline = time.monotonic() + 1
    queued = []
    while time.monotonic() < deadline:
        with transport._responses.mutex:
            queued = list(transport._responses.queue)
        overflowed = any(
            item.get("error", {}).get("message") == "MCP response queue exceeded its limit"
            for item in queued
        )
        if "done" in transport._stderr_tail and overflowed:
            break
        time.sleep(0.01)

    assert transport._responses.qsize() == transport_module.MAX_PENDING_MCP_RESPONSES
    assert any(item.get("error", {}).get("message") == "MCP response queue exceeded its limit" for item in queued)
    transport.close()


def test_create_app_activates_stdio_mcp_tool_on_demand(tmp_path, monkeypatch):
    script_path = write_stdio_server(tmp_path)
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".testcode"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        f"""
[[mcp.servers]]
name = "local"
transport = "stdio"
command = "{sys.executable}"
args = ["-u", "{script_path}"]
        """.strip(),
        encoding="utf-8",
    )

    app = create_app()
    warehouse = app.engine.capability_warehouse
    assert app.engine.tools.definition_for("local__ping") is None
    manifest = warehouse.open_toolbox("mcp:local")
    assert [item.name for item in manifest.items] == ["ping"]
    warehouse.activate(["mcp:local:ping"])
    definition = app.engine.tools.definition_for("local__ping")

    assert definition is not None
    assert definition.risk_level == "confirm"
    app.engine.tools.reset_state()
