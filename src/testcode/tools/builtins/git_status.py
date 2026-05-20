from __future__ import annotations

from pathlib import Path

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import run_command, schema


def tool() -> SimpleTool:
    return SimpleTool(
        name="git_status",
        description="Return the current git branch and porcelain status.",
        arguments={},
        input_schema=schema({}),
        handler=run,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    branch = run_command(["git", "branch", "--show-current"], Path(context.cwd), shell=False)
    status = run_command(["git", "status", "--porcelain"], Path(context.cwd), shell=False)
    if not branch.success or not status.success:
        return ToolResult(
            name=action.name,
            success=False,
            output=(branch.output or status.output),
            error_code="not_git_repository",
            metadata={"exit_code": branch.metadata.get("exit_code", status.metadata.get("exit_code"))},
        )
    output = f"branch: {branch.output.strip() or '(detached)'}\n{status.output.strip()}".rstrip()
    return ToolResult(name=action.name, success=True, output=output, metadata={"branch": branch.output.strip()})
