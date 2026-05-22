from __future__ import annotations

from pathlib import Path

from ...types import ToolResult
from ..shared import run_command


def ensure_git_repository(cwd: Path, tool_name: str) -> ToolResult | None:
    result = run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd, shell=False)
    if result.success and result.metadata.get("stdout", "").strip() == "true":
        return None
    return ToolResult(
        name=tool_name,
        success=False,
        output="not a git repository",
        error_code="not_git_repository",
        metadata={
            "exit_code": result.metadata.get("exit_code"),
            "stdout": result.metadata.get("stdout", ""),
            "stderr": result.metadata.get("stderr", ""),
        },
    )


def git_failure(tool_name: str, result: ToolResult, error_code: str, fallback: str) -> ToolResult:
    stdout = result.metadata.get("stdout", "")
    stderr = result.metadata.get("stderr", "")
    return ToolResult(
        name=tool_name,
        success=False,
        output=(stderr or stdout or fallback).strip(),
        error_code=error_code,
        metadata={
            "exit_code": result.metadata.get("exit_code"),
            "stdout": stdout,
            "stderr": stderr,
        },
    )
