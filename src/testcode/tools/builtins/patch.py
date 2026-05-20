from __future__ import annotations

from pathlib import Path

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import resolve_workspace_path, retarget, run_command, schema


def tool() -> SimpleTool:
    return SimpleTool(
        name="patch",
        description="Apply a unified diff inside the workspace after validating paths and context.",
        arguments={"diff": "Unified diff text to apply."},
        input_schema=schema({"diff": {"type": "string"}}, required=["diff"]),
        risk_level="write",
        handler=run,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    diff = str(action.arguments["diff"])
    changed_files = changed_files_from_diff(diff)
    if not changed_files:
        return ToolResult(name=action.name, success=False, output="diff contains no changed files", error_code="invalid_patch")

    root = Path(context.cwd).expanduser().resolve()
    for path in changed_files:
        resolved = resolve_workspace_path(context, path)
        if isinstance(resolved, ToolResult):
            return retarget(resolved, action.name)
        resolved.path.relative_to(root)

    check = run_command(["git", "apply", "--check", "-"], root, input_text=diff, shell=False)
    if not check.success:
        return ToolResult(
            name=action.name,
            success=False,
            output=check.output,
            error_code="patch_context_mismatch",
            metadata={"changed_files": changed_files},
        )

    applied = run_command(["git", "apply", "-"], root, input_text=diff, shell=False)
    if not applied.success:
        return retarget(applied, action.name)

    return ToolResult(
        name=action.name,
        success=True,
        output="applied patch:\n" + "\n".join(changed_files),
        metadata={"changed_files": changed_files},
    )


def changed_files_from_diff(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            raw_path = line[4:].strip()
            if raw_path == "/dev/null":
                continue
            if raw_path.startswith("b/"):
                raw_path = raw_path[2:]
            files.append(raw_path)
        elif line.startswith("--- "):
            raw_path = line[4:].strip()
            if raw_path == "/dev/null":
                continue
            if raw_path.startswith("a/"):
                raw_path = raw_path[2:]
            if raw_path not in files:
                files.append(raw_path)
    return sorted(set(files))
