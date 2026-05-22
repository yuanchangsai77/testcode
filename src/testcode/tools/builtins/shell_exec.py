from __future__ import annotations

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import path_error, resolve_workspace_path, retarget, run_command, schema
from ..summary import process_result_summary


def tool() -> SimpleTool:
    return SimpleTool(
        name="shell_exec",
        description="Execute a shell command in the workspace and return stdout, stderr, and exit code.",
        arguments={
            "command": "Command to execute.",
            "cwd": "Optional workspace-relative working directory.",
            "timeout": "Timeout in seconds. Defaults to 30.",
        },
        input_schema=schema(
            {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout": {"type": "number"},
            },
            required=["command"],
        ),
        risk_level="execute",
        handler=run,
        summarizer=process_result_summary,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    cwd = resolve_workspace_path(context, action.arguments.get("cwd", "."))
    if isinstance(cwd, ToolResult):
        return retarget(cwd, action.name)
    if error := path_error(action, cwd, "directory"):
        return error
    timeout = float(action.arguments.get("timeout", 30))
    return retarget(run_command(str(action.arguments["command"]), cwd.path, timeout=timeout), action.name)
