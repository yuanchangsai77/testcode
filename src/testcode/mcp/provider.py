from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit

from ..orchestration.ext import ResourceProvider
from ..tools.base import Tool
from ..types import ResourceContent, ResourceDescriptor
from .adapter import (
    MCPToolAdapter,
    adapt_resource_content,
    adapt_resource_descriptor,
    build_stable_resource_id,
    build_stable_tool_name,
)
from .config import MCPServerConfig
from .discovery import MCPDiscoveryService
from .manager import MCPManager
from ..safety.redaction import redact, redact_text


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

    def get_statuses(self) -> list[dict[str, object]]:
        """Expose bounded, secret-safe MCP availability diagnostics."""
        statuses: list[dict[str, object]] = []
        now = time.time()
        for config in self.configs:
            snapshot = self.discovery.peek_snapshot(config.name)
            if not config.enabled:
                state = "disabled"
            elif snapshot is None:
                state = "not_discovered"
            elif snapshot.error_code and not snapshot.tools:
                state = "unavailable"
            elif snapshot.error_code or snapshot.resource_error_code:
                state = "degraded"
            else:
                state = "ready"

            tool_names = (
                [build_stable_tool_name(config, item.tool_name) for item in snapshot.tools]
                if snapshot is not None
                else []
            )
            refreshed_at = snapshot.refreshed_at if snapshot is not None else 0.0
            status: dict[str, object] = {
                "provider": "mcp",
                "server_name": config.name,
                "tool_name_prefix": config.stable_prefix,
                "transport": config.transport,
                "target": self._safe_target(config),
                "configured": True,
                "enabled": config.enabled,
                "state": state,
                "source": snapshot.source if snapshot is not None else "none",
                "tool_count": len(snapshot.tools) if snapshot is not None else 0,
                "resource_count": len(snapshot.resources) if snapshot is not None else 0,
                "tool_names": tool_names,
                "refreshed_at": refreshed_at,
                "age_seconds": round(max(0.0, now - refreshed_at), 3) if refreshed_at else None,
                "error_code": (
                    snapshot.cause_error_code or snapshot.error_code
                    if snapshot is not None
                    else None
                ),
                "error_message": redact_text(snapshot.error_message) if snapshot is not None else "",
                "error_details": redact(dict(snapshot.error_details)) if snapshot is not None else {},
                "resource_error_code": snapshot.resource_error_code if snapshot is not None else None,
                "resource_error_message": (
                    redact_text(snapshot.resource_error_message) if snapshot is not None else ""
                ),
            }
            statuses.append(status)
        return statuses

    def _safe_target(self, config: MCPServerConfig) -> str:
        if config.url:
            parsed = urlsplit(config.url)
            safe_netloc = parsed.netloc.rsplit("@", 1)[-1]
            return urlunsplit((parsed.scheme, safe_netloc, parsed.path, "", ""))
        if config.command:
            return Path(config.command).name
        return ""


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
