from __future__ import annotations

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import path_error, resolve_workspace_path, retarget, schema


def tool() -> SimpleTool:
    return SimpleTool(
        name="file_info",
        description="Return file type, size, and mtime for a workspace path.",
        arguments={"path": "Workspace-relative or absolute path."},
        input_schema=schema({"path": {"type": "string"}}, required=["path"]),
        handler=run,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    resolved = resolve_workspace_path(context, action.arguments["path"])
    if isinstance(resolved, ToolResult):
        return retarget(resolved, action.name)
    if error := path_error(action, resolved):
        return error

    stat = resolved.path.stat()
    if resolved.path.is_dir():
        kind = "directory"
    elif resolved.path.is_file():
        kind = "file"
    else:
        kind = "other"
    return ToolResult(
        name=action.name,
        success=True,
        output=f"{kind} {resolved.path} size={stat.st_size} mtime={int(stat.st_mtime)}",
        metadata={"path": str(resolved.path), "type": kind, "size": stat.st_size, "mtime": stat.st_mtime},
    )
