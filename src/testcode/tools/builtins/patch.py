from __future__ import annotations

import hashlib
from pathlib import Path

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import ResolvedPath, resolve_workspace_path, retarget, run_command, schema
from ..summary import patch_summary

MAX_PATCH_FILES = 20
MAX_PATCH_LINES = 2_000


def tool() -> SimpleTool:
    return SimpleTool(
        name="patch",
        description="Apply a unified diff inside the workspace after validating paths and context.",
        arguments={"diff": "Unified diff text to apply."},
        input_schema=schema({"diff": {"type": "string"}}, required=["diff"]),
        risk_level="write",
        handler=run,
        summarizer=patch_summary,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    diff = str(action.arguments["diff"])
    lines = diff.splitlines()
    if len(lines) > MAX_PATCH_LINES:
        return ToolResult(
            name=action.name,
            success=False,
            output=f"patch exceeds maximum line count: {len(lines)} > {MAX_PATCH_LINES}",
            error_code="patch_too_large",
            metadata={"line_count": len(lines), "max_lines": MAX_PATCH_LINES},
        )

    changed_files = changed_files_from_diff(diff)
    if not changed_files:
        return ToolResult(name=action.name, success=False, output="diff contains no changed files", error_code="invalid_patch")
    if len(changed_files) > MAX_PATCH_FILES:
        return ToolResult(
            name=action.name,
            success=False,
            output=f"patch changes too many files: {len(changed_files)} > {MAX_PATCH_FILES}",
            error_code="patch_too_large",
            metadata={"changed_files": changed_files, "max_files": MAX_PATCH_FILES},
        )

    resolved_files = {}
    for path in changed_files:
        resolved = resolve_workspace_path(context, path)
        if isinstance(resolved, ToolResult):
            return retarget(resolved, action.name)
        resolved_files[path] = resolved
        if resolved.path.exists():
            read_error = validate_file_was_read(action.name, resolved.path, context.state)
            if read_error is not None:
                read_error.metadata.setdefault("changed_files", changed_files)
                return read_error

    roots = {resolved.root for resolved in resolved_files.values()}
    if len(roots) != 1:
        return ToolResult(
            name=action.name,
            success=False,
            output="a patch may only modify files within one authorized workspace root",
            error_code="patch_multiple_roots",
            metadata={"changed_files": changed_files, "roots": sorted(str(root) for root in roots)},
        )

    root = roots.pop()
    diff = rebase_diff_paths(diff, resolved_files, root)

    check = run_command(["git", "apply", "--check", "-"], root, input_text=diff, shell=False)
    if not check.success:
        error_code = classify_git_apply_failure(check.output)
        return ToolResult(
            name=action.name,
            success=False,
            output=format_patch_failure(check.output, error_code),
            error_code=error_code,
            metadata={"changed_files": changed_files, "preview": diff, "line_stats": line_stats(diff)},
        )

    applied = run_command(["git", "apply", "-"], root, input_text=diff, shell=False)
    if not applied.success:
        return retarget(applied, action.name)

    return ToolResult(
        name=action.name,
        success=True,
        output="applied patch:\n" + "\n".join(changed_files),
        metadata={"changed_files": changed_files, "preview": diff, "line_stats": line_stats(diff)},
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


def rebase_diff_paths(diff: str, resolved_files: dict[str, ResolvedPath], root: Path) -> str:
    """Make accepted absolute paths relative to the root used by git apply."""
    relative_paths = {
        path: resolved.path.relative_to(root).as_posix()
        for path, resolved in resolved_files.items()
    }
    rewritten = []
    for line in diff.splitlines():
        if line.startswith(("--- ", "+++ ")):
            raw_path = line[4:].strip()
            path = _diff_path(raw_path)
            relative_path = relative_paths.get(path)
            if relative_path is not None:
                prefix = "a/" if line.startswith("--- ") else "b/"
                line = line[:4] + prefix + relative_path
        rewritten.append(line)
    return "\n".join(rewritten) + ("\n" if diff.endswith("\n") else "")


def _diff_path(raw_path: str) -> str:
    if raw_path.startswith(("a/", "b/")):
        return raw_path[2:]
    return raw_path


def validate_file_was_read(tool_name: str, path: Path, state: dict) -> ToolResult | None:
    read_files = state.get("read_files", {})
    previous = read_files.get(str(path))
    if not previous:
        return ToolResult(
            name=tool_name,
            success=False,
            output=f"patch target must be read before modification: {path}",
            error_code="file_not_read",
            metadata={"path": str(path)},
        )

    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    stat = path.stat()
    if digest != previous.get("sha256") or stat.st_mtime_ns != previous.get("mtime_ns"):
        return ToolResult(
            name=tool_name,
            success=False,
            output=f"patch target changed after last read: {path}",
            error_code="file_changed_since_read",
            metadata={
                "path": str(path),
                "previous_sha256": previous.get("sha256"),
                "current_sha256": digest,
                "previous_mtime_ns": previous.get("mtime_ns"),
                "current_mtime_ns": stat.st_mtime_ns,
            },
        )

    return None


def classify_git_apply_failure(output: str) -> str:
    lowered = output.lower()
    if "corrupt patch" in lowered or "unrecognized input" in lowered:
        return "patch_syntax_error"
    return "patch_context_mismatch"


def format_patch_failure(output: str, error_code: str) -> str:
    if error_code == "patch_syntax_error":
        return "patch syntax error; generate a valid unified diff and retry.\n" + output
    return output


def line_stats(diff: str) -> dict[str, int]:
    added = 0
    removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return {"added": added, "removed": removed}
