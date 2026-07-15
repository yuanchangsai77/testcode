from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import time
from urllib.parse import quote

from ..tools.base import SimpleTool, ToolContext
from ..types import ResourceContent, ResourceDescriptor, ToolAction, ToolResult
from .config import MCPServerConfig
from .manager import MCPManager
from .types import MCPResourceDescriptor, MCPToolCallResult, MCPToolDescriptor


MAX_MODEL_TOOL_NAME_LENGTH = 64
MAX_MCP_TOOL_RESULT_CHARS = 100_000
_INVALID_MODEL_TOOL_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _serialized_chars(value: object) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(repr(value))


def build_stable_tool_name(server: MCPServerConfig, tool_name: str) -> str:
    original = f"{server.stable_prefix}__{tool_name}"
    normalized = _INVALID_MODEL_TOOL_NAME_CHARS.sub("_", original)
    if normalized == original and len(normalized) <= MAX_MODEL_TOOL_NAME_LENGTH:
        return normalized

    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:10]
    suffix = f"__{digest}"
    prefix_length = MAX_MODEL_TOOL_NAME_LENGTH - len(suffix)
    normalized_prefix = normalized[:prefix_length].rstrip("_-") or "mcp_tool"
    return f"{normalized_prefix}{suffix}"


def build_stable_resource_id(server_name: str, resource_id: str) -> str:
    encoded_server = quote(server_name, safe="")
    encoded_resource = quote(resource_id, safe="")
    return f"mcp-resource://{encoded_server}/{encoded_resource}"


def infer_mcp_tool_traits(descriptor: MCPToolDescriptor) -> tuple[str, ...]:
    annotations = descriptor.annotations
    lowered = f"{descriptor.tool_name} {descriptor.description}".lower()
    traits: set[str] = set()
    if annotations.get("destructiveHint") is True or any(
        word in lowered for word in ("delete", "destroy", "purge", "drop")
    ):
        traits.add("destructive")
    if annotations.get("readOnlyHint") is False or any(
        word in lowered for word in ("create", "update", "write", "edit", "mutate")
    ):
        traits.add("remote_write")
    if any(word in lowered for word in ("run", "execute", "trigger", "command")):
        traits.add("execute")
    if annotations.get("openWorldHint") is True or any(
        word in lowered for word in ("search", "fetch", "query", "http", "api", "web")
    ):
        traits.add("network")
    return tuple(sorted(traits))


def map_mcp_tool_risk(server: MCPServerConfig, descriptor: MCPToolDescriptor) -> str:
    override = server.risk_overrides.get(descriptor.tool_name)
    if override:
        return override

    traits = set(infer_mcp_tool_traits(descriptor))
    if "destructive" in traits:
        return "destructive"
    if "remote_write" in traits:
        return "write"
    if "execute" in traits:
        return "execute"
    if "network" in traits:
        return "network"
    # Server-provided annotations are untrusted and may raise risk, but they
    # must never lower an otherwise unknown tool to an approval-free read.
    # A configured risk override is the explicit trust boundary for that.
    return "confirm"


@dataclass(slots=True)
class MCPToolAdapter:
    server: MCPServerConfig
    manager: MCPManager
    logger: object | None = None

    def adapt(self, descriptor: MCPToolDescriptor) -> SimpleTool:
        stable_name = build_stable_tool_name(self.server, descriptor.tool_name)
        traits = infer_mcp_tool_traits(descriptor)
        risk_level = map_mcp_tool_risk(self.server, descriptor)

        def handler(action: ToolAction, _context: ToolContext) -> ToolResult:
            started = time.monotonic()
            if self.logger is not None:
                self.logger.record("mcp.tool.call", {
                    "server_name": self.server.name,
                    "tool_name": descriptor.tool_name,
                    "stable_id": stable_name,
                    "traits": traits,
                    "risk_level": risk_level,
                    "risk_overridden": descriptor.tool_name in self.server.risk_overrides,
                })
            try:
                result = self.manager.call_tool(self.server.name, descriptor.tool_name, action.arguments)
            except Exception as exc:
                result = MCPToolCallResult(
                    content=str(exc),
                    is_error=True,
                    error_code=getattr(exc, "error_code", "mcp_server_unavailable"),
                    metadata=getattr(exc, "metadata", {}),
                )
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            original_chars = len(result.content)
            structured_chars = _serialized_chars(result.structured_content)
            remote_metadata = dict(result.metadata)
            remote_metadata_chars = _serialized_chars(remote_metadata)
            output_truncated = original_chars > MAX_MCP_TOOL_RESULT_CHARS
            structured_truncated = structured_chars > MAX_MCP_TOOL_RESULT_CHARS
            remote_metadata_truncated = remote_metadata_chars > MAX_MCP_TOOL_RESULT_CHARS
            truncated = output_truncated or structured_truncated or remote_metadata_truncated
            artifact_path = None
            if truncated and self.logger is not None:
                write_artifact = getattr(self.logger, "write_artifact", None)
                if callable(write_artifact):
                    artifact_path = write_artifact(
                        f"mcp-{self.server.name}-{descriptor.tool_name}",
                        {
                            "content": result.content,
                            "structured_content": result.structured_content,
                            "remote_metadata": remote_metadata,
                        },
                    )
            output = result.content
            if output_truncated:
                location = artifact_path or "artifact unavailable"
                marker = f"\n[truncated; original_chars={original_chars}; full_result={location}]"
                output = result.content[: MAX_MCP_TOOL_RESULT_CHARS - len(marker)] + marker
            structured_content = result.structured_content
            if structured_truncated:
                structured_content = {
                    "truncated": True,
                    "original_chars": structured_chars,
                    "artifact_path": artifact_path,
                }
            bounded_remote_metadata = remote_metadata
            if remote_metadata_truncated:
                bounded_remote_metadata = {
                    "truncated": True,
                    "original_chars": remote_metadata_chars,
                    "artifact_path": artifact_path,
                }
            if self.logger is not None:
                self.logger.record("mcp.tool.result", {
                    "server_name": self.server.name,
                    "tool_name": descriptor.tool_name,
                    "stable_id": stable_name,
                    "success": not result.is_error,
                    "error_code": result.error_code,
                    "duration_ms": duration_ms,
                })
            return ToolResult(
                name=stable_name,
                success=not result.is_error,
                output=output,
                error_code=result.error_code,
                metadata={
                    "server_name": self.server.name,
                    "remote_tool_name": descriptor.tool_name,
                    "traits": traits,
                    "risk_level": risk_level,
                    "risk_overridden": descriptor.tool_name in self.server.risk_overrides,
                    "duration_ms": duration_ms,
                    "structured_content": structured_content,
                    "remote_metadata": bounded_remote_metadata,
                    "truncated": truncated,
                    "original_chars": original_chars,
                    "structured_chars": structured_chars,
                    "remote_metadata_chars": remote_metadata_chars,
                    "artifact_path": artifact_path,
                },
            )

        return SimpleTool(
            name=stable_name,
            description=descriptor.description or descriptor.title or descriptor.tool_name,
            arguments={},
            input_schema=descriptor.input_schema or {"type": "object", "properties": {}},
            risk_level=risk_level,
            handler=handler,
        )


def adapt_resource_descriptor(descriptor: MCPResourceDescriptor) -> ResourceDescriptor:
    return ResourceDescriptor(
        id=build_stable_resource_id(descriptor.server_name, descriptor.resource_id),
        name=descriptor.name,
        description=descriptor.description,
        source=descriptor.server_name,
        mime_type=descriptor.mime_type,
        metadata={"uri": descriptor.uri, "remote_metadata": dict(descriptor.metadata)},
    )


def adapt_resource_content(resource_id: str, server_name: str, text: str) -> ResourceContent:
    return ResourceContent(
        id=build_stable_resource_id(server_name, resource_id),
        text=text,
        metadata={"server_name": server_name},
    )
