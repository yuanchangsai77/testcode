from __future__ import annotations

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import path_error, positive_int, resolve_workspace_path, retarget, schema
from ..summary import list_dir_summary

MAX_LIST_ENTRIES = 200


def tool() -> SimpleTool:
    return SimpleTool(
        name="list_dir",
        description="List a workspace directory.",
        arguments={
            "path": "Workspace-relative or absolute path. Defaults to '.'.",
            "max_entries": f"Maximum entries to return. Defaults to {MAX_LIST_ENTRIES}.",
        },
        input_schema=schema(
            {
                "path": {"type": "string"},
                "max_entries": {"type": "integer"},
            }
        ),
        handler=run,
        summarizer=list_dir_summary,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    resolved = resolve_workspace_path(context, action.arguments.get("path", "."))
    if isinstance(resolved, ToolResult):
        return retarget(resolved, action.name)
    if error := path_error(action, resolved, "directory"):
        return error

    max_entries = positive_int(action.arguments.get("max_entries"), MAX_LIST_ENTRIES)
    children = sorted(resolved.path.iterdir(), key=lambda item: item.name)
    shown = children[:max_entries]
    lines = [f"{child.name}/" if child.is_dir() else child.name for child in shown]
    output = "\n".join(lines) if lines else "empty directory"
    return ToolResult(
        name=action.name,
        success=True,
        output=output,
        metadata={
            "path": str(resolved.path),
            "count": len(shown),
            "truncated": len(children) > len(shown),
        },
    )
