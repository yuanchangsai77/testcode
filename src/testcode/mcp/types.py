from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


MAX_MCP_TOOLS_PER_SERVER = 256
MAX_MCP_RESOURCES_PER_SERVER = 1_000
MAX_MCP_DISCOVERY_PAGES = 1_024


@dataclass(frozen=True, slots=True)
class MCPToolDescriptor:
    server_name: str
    tool_name: str
    title: str = ""
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MCPResourceDescriptor:
    server_name: str
    resource_id: str
    name: str
    uri: str = ""
    description: str = ""
    mime_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MCPToolCallResult:
    content: str
    structured_content: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class MCPDiscoverySnapshot:
    server_name: str
    tools: tuple[MCPToolDescriptor, ...] = ()
    resources: tuple[MCPResourceDescriptor, ...] = ()
    error_code: str | None = None
    error_message: str = ""
    cause_error_code: str | None = None
    error_details: dict[str, Any] = field(default_factory=dict)
    resource_error_code: str | None = None
    resource_error_message: str = ""
    refreshed_at: float = 0.0
    source: str = "live"
