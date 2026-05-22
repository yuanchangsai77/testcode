from __future__ import annotations

from pathlib import Path

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import run_command, schema
from .git_common import ensure_git_repository, git_failure


def tool() -> SimpleTool:
    return SimpleTool(
        name="git_status",
        description="Return the current git branch and porcelain status.",
        arguments={},
        input_schema=schema({}),
        handler=run,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    cwd = Path(context.cwd)
    repository_error = ensure_git_repository(cwd, action.name)
    if repository_error is not None:
        return repository_error

    branch = run_command(["git", "branch", "--show-current"], cwd, shell=False)
    status = run_command(["git", "status", "--porcelain"], cwd, shell=False)
    if not branch.success:
        return git_failure(action.name, branch, "git_status_failed", "git branch failed")
    if not status.success:
        return git_failure(action.name, status, "git_status_failed", "git status failed")

    branch_name = branch.metadata.get("stdout", "").strip() or "(detached)"
    porcelain = status.metadata.get("stdout", "").rstrip("\n")
    changes = _parse_porcelain(porcelain)
    if changes:
        output = f"branch: {branch_name}\nchanges:\n" + "\n".join(
            f"{change['status']} {change['path']}" for change in changes
        )
    else:
        output = f"branch: {branch_name}\nstatus: clean"
    return ToolResult(
        name=action.name,
        success=True,
        output=output,
        metadata={
            "branch": branch_name,
            "clean": not changes,
            "changed_files": changes,
            "porcelain": porcelain,
        },
    )


def _parse_porcelain(output: str) -> list[dict[str, str]]:
    changes = []
    for line in output.splitlines():
        if not line:
            continue
        status = line[:2].strip() or line[:2]
        path = line[3:] if len(line) > 3 else ""
        changes.append({"status": status, "path": path})
    return changes
