from __future__ import annotations

from ...types import ToolAction, ToolResult
from ...safety.redaction import is_sensitive_path
from ..base import SimpleTool, ToolContext
from ..shared import looks_binary, path_error, positive_int, resolve_workspace_path, retarget, schema
from ..summary import read_file_summary
from ..observation_state import record_observation, snapshot

MAX_READ_BYTES = 1_048_576
DEFAULT_READ_BYTES = 64_000


def tool(default_max_bytes: int = DEFAULT_READ_BYTES) -> SimpleTool:
    default_max_bytes = min(max(1, int(default_max_bytes)), MAX_READ_BYTES)
    return SimpleTool(
        name="read_file",
        description="Read a UTF-8 text file from the workspace.",
        arguments={
            "path": "Workspace-relative or absolute file path.",
            "max_bytes": f"Maximum bytes to read. Defaults to {default_max_bytes}; hard limit {MAX_READ_BYTES}.",
            "start_line": "Optional first line to return, starting at 1.",
            "end_line": "Optional last line to return, inclusive.",
        },
        input_schema=schema(
            {
                "path": {"type": "string"},
                "max_bytes": {"type": "integer"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            required=["path"],
        ),
        handler=lambda action, context: run(action, context, default_max_bytes),
        summarizer=read_file_summary,
    )


def run(action: ToolAction, context: ToolContext, default_max_bytes: int = DEFAULT_READ_BYTES) -> ToolResult:
    resolved = resolve_workspace_path(context, action.arguments["path"])
    if isinstance(resolved, ToolResult):
        return retarget(resolved, action.name)
    if error := path_error(action, resolved, "file"):
        return error
    if is_sensitive_path(resolved.path):
        return ToolResult(
            name=action.name,
            success=False,
            output=f"sensitive file refused: {resolved.path}",
            error_code="sensitive_file",
            metadata={"path": str(resolved.path)},
        )

    max_bytes = min(positive_int(action.arguments.get("max_bytes"), default_max_bytes), MAX_READ_BYTES)
    data, digest, mtime_ns = snapshot(resolved.path)
    if looks_binary(data[:4096]):
        return ToolResult(
            name=action.name,
            success=False,
            output=f"binary file refused: {resolved.path}",
            error_code="binary_file",
            metadata={"path": str(resolved.path), "size": len(data)},
        )

    decoded = data.decode("utf-8", errors="replace")
    source_lines = decoded.splitlines(keepends=True)
    total_lines = len(source_lines)
    start_line = int(action.arguments.get("start_line", 1))
    end_line = int(action.arguments.get("end_line", total_lines or 1))
    if end_line < start_line:
        return ToolResult(
            name=action.name,
            success=False,
            output="end_line must be greater than or equal to start_line",
            error_code="invalid_argument_value",
            metadata={"start_line": start_line, "end_line": end_line},
        )

    selected = "".join(source_lines[start_line - 1 : end_line])
    selected_bytes = selected.encode("utf-8")
    chunk = selected_bytes[:max_bytes]
    output = chunk.decode("utf-8", errors="replace")
    clipped = len(selected_bytes) > len(chunk)
    visible_lines = output.splitlines()
    if clipped and output and not output.endswith(("\n", "\r")):
        visible_lines = visible_lines[:-1]
    observed = {
        start_line + index: content
        for index, content in enumerate(visible_lines)
    }
    record_observation(
        context.state,
        resolved.path,
        sha256=digest,
        mtime_ns=mtime_ns,
        lines=observed,
        empty=not data and start_line == 1,
    )
    truncated = clipped or start_line > 1 or end_line < total_lines
    metadata = {
        "path": str(resolved.path),
        "bytes": len(chunk),
        "size": len(data),
        "sha256": digest,
        "mtime_ns": mtime_ns,
        "truncated": truncated,
        "start_line": start_line,
        "end_line": start_line + len(visible_lines) - 1 if visible_lines else start_line - 1,
        "total_lines": total_lines,
    }
    return ToolResult(
        name=action.name,
        success=True,
        output=output,
        metadata=metadata,
    )
