from __future__ import annotations

import hashlib

from ...types import ToolAction, ToolResult
from ...safety.redaction import is_sensitive_path
from ..base import SimpleTool, ToolContext
from ..shared import looks_binary, path_error, positive_int, resolve_workspace_path, retarget, schema
from ..summary import read_file_summary

MAX_READ_BYTES = 64_000


def tool() -> SimpleTool:
    return SimpleTool(
        name="read_file",
        description="Read a UTF-8 text file from the workspace.",
        arguments={
            "path": "Workspace-relative or absolute file path.",
            "max_bytes": f"Maximum bytes to read. Defaults to {MAX_READ_BYTES}.",
        },
        input_schema=schema(
            {
                "path": {"type": "string"},
                "max_bytes": {"type": "integer"},
            },
            required=["path"],
        ),
        handler=run,
        summarizer=read_file_summary,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
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

    max_bytes = positive_int(action.arguments.get("max_bytes"), MAX_READ_BYTES)
    data = resolved.path.read_bytes()
    if looks_binary(data[:4096]):
        return ToolResult(
            name=action.name,
            success=False,
            output=f"binary file refused: {resolved.path}",
            error_code="binary_file",
            metadata={"path": str(resolved.path), "size": len(data)},
        )

    chunk = data[:max_bytes]
    output = chunk.decode("utf-8", errors="replace")
    stat = resolved.path.stat()
    digest = hashlib.sha256(data).hexdigest()
    metadata = {
        "path": str(resolved.path),
        "bytes": len(chunk),
        "size": len(data),
        "sha256": digest,
        "mtime_ns": stat.st_mtime_ns,
        "truncated": len(data) > len(chunk),
    }
    context.state.setdefault("read_files", {})[str(resolved.path)] = metadata
    return ToolResult(
        name=action.name,
        success=True,
        output=output,
        metadata=metadata,
    )
