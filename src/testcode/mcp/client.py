from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import MCPServerConfig
from .transport import MCPHTTPError, MCPTransport, MCPTransportError, UnsupportedMCPTransport
from .types import (
    MAX_MCP_DISCOVERY_PAGES,
    MAX_MCP_RESOURCES_PER_SERVER,
    MAX_MCP_TOOLS_PER_SERVER,
    MCPResourceDescriptor,
    MCPToolCallResult,
    MCPToolDescriptor,
)

DEFAULT_PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOL_VERSIONS = {DEFAULT_PROTOCOL_VERSION}
CLIENT_INFO = {"name": "testcode", "version": "0.1"}


class MCPClient(Protocol):
    config: MCPServerConfig

    def initialize(self) -> None:
        ...

    def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        ...

    def call_tool(self, tool_name: str, arguments: dict) -> MCPToolCallResult:
        ...

    def list_resources(self) -> tuple[MCPResourceDescriptor, ...]:
        ...

    def read_resource(self, resource_id: str) -> str:
        ...

    def close(self) -> None:
        ...


class MCPClientError(RuntimeError):
    def __init__(self, message: str, *, error_code: str, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.metadata = metadata or {}


@dataclass(slots=True)
class TransportBackedMCPClient:
    config: MCPServerConfig
    transport: MCPTransport
    initialized: bool = False
    server_capabilities: dict[str, Any] = field(default_factory=dict)
    server_info: dict[str, Any] = field(default_factory=dict)
    protocol_version: str = DEFAULT_PROTOCOL_VERSION

    def initialize(self) -> None:
        if self.initialized:
            return

        self.transport.connect()
        response = self.transport.request(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": dict(CLIENT_INFO),
            },
        )
        result = _extract_result(response)
        self.server_capabilities = _object_value(result, "capabilities")
        self.server_info = _object_value(result, "serverInfo")
        server_protocol = result.get("protocolVersion")
        if not isinstance(server_protocol, str) or server_protocol.strip() not in SUPPORTED_PROTOCOL_VERSIONS:
            self.transport.close()
            reported_version = server_protocol.strip() if isinstance(server_protocol, str) else ""
            raise MCPClientError(
                f"MCP server returned unsupported protocol version '{reported_version or 'missing'}'",
                error_code="mcp_protocol_error",
                metadata={"protocol_version": reported_version or None},
            )
        self.protocol_version = server_protocol.strip()
        self.transport.notify("notifications/initialized", {})
        self.initialized = True

    def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        self.initialize()
        if "tools" not in self.server_capabilities:
            return ()
        raw_tools = self._list_all(
            "tools/list",
            "tools",
            max_items=MAX_MCP_TOOLS_PER_SERVER,
        )

        tools: list[MCPToolDescriptor] = []
        for raw_tool in raw_tools:
            if not isinstance(raw_tool, dict):
                continue
            tool_name = str(raw_tool.get("name", "")).strip()
            if not tool_name:
                continue
            input_schema = raw_tool.get("inputSchema")
            if not isinstance(input_schema, dict):
                input_schema = raw_tool.get("input_schema")
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}}
            annotations = raw_tool.get("annotations", {})
            if not isinstance(annotations, dict):
                annotations = {}
            tools.append(
                MCPToolDescriptor(
                    server_name=self.config.name,
                    tool_name=tool_name,
                    title=str(raw_tool.get("title", "") or ""),
                    description=str(raw_tool.get("description", "") or ""),
                    input_schema=input_schema,
                    annotations=annotations,
                )
            )
        return tuple(tools)

    def call_tool(self, tool_name: str, arguments: dict) -> MCPToolCallResult:
        self.initialize()
        try:
            response = self._request(
                "tools/call",
                {
                    "name": tool_name,
                    "arguments": dict(arguments),
                },
                retry_on_session_expiry=False,
            )
            result = _extract_result(response)
        except MCPTransportError as exc:
            return MCPToolCallResult(
                content=str(exc),
                metadata=dict(exc.metadata),
                is_error=True,
                error_code=exc.error_code,
            )
        except MCPClientError as exc:
            return MCPToolCallResult(
                content=str(exc),
                metadata=dict(exc.metadata),
                is_error=True,
                error_code=exc.error_code,
            )

        raw_content = result.get("content", [])
        if not isinstance(raw_content, list):
            raw_content = []
        text_content = _flatten_content_blocks(raw_content)
        structured_content = _object_value(result, "structuredContent")
        metadata = _object_value(result, "_meta")
        return MCPToolCallResult(
            content=text_content,
            structured_content=structured_content,
            metadata=metadata,
            is_error=bool(result.get("isError", False)),
            error_code="mcp_tool_call_failed" if bool(result.get("isError", False)) else None,
        )

    def list_resources(self) -> tuple[MCPResourceDescriptor, ...]:
        self.initialize()
        if "resources" not in self.server_capabilities:
            return ()
        raw_resources = self._list_all(
            "resources/list",
            "resources",
            max_items=MAX_MCP_RESOURCES_PER_SERVER,
        )

        resources: list[MCPResourceDescriptor] = []
        for raw_resource in raw_resources:
            if not isinstance(raw_resource, dict):
                continue
            resource_id = str(raw_resource.get("uri", "") or raw_resource.get("id", "")).strip()
            if not resource_id:
                continue
            resources.append(
                MCPResourceDescriptor(
                    server_name=self.config.name,
                    resource_id=resource_id,
                    name=str(raw_resource.get("name", "") or resource_id),
                    uri=str(raw_resource.get("uri", "") or resource_id),
                    description=str(raw_resource.get("description", "") or ""),
                    mime_type=str(raw_resource.get("mimeType", "") or raw_resource.get("mime_type", "") or ""),
                    metadata=_object_value(raw_resource, "_meta"),
                )
            )
        return tuple(resources)

    def read_resource(self, resource_id: str) -> str:
        self.initialize()
        if "resources" not in self.server_capabilities:
            raise MCPClientError("MCP server does not advertise resources", error_code="mcp_resource_unavailable")
        response = self._request("resources/read", {"uri": resource_id})
        result = _extract_result(response)
        raw_contents = result.get("contents", [])
        if not isinstance(raw_contents, list):
            raise MCPClientError(
                "MCP resources/read result did not contain a contents array",
                error_code="mcp_protocol_error",
            )
        return _flatten_content_blocks(raw_contents)

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        retry_on_session_expiry: bool = True,
    ) -> dict[str, Any]:
        try:
            return self.transport.request(method, params)
        except MCPHTTPError as exc:
            if exc.metadata.get("http_status") != 404:
                raise
            self.transport.close()
            self.initialized = False
            if not retry_on_session_expiry:
                raise
            self.initialize()
            return self.transport.request(method, params)

    def _list_all(
        self,
        method: str,
        result_key: str,
        *,
        max_items: int,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page_number in range(MAX_MCP_DISCOVERY_PAGES):
            params = {"cursor": cursor} if cursor is not None else {}
            result = _extract_result(self._request(method, params))
            page = result.get(result_key, [])
            if not isinstance(page, list):
                raise MCPClientError(
                    f"MCP {method} result did not contain a {result_key} array",
                    error_code="mcp_protocol_error",
                )
            # Keep one overflow item so discovery can emit its existing
            # truncation diagnostic, but never accumulate an unbounded result.
            for item in page:
                if isinstance(item, dict):
                    items.append(item)
                    if len(items) > max_items:
                        return items
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return items
            if next_cursor in seen_cursors:
                raise MCPClientError(
                    f"MCP {method} returned a repeated pagination cursor",
                    error_code="mcp_protocol_error",
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise MCPClientError(
            f"MCP {method} exceeded the pagination limit of {MAX_MCP_DISCOVERY_PAGES} pages",
            error_code="mcp_protocol_error",
        )

    def close(self) -> None:
        self.transport.close()
        self.initialized = False


@dataclass(slots=True)
class UnsupportedMCPClient:
    config: MCPServerConfig

    def initialize(self) -> None:
        raise UnsupportedMCPTransport(
            f"MCP transport '{self.config.transport}' is not implemented for server '{self.config.name}'"
        )

    def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        return ()

    def call_tool(self, tool_name: str, arguments: dict) -> MCPToolCallResult:
        raise UnsupportedMCPTransport(
            f"MCP tool call is unavailable for server '{self.config.name}' and tool '{tool_name}'"
        )

    def list_resources(self) -> tuple[MCPResourceDescriptor, ...]:
        return ()

    def read_resource(self, resource_id: str) -> str:
        raise UnsupportedMCPTransport(
            f"MCP resource read is unavailable for server '{self.config.name}' and resource '{resource_id}'"
        )

    def close(self) -> None:
        return None


def _extract_result(response: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise MCPClientError("MCP response was not a JSON object", error_code="mcp_protocol_error")
    if response.get("jsonrpc") != "2.0":
        raise MCPClientError("MCP response did not declare JSON-RPC 2.0", error_code="mcp_protocol_error")
    has_result = "result" in response and response["result"] is not None
    has_error = "error" in response and response["error"] is not None
    if has_result == has_error:
        raise MCPClientError(
            "MCP response must contain exactly one of result or error",
            error_code="mcp_protocol_error",
        )
    if has_error:
        error = response["error"]
        if not isinstance(error, dict):
            raise MCPClientError("MCP error response was malformed", error_code="mcp_protocol_error")
        raise MCPClientError(
            str(error.get("message", "MCP request failed")),
            error_code=_map_jsonrpc_error_code(error.get("code")),
            metadata={"error": error},
        )
    result = response["result"]
    if not isinstance(result, dict):
        raise MCPClientError("MCP result was not an object", error_code="mcp_protocol_error")
    return result


def _object_value(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    return value if isinstance(value, dict) else {}


def _flatten_content_blocks(blocks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            lines.append(text)
            continue
        uri = block.get("uri")
        if isinstance(uri, str) and uri:
            lines.append(uri)
            continue
        blob = block.get("blob")
        if isinstance(blob, str):
            lines.append(blob)
            continue
        block_type = block.get("type")
        if block_type in {"image", "audio"}:
            mime_type = str(block.get("mimeType", "application/octet-stream"))
            data = block.get("data")
            size = len(data) if isinstance(data, str) else 0
            lines.append(f"[{block_type} content: {mime_type}, base64 chars={size}]")
            continue
        embedded = block.get("resource")
        if isinstance(embedded, dict):
            embedded_text = embedded.get("text")
            if isinstance(embedded_text, str):
                lines.append(embedded_text)
                continue
            embedded_uri = embedded.get("uri")
            if isinstance(embedded_uri, str) and embedded_uri:
                lines.append(f"[embedded resource: {embedded_uri}]")
                continue
        if isinstance(block_type, str) and block_type:
            lines.append(f"[unsupported MCP content type: {block_type}]")
    return "\n".join(line for line in lines if line)


def _map_jsonrpc_error_code(code: Any) -> str:
    if isinstance(code, int) and code == -32602:
        return "mcp_invalid_schema"
    return "mcp_tool_call_failed"
