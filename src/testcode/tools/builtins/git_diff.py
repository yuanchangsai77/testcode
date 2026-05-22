from __future__ import annotations

from pathlib import Path

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import resolve_workspace_path, retarget, run_command, schema
from .git_common import ensure_git_repository, git_failure


def tool() -> SimpleTool:
    return SimpleTool(
        name="git_diff",
        description="Return the working tree git diff.",
        arguments={"path": "Optional workspace-relative path to diff."},
        input_schema=schema({"path": {"type": "string"}}),
        handler=run,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    cwd = Path(context.cwd)
    repository_error = ensure_git_repository(cwd, action.name)
    if repository_error is not None:
        return repository_error

    command = ["git", "diff"]
    diff_path = None
    if "path" in action.arguments:
        resolved = resolve_workspace_path(context, action.arguments["path"])
        if isinstance(resolved, ToolResult):
            return retarget(resolved, action.name)
        diff_path = str(resolved.path.relative_to(resolved.root))
        command.extend(["--", diff_path])

    result = run_command(command, cwd, shell=False)
    if not result.success:
        return git_failure(action.name, result, "git_diff_failed", "git diff failed")

    diff = result.metadata.get("stdout", "")
    return ToolResult(
        name=action.name,
        success=True,
        output=diff or "no diff",
        metadata={
            "exit_code": result.metadata.get("exit_code"),
            "path": diff_path,
            "has_changes": bool(diff),
            "stdout": diff,
            "stderr": result.metadata.get("stderr", ""),
        },
    )
