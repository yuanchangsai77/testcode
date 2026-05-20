from __future__ import annotations

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import resolve_workspace_path, retarget, schema


def tool() -> SimpleTool:
    return SimpleTool(
        name="apply_change",
        description="Deprecated. Use the patch tool when it is available.",
        arguments={
            "path": "Absolute or relative path of the file to write.",
            "content": "Full text content to write to the file.",
        },
        input_schema=schema(
            {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            required=["path", "content"],
        ),
        risk_level="write",
        exposed=False,
        handler=run,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    resolved = resolve_workspace_path(context, action.arguments["path"])
    if isinstance(resolved, ToolResult):
        return retarget(resolved, action.name)
    resolved.path.parent.mkdir(parents=True, exist_ok=True)
    resolved.path.write_text(str(action.arguments.get("content", "")), encoding="utf-8")
    return ToolResult(name=action.name, success=True, output=f"wrote file: {resolved.path}", metadata={"path": str(resolved.path)})
