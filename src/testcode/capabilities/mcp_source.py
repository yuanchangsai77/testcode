from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ..mcp.adapter import MCPToolAdapter, infer_mcp_tool_traits, map_mcp_tool_risk
from ..mcp.config import MCPServerConfig
from ..mcp.discovery import MCPDiscoveryService
from ..mcp.manager import MCPManager
from ..mcp.types import MCPToolDescriptor
from .model import ActivatedCapability, CapabilityEntry, CapabilityManifest, ManifestItem


@dataclass(slots=True)
class MCPToolboxSource:
    configs: tuple[MCPServerConfig, ...]
    discovery: MCPDiscoveryService
    manager: MCPManager
    logger: object | None = None
    source_name: str = "mcp"
    _descriptors: dict[str, MCPToolDescriptor] = field(default_factory=dict)

    def catalog_entries(self) -> list[CapabilityEntry]:
        entries: list[CapabilityEntry] = []
        for config in self.configs:
            declared_capabilities = ", ".join(config.capabilities)
            description = config.description or (
                f"Provides {declared_capabilities}."
                if declared_capabilities
                else f"MCP toolbox '{config.name}'; its purpose was not declared in configuration."
            )
            tags = tuple(dict.fromkeys(("mcp", config.name, *config.capabilities)))
            entries.append(
                CapabilityEntry(
                    id=f"mcp:{config.name}",
                    name=config.name,
                    kind="toolbox",
                    source="mcp",
                    description=description,
                    tags=tags,
                    configured=True,
                    enabled=config.enabled,
                    metadata={
                        "transport": config.transport,
                        "target": self._safe_target(config),
                    },
                )
            )
        return entries

    def owns_toolbox(self, toolbox_id: str) -> bool:
        return toolbox_id.startswith("mcp:") and any(
            toolbox_id == f"mcp:{config.name}" for config in self.configs
        )

    def open_toolbox(self, toolbox_id: str) -> CapabilityManifest:
        config = self._config(toolbox_id)
        if not config.enabled:
            return CapabilityManifest(
                toolbox_id=toolbox_id,
                name=config.name,
                source="mcp",
                state="disabled",
                error_code="mcp_server_disabled",
                error_message="MCP server is disabled by configuration.",
                metadata={"transport": config.transport, "target": self._safe_target(config)},
            )

        snapshot = self.discovery.get_snapshot(config.name)
        state = "ready"
        if snapshot.error_code and not snapshot.tools:
            state = "unavailable"
        elif snapshot.error_code or snapshot.resource_error_code:
            state = "degraded"

        items: list[ManifestItem] = []
        for descriptor in snapshot.tools:
            capability_id = f"{toolbox_id}:{descriptor.tool_name}"
            self._descriptors[capability_id] = descriptor
            schema = descriptor.input_schema if isinstance(descriptor.input_schema, dict) else {}
            properties = schema.get("properties", {})
            parameter_names = tuple(properties) if isinstance(properties, dict) else ()
            items.append(
                ManifestItem(
                    id=capability_id,
                    toolbox_id=toolbox_id,
                    name=descriptor.tool_name,
                    kind="tool",
                    description=descriptor.description or descriptor.title or descriptor.tool_name,
                    risk_level=map_mcp_tool_risk(config, descriptor),
                    parameter_names=parameter_names,
                    metadata={"traits": list(infer_mcp_tool_traits(descriptor))},
                )
            )
        return CapabilityManifest(
            toolbox_id=toolbox_id,
            name=config.name,
            source="mcp",
            state=state,
            items=tuple(items),
            origin=snapshot.source,
            refreshed_at=snapshot.refreshed_at,
            error_code=snapshot.cause_error_code or snapshot.error_code,
            error_message=snapshot.error_message,
            metadata={
                "transport": config.transport,
                "target": self._safe_target(config),
                "tool_count": len(snapshot.tools),
                "resource_count": len(snapshot.resources),
                "resource_error_code": snapshot.resource_error_code,
            },
        )

    def activate(self, capability_id: str) -> ActivatedCapability:
        descriptor = self._descriptors.get(capability_id)
        if descriptor is None:
            raise KeyError(f"MCP capability has not been opened: {capability_id}")
        toolbox_id = ":".join(capability_id.split(":")[:2])
        config = self._config(toolbox_id)
        tool = MCPToolAdapter(config, self.manager, self.logger).adapt(descriptor)
        return ActivatedCapability(
            id=capability_id,
            toolbox_id=toolbox_id,
            kind="tool",
            tool=tool,
        )

    def _config(self, toolbox_id: str) -> MCPServerConfig:
        name = toolbox_id.split(":", 1)[1] if ":" in toolbox_id else ""
        for config in self.configs:
            if config.name == name:
                return config
        raise KeyError(f"unknown MCP toolbox: {toolbox_id}")

    def _safe_target(self, config: MCPServerConfig) -> str:
        if config.url:
            parsed = urlsplit(config.url)
            safe_netloc = parsed.netloc.rsplit("@", 1)[-1]
            return urlunsplit((parsed.scheme, safe_netloc, parsed.path, "", ""))
        return Path(config.command).name if config.command else ""
