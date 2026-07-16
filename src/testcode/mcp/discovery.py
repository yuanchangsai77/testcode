from __future__ import annotations

import time
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..safety.redaction import redact, redact_text
from .manager import MCPManager
from .types import (
    MAX_MCP_RESOURCES_PER_SERVER,
    MAX_MCP_TOOLS_PER_SERVER,
    MCPDiscoverySnapshot,
    MCPResourceDescriptor,
    MCPToolDescriptor,
)

MAX_MCP_DESCRIPTOR_CHARS = 100_000
MAX_MCP_DISCOVERY_CACHE_BYTES = 128 * 1024 * 1024


@dataclass(slots=True)
class MCPDiscoveryService:
    manager: MCPManager
    _snapshots: dict[str, MCPDiscoverySnapshot] = field(default_factory=dict)
    cache_ttl: float = 60.0
    logger: object | None = None
    cache_path: Path | None = None
    max_tools_per_server: int = MAX_MCP_TOOLS_PER_SERVER

    def __post_init__(self) -> None:
        self._load_cache()

    def get_snapshot(self, server_name: str) -> MCPDiscoverySnapshot:
        snapshot = self._snapshots.get(server_name)
        if snapshot is not None and time.time() - snapshot.refreshed_at < self.cache_ttl:
            return snapshot
        return self.refresh(server_name)

    def peek_snapshot(self, server_name: str) -> MCPDiscoverySnapshot | None:
        """Return the current snapshot without triggering network discovery."""
        return self._snapshots.get(server_name)

    def refresh(self, server_name: str) -> MCPDiscoverySnapshot:
        previous = self._snapshots.get(server_name)
        try:
            client = self.manager.get_client(server_name)
            tools = self._bounded_tools(server_name, client.list_tools())
        except Exception as exc:
            self.manager.invalidate(server_name)
            cause_error_code = getattr(exc, "error_code", "mcp_tool_list_failed")
            snapshot = MCPDiscoverySnapshot(
                server_name=server_name,
                tools=previous.tools if previous is not None else (),
                resources=previous.resources if previous is not None else (),
                error_code="mcp_server_unavailable",
                error_message=redact_text(str(exc)),
                cause_error_code=cause_error_code,
                error_details=self._error_details(exc),
                refreshed_at=time.time(),
                source="stale" if previous is not None and (previous.tools or previous.resources) else "live",
            )
            if self.logger is not None:
                self.logger.record("mcp.server.error", {
                    "server_name": server_name,
                    "error_code": cause_error_code,
                    "error": str(exc),
                })
            self._snapshots[server_name] = snapshot
            self._save_cache()
            return snapshot

        resource_error = ""
        resource_error_code = None
        try:
            resources = self._bounded_resources(server_name, client.list_resources())
        except Exception as exc:
            self.manager.invalidate(server_name)
            resources = ()
            resource_error = redact_text(str(exc))
            resource_error_code = getattr(exc, "error_code", "mcp_resource_list_failed")
            if self.logger is not None:
                self.logger.record("mcp.resource.error", {
                    "server_name": server_name,
                    "error_code": getattr(exc, "error_code", "mcp_resource_list_failed"),
                    "error": str(exc),
                })
        snapshot = MCPDiscoverySnapshot(
            server_name=server_name,
            tools=tools,
            resources=resources,
            error_message=resource_error,
            resource_error_code=resource_error_code,
            resource_error_message=resource_error,
            refreshed_at=time.time(),
            source="live",
        )
        self._snapshots[server_name] = snapshot
        self._save_cache()
        if self.logger is not None:
            self.logger.record("mcp.tools.discovered", {
                "server_name": server_name,
                "tool_count": len(tools),
                "resource_count": len(resources),
                "resource_error": bool(resource_error),
            })
        return snapshot

    def all_snapshots(self) -> tuple[MCPDiscoverySnapshot, ...]:
        return tuple(self.get_snapshot(server_name) for server_name in self.manager.configs)

    def _bounded_tools(
        self,
        server_name: str,
        tools: tuple[MCPToolDescriptor, ...],
    ) -> tuple[MCPToolDescriptor, ...]:
        accepted: list[MCPToolDescriptor] = []
        dropped = 0
        for tool in tools:
            size = len(json.dumps({
                "server_name": tool.server_name,
                "tool_name": tool.tool_name,
                "title": tool.title,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "annotations": tool.annotations,
            }, ensure_ascii=False, default=str))
            if size > MAX_MCP_DESCRIPTOR_CHARS or len(accepted) >= self.max_tools_per_server:
                dropped += 1
                continue
            accepted.append(tool)
        if dropped and self.logger is not None:
            self.logger.record("mcp.tools.truncated", {
                "server_name": server_name,
                "accepted": len(accepted),
                "dropped": dropped,
                "max_items": self.max_tools_per_server,
                "max_descriptor_chars": MAX_MCP_DESCRIPTOR_CHARS,
            })
        return tuple(accepted)

    def _bounded_resources(
        self,
        server_name: str,
        resources: tuple[MCPResourceDescriptor, ...],
    ) -> tuple[MCPResourceDescriptor, ...]:
        accepted: list[MCPResourceDescriptor] = []
        dropped = 0
        for resource in resources:
            size = len(json.dumps({
                "server_name": resource.server_name,
                "resource_id": resource.resource_id,
                "name": resource.name,
                "uri": resource.uri,
                "description": resource.description,
                "mime_type": resource.mime_type,
                "metadata": resource.metadata,
            }, ensure_ascii=False, default=str))
            if size > MAX_MCP_DESCRIPTOR_CHARS or len(accepted) >= MAX_MCP_RESOURCES_PER_SERVER:
                dropped += 1
                continue
            accepted.append(resource)
        if dropped and self.logger is not None:
            self.logger.record("mcp.resources.truncated", {
                "server_name": server_name,
                "accepted": len(accepted),
                "dropped": dropped,
                "max_items": MAX_MCP_RESOURCES_PER_SERVER,
                "max_descriptor_chars": MAX_MCP_DESCRIPTOR_CHARS,
            })
        return tuple(accepted)

    def _load_cache(self) -> None:
        if self.cache_path is None or not self.cache_path.exists():
            return
        try:
            with self.cache_path.open("rb") as cache_file:
                raw_cache = cache_file.read(MAX_MCP_DISCOVERY_CACHE_BYTES + 1)
            if len(raw_cache) > MAX_MCP_DISCOVERY_CACHE_BYTES:
                self._snapshots.clear()
                return
            payload = json.loads(raw_cache.decode("utf-8"))
            if not isinstance(payload, dict):
                self._snapshots.clear()
                return
            for server_name, raw in payload.items():
                if not isinstance(server_name, str) or not isinstance(raw, dict):
                    continue
                if server_name not in self.manager.configs:
                    continue
                raw_tools = raw.get("tools", [])
                raw_resources = raw.get("resources", [])
                if not isinstance(raw_tools, list) or not isinstance(raw_resources, list):
                    continue
                tools = tuple(
                    MCPToolDescriptor(**item)
                    for item in raw_tools[: self.max_tools_per_server + 1]
                    if isinstance(item, dict)
                )
                resources = tuple(
                    MCPResourceDescriptor(**item)
                    for item in raw_resources[: MAX_MCP_RESOURCES_PER_SERVER + 1]
                    if isinstance(item, dict)
                )
                self._snapshots[server_name] = MCPDiscoverySnapshot(
                    server_name=server_name,
                    tools=self._bounded_tools(server_name, tools),
                    resources=self._bounded_resources(server_name, resources),
                    error_code=self._optional_string(raw.get("error_code")),
                    error_message=self._string_value(raw.get("error_message")),
                    cause_error_code=self._optional_string(raw.get("cause_error_code")),
                    error_details=(
                        redact(dict(raw.get("error_details", {})))
                        if isinstance(raw.get("error_details", {}), dict)
                        else {}
                    ),
                    resource_error_code=self._optional_string(raw.get("resource_error_code")),
                    resource_error_message=self._string_value(raw.get("resource_error_message")),
                    refreshed_at=float(raw.get("refreshed_at", 0.0)),
                    source="cache",
                )
        except (AttributeError, OSError, UnicodeError, ValueError, TypeError):
            self._snapshots.clear()

    def _save_cache(self) -> None:
        if self.cache_path is None:
            return
        payload = {}
        for server_name, snapshot in self._snapshots.items():
            payload[server_name] = {
                "refreshed_at": snapshot.refreshed_at,
                "error_code": snapshot.error_code,
                "error_message": redact_text(snapshot.error_message),
                "cause_error_code": snapshot.cause_error_code,
                "error_details": redact(snapshot.error_details),
                "resource_error_code": snapshot.resource_error_code,
                "resource_error_message": redact_text(snapshot.resource_error_message),
                "tools": [
                    {
                        "server_name": item.server_name,
                        "tool_name": item.tool_name,
                        "title": item.title,
                        "description": item.description,
                        "input_schema": item.input_schema,
                        "annotations": item.annotations,
                    }
                    for item in snapshot.tools
                ],
                "resources": [
                    {
                        "server_name": item.server_name,
                        "resource_id": item.resource_id,
                        "name": item.name,
                        "uri": item.uri,
                        "description": item.description,
                        "mime_type": item.mime_type,
                        "metadata": item.metadata,
                    }
                    for item in snapshot.resources
                ],
            }
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except (OSError, TypeError, ValueError):
            return

    def _optional_string(self, value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    def _string_value(self, value: object) -> str:
        return redact_text(value) if isinstance(value, str) else ""

    def _error_details(self, error: Exception) -> dict[str, object]:
        metadata = getattr(error, "metadata", {})
        if not isinstance(metadata, dict):
            return {}
        return redact(dict(metadata))
