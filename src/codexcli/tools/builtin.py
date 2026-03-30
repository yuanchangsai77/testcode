from __future__ import annotations

from pathlib import Path

from ..types import ToolAction, ToolResult
from .base import SimpleTool
from .registry import ToolRegistry


def build_builtin_registry(logger) -> ToolRegistry:
    registry = ToolRegistry(logger=logger)
    registry.register(
        SimpleTool(
            name="workspace_summary",
            description="List the names of files and directories in a target workspace directory.",
            arguments={"cwd": "Absolute or relative path of the workspace to inspect."},
            handler=_workspace_summary,
        )
    )
    registry.register(
        SimpleTool(
            name="echo",
            description="Return the provided message unchanged.",
            arguments={"message": "Message text to echo back."},
            handler=_echo,
        )
    )
    return registry


def _workspace_summary(action: ToolAction) -> ToolResult:
    cwd = Path(action.arguments["cwd"])
    entries = sorted(path.name for path in cwd.iterdir())
    listing = ", ".join(entries) if entries else "empty workspace"
    return ToolResult(name=action.name, success=True, output=listing)


def _echo(action: ToolAction) -> ToolResult:
    message = str(action.arguments.get("message", ""))
    return ToolResult(name=action.name, success=True, output=message)
