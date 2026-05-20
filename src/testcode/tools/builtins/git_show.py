from __future__ import annotations

from pathlib import Path

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import retarget, run_command, schema


def tool() -> SimpleTool:
    return SimpleTool(
        name="git_show",
        description="Show a git revision or revision:path value.",
        arguments={"revision": "Revision expression to show."},
        input_schema=schema({"revision": {"type": "string"}}, required=["revision"]),
        handler=run,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    revision = str(action.arguments["revision"])
    return retarget(run_command(["git", "show", "--stat", "--patch", revision], Path(context.cwd), shell=False), action.name)
