from __future__ import annotations

import json
from dataclasses import asdict

from ..tools.base import SimpleTool
from ..types import ToolAction, ToolResult
from .warehouse import CapabilityWarehouse, MAX_CATALOG_RESULTS


def build_warehouse_tools(warehouse: CapabilityWarehouse) -> list[SimpleTool]:
    return [
        _warehouse_list_tool(warehouse),
        _toolbox_open_tool(warehouse),
        _capability_activate_tool(warehouse),
        _capability_release_tool(warehouse),
        _capability_status_tool(warehouse),
    ]


def _warehouse_list_tool(warehouse: CapabilityWarehouse) -> SimpleTool:
    def handler(action: ToolAction, _context) -> ToolResult:
        offset = _bounded_offset(action.arguments.get("offset"))
        max_results = _bounded_int(action.arguments.get("max_results"), 8, MAX_CATALOG_RESULTS)
        entries = warehouse.list_entries(offset=offset, max_results=max_results)
        described_entries = [warehouse.describe_entry(entry) for entry in entries]
        total = len(warehouse.catalog_entries())
        next_offset = offset + len(described_entries)
        payload = {
            "entries": described_entries,
            "total": total,
            "next_offset": next_offset if next_offset < total else None,
        }
        return ToolResult(
            name=action.name,
            success=True,
            output=json.dumps(payload, ensure_ascii=False, indent=2),
            metadata={"offset": offset, "result_count": len(described_entries), "total": total},
        )

    return SimpleTool(
        name="warehouse_list",
        description=(
            "List capability warehouse entries and their purpose descriptions. "
            "Use pagination only when the catalog shown in the prompt is incomplete; choose relevance yourself."
        ),
        arguments={
            "offset": "Zero-based catalog offset. Defaults to 0.",
            "max_results": "Maximum outer catalog entries to return. Defaults to 8.",
        },
        input_schema={
            "type": "object",
            "properties": {
                "offset": {"type": "integer", "minimum": 0},
                "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_CATALOG_RESULTS},
            },
            "additionalProperties": False,
        },
        risk_level="read",
        handler=handler,
    )


def _toolbox_open_tool(warehouse: CapabilityWarehouse) -> SimpleTool:
    def handler(action: ToolAction, _context) -> ToolResult:
        toolbox_id = str(action.arguments.get("toolbox_id", "")).strip()
        try:
            manifest = warehouse.open_toolbox(toolbox_id)
        except (KeyError, ValueError) as exc:
            return _failure(action.name, exc, "capability_toolbox_unavailable")
        payload = {
            "toolbox_id": manifest.toolbox_id,
            "name": manifest.name,
            "source": manifest.source,
            "state": manifest.state,
            "origin": manifest.origin,
            "refreshed_at": manifest.refreshed_at,
            "error_code": manifest.error_code,
            "error_message": manifest.error_message,
            "items": [asdict(item) for item in manifest.items],
        }
        status = warehouse.status(toolbox_id)["entries"][0]
        payload.update(
            {
                "lifecycle_state": status["lifecycle_state"],
                "health_state": status["health_state"],
                "catalog_source": status["catalog_source"],
                "connection_state": status["connection_state"],
            }
        )
        success = manifest.state not in {"unavailable", "disabled"}
        return ToolResult(
            name=action.name,
            success=success,
            output=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            error_code=None if success else manifest.error_code or "capability_toolbox_unavailable",
            metadata={"toolbox_id": toolbox_id, "item_count": len(manifest.items)},
        )

    return SimpleTool(
        name="toolbox_open",
        description=(
            "Open one warehouse toolbox and return its bounded manifest. "
            "This reveals item summaries but does not activate their full schemas or instructions."
        ),
        arguments={"toolbox_id": "Stable toolbox id shown in the capability catalog."},
        input_schema={
            "type": "object",
            "properties": {"toolbox_id": {"type": "string"}},
            "required": ["toolbox_id"],
            "additionalProperties": False,
        },
        risk_level="read",
        handler=handler,
    )


def _capability_activate_tool(warehouse: CapabilityWarehouse) -> SimpleTool:
    def handler(action: ToolAction, _context) -> ToolResult:
        capability_ids = action.arguments.get("capability_ids", [])
        if isinstance(capability_ids, str):
            capability_ids = [capability_ids]
        if not isinstance(capability_ids, list):
            return _failure(
                action.name,
                ValueError("capability_ids must be an array of strings"),
                "capability_activation_invalid",
            )
        scope = str(action.arguments.get("scope", "run"))
        reason = str(action.arguments.get("reason", "selected for current task"))
        try:
            records = warehouse.activate(capability_ids, scope=scope, reason=reason)
        except (KeyError, ValueError) as exc:
            return _failure(action.name, exc, "capability_activation_failed")
        return ToolResult(
            name=action.name,
            success=True,
            output=json.dumps([asdict(record) for record in records], ensure_ascii=False, indent=2),
            metadata={
                "activated": [record.capability_id for record in records],
                "scope": scope,
            },
        )

    return SimpleTool(
        name="capability_activate",
        description=(
            "Activate selected items from an already opened toolbox. "
            "Activated tool schemas or Skill instructions become visible on the next model turn."
        ),
        arguments={
            "capability_ids": "Array of leaf capability ids returned by toolbox_open.",
            "scope": "Activation scope: turn, run, or session. Defaults to run.",
            "reason": "Short explanation of why these capabilities are needed now.",
        },
        input_schema={
            "type": "object",
            "properties": {
                "capability_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "scope": {"type": "string", "enum": ["turn", "run", "session"]},
                "reason": {"type": "string"},
            },
            "required": ["capability_ids"],
            "additionalProperties": False,
        },
        risk_level="read",
        handler=handler,
    )


def _capability_release_tool(warehouse: CapabilityWarehouse) -> SimpleTool:
    def handler(action: ToolAction, _context) -> ToolResult:
        capability_ids = action.arguments.get("capability_ids")
        if isinstance(capability_ids, str):
            capability_ids = [capability_ids]
        if capability_ids is not None and not isinstance(capability_ids, list):
            return _failure(
                action.name,
                ValueError("capability_ids must be an array of strings"),
                "capability_release_invalid",
            )
        released = warehouse.release(capability_ids)
        return ToolResult(
            name=action.name,
            success=True,
            output=json.dumps({"released": released}, ensure_ascii=False),
            metadata={"released": released},
        )

    return SimpleTool(
        name="capability_release",
        description="Release selected activated capabilities, or all active capabilities when ids are omitted.",
        arguments={"capability_ids": "Optional array of activated capability ids."},
        input_schema={
            "type": "object",
            "properties": {
                "capability_ids": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        risk_level="read",
        handler=handler,
    )


def _capability_status_tool(warehouse: CapabilityWarehouse) -> SimpleTool:
    def handler(action: ToolAction, _context) -> ToolResult:
        toolbox_id = action.arguments.get("toolbox_id")
        if toolbox_id is not None:
            toolbox_id = str(toolbox_id)
        try:
            payload = warehouse.status(toolbox_id)
        except KeyError as exc:
            return _failure(action.name, exc, "capability_status_unknown")
        return ToolResult(
            name=action.name,
            success=True,
            output=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        )

    return SimpleTool(
        name="capability_status",
        description="Inspect warehouse, opened toolbox, activation, and budget status on demand.",
        arguments={"toolbox_id": "Optional toolbox id to inspect."},
        input_schema={
            "type": "object",
            "properties": {"toolbox_id": {"type": "string"}},
            "additionalProperties": False,
        },
        risk_level="read",
        handler=handler,
    )


def _failure(name: str, error: Exception, error_code: str) -> ToolResult:
    return ToolResult(
        name=name,
        success=False,
        output=str(error),
        error_code=error_code,
    )


def _bounded_int(value: object, default: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def _bounded_offset(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
