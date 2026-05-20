from __future__ import annotations

from pathlib import Path

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import resolve_workspace_path, retarget, run_command, schema


def tool() -> SimpleTool:
    return SimpleTool(
        name="git_diff",
        description="Return the working tree git diff.",
        arguments={"path": "Optional workspace-relative path to diff."},
        input_schema=schema({"path": {"type": "string"}}),
        handler=run,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    command = ["git", "diff"]
    if "path" in action.arguments:
        resolved = resolve_workspace_path(context, action.arguments["path"])
        if isinstance(resolved, ToolResult):
            return retarget(resolved, action.name)
        command.extend(["--", str(resolved.path.relative_to(resolved.root))])
    return retarget(run_command(command, Path(context.cwd), shell=False), action.name)
