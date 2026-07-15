from __future__ import annotations

from dataclasses import dataclass

from ..orchestration.ext import ResourceProvider
from ..tools.base import Tool
from ..types import ResourceContent, ResourceDescriptor
from .adapter import (
    MCPToolAdapter,
    adapt_resource_content,
    adapt_resource_descriptor,
    build_stable_resource_id,
)
from .config import MCPServerConfig
from .discovery import MCPDiscoveryService
from .manager import MCPManager
from ..safety.redaction import redact_text


MAX_MCP_RESOURCE_CHARS = 100_000


@dataclass(slots=True)
class MCPToolProvider:
    configs: tuple[MCPServerConfig, ...]
    discovery: MCPDiscoveryService
    manager: MCPManager
    logger: object | None = None

    def get_tools(self) -> list[Tool]:
        tools: list[Tool] = []
        registered_names: set[str] = set()
        for config in self.configs:
            if not config.enabled:
                continue
            adapter = MCPToolAdapter(server=config, manager=self.manager, logger=self.logger)
            snapshot = self.discovery.get_snapshot(config.name)
            for descriptor in snapshot.tools:
                schema = descriptor.input_schema
                if schema.get("type", "object") != "object" or not isinstance(schema.get("properties", {}), dict):
                    if self.logger is not None:
                        self.logger.record("mcp.tool.invalid_schema", {
                            "server_name": config.name,
                            "tool_name": descriptor.tool_name,
                            "error_code": "mcp_invalid_schema",
                        })
                    continue
                tool = adapter.adapt(descriptor)
                if tool.name in registered_names:
                    if self.logger is not None:
                        self.logger.record("mcp.tool.conflict", {
                            "server_name": config.name,
                            "stable_id": tool.name,
                            "error_code": "duplicate_tool_name",
                        })
                    continue
                registered_names.add(tool.name)
                tools.append(tool)
        return tools


@dataclass(slots=True)
class MCPResourceProvider(ResourceProvider):
    configs: tuple[MCPServerConfig, ...]
    discovery: MCPDiscoveryService
    manager: MCPManager
    logger: object | None = None

    def list_resources(self) -> list[ResourceDescriptor]:
        resources: list[ResourceDescriptor] = []
        for config in self.configs:
            if not config.enabled:
                continue
            snapshot = self.discovery.get_snapshot(config.name)
            resources.extend(adapt_resource_descriptor(resource) for resource in snapshot.resources)
        return resources

    def read_resource(self, resource_id: str) -> ResourceContent:
        for config in self.configs:
            if not config.enabled:
                continue
            snapshot = self.discovery.get_snapshot(config.name)
            descriptor = next(
                (
                    resource
                    for resource in snapshot.resources
                    if build_stable_resource_id(resource.server_name, resource.resource_id) == resource_id
                ),
                None,
            )
            if descriptor is not None:
                text = self.manager.read_resource(config.name, descriptor.resource_id)
                redacted = redact_text(text)
                truncated = len(redacted) > MAX_MCP_RESOURCE_CHARS
                content = adapt_resource_content(
                    descriptor.resource_id,
                    config.name,
                    redacted[:MAX_MCP_RESOURCE_CHARS],
                )
                content.metadata.update({"truncated": truncated, "original_chars": len(redacted)})
                if self.logger is not None:
                    self.logger.record("mcp.resource.read", {
                        "server_name": config.name,
                        "resource_id": resource_id,
                        "chars": len(content.text),
                        "truncated": truncated,
                    })
                return content
        raise KeyError(f"Unknown MCP resource id: {resource_id}")
