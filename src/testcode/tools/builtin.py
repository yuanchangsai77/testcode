from __future__ import annotations

from pathlib import Path
from tempfile import gettempdir

from ..types import ToolAction, ToolResult
from .base import SimpleTool
from .registry import ToolRegistry


def build_builtin_registry(logger) -> ToolRegistry:
    registry = ToolRegistry(logger=logger)
    registry.register(
        SimpleTool(
            name="inspect",
            description="Inspect a path in the workspace. Use it to list a directory or read a text file.",
            arguments={
                "path": "Absolute or relative path to inspect. Defaults to the current workspace root.",
            },
            handler=_inspect,
        )
    )
    registry.register(
        SimpleTool(
            name="scratchpad",
            description="Write temporary text content to a scratch file and return its path.",
            arguments={
                "name": "Short scratch file name, for example notes.txt.",
                "content": "Text content to write into the scratch file.",
            },
            handler=_scratchpad,
        )
    )
    registry.register(
        SimpleTool(
            name="apply_change",
            description="Write text content to a target file path in the workspace.",
            arguments={
                "path": "Absolute or relative path of the file to write.",
                "content": "Full text content to write to the file.",
            },
            handler=_apply_change,
        )
    )
    return registry


def _inspect(action: ToolAction) -> ToolResult:
    raw_path = str(action.arguments.get("path", "")).strip() or "."
    path = Path(raw_path)

    if path.is_dir():
        entries = sorted(child.name for child in path.iterdir())
        listing = ", ".join(entries) if entries else "empty directory"
        output = f"directory {path.resolve()}: {listing}"
        return ToolResult(name=action.name, success=True, output=output)

    if path.is_file():
        output = path.read_text(encoding="utf-8")
        return ToolResult(name=action.name, success=True, output=output)

    return ToolResult(name=action.name, success=False, output=f"path not found: {path}")


def _scratchpad(action: ToolAction) -> ToolResult:
    name = str(action.arguments.get("name", "scratch.txt")).strip() or "scratch.txt"
    content = str(action.arguments.get("content", ""))
    path = Path(gettempdir()) / "testcode-scratch" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    output = f"wrote scratch file: {path}"
    return ToolResult(name=action.name, success=True, output=output)


def _apply_change(action: ToolAction) -> ToolResult:
    path = Path(str(action.arguments["path"]))
    content = str(action.arguments.get("content", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    output = f"wrote file: {path}"
    return ToolResult(name=action.name, success=True, output=output)
