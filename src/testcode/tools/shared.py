from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..types import ToolAction, ToolResult
from .base import ToolContext

MAX_OUTPUT_BYTES = 32_000


def schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


@dataclass(slots=True)
class ResolvedPath:
    path: Path
    root: Path


def resolve_workspace_path(context: ToolContext, raw_path: object = ".") -> ResolvedPath | ToolResult:
    root = Path(context.cwd).expanduser().resolve()
    raw = str(raw_path or ".")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)

    try:
        resolved.relative_to(root)
    except ValueError:
        return ToolResult(
            name="path",
            success=False,
            output=f"path escapes workspace: {raw}",
            error_code="path_outside_workspace",
            metadata={"path": raw, "workspace": str(root)},
        )

    return ResolvedPath(path=resolved, root=root)


def path_error(action: ToolAction, resolved: ResolvedPath, expected: str | None = None) -> ToolResult | None:
    if not resolved.path.exists():
        return ToolResult(
            name=action.name,
            success=False,
            output=f"path not found: {resolved.path}",
            error_code="path_not_found",
            metadata={"path": str(resolved.path)},
        )
    if expected == "file" and not resolved.path.is_file():
        return ToolResult(
            name=action.name,
            success=False,
            output=f"path is not a file: {resolved.path}",
            error_code="path_not_file",
            metadata={"path": str(resolved.path)},
        )
    if expected == "directory" and not resolved.path.is_dir():
        return ToolResult(
            name=action.name,
            success=False,
            output=f"path is not a directory: {resolved.path}",
            error_code="path_not_directory",
            metadata={"path": str(resolved.path)},
        )
    return None


def run_command(command, cwd: Path, *, timeout: float = 30, shell: bool = True, input_text: str | None = None) -> ToolResult:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=shell,
            text=True,
            capture_output=True,
            input=input_text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        stdout = clip(error.stdout or "")
        stderr = clip(error.stderr or "")
        return ToolResult(
            name="command",
            success=False,
            output=format_process_output(stdout, stderr, None),
            error_code="timeout",
            metadata={"timeout": timeout, "stdout": stdout, "stderr": stderr},
        )
    except FileNotFoundError as error:
        return ToolResult(name="command", success=False, output=str(error), error_code="command_not_found")

    stdout = clip(completed.stdout)
    stderr = clip(completed.stderr)
    return ToolResult(
        name="command",
        success=completed.returncode == 0,
        output=format_process_output(stdout, stderr, completed.returncode),
        error_code=None if completed.returncode == 0 else "nonzero_exit",
        metadata={"exit_code": completed.returncode, "stdout": stdout, "stderr": stderr},
    )


def format_process_output(stdout: str, stderr: str, exit_code: int | None) -> str:
    parts = []
    if exit_code is not None:
        parts.append(f"exit_code: {exit_code}")
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    return "\n".join(parts) if parts else "exit_code: 0"


def positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def looks_binary(data: bytes) -> bool:
    return b"\0" in data


def clip(value: str | bytes) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return value
    return encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace") + "\n...truncated..."


def retarget(result: ToolResult, name: str) -> ToolResult:
    result.name = name
    return result
