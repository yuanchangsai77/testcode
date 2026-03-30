from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class UserRequest:
    prompt: str
    cwd: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    arguments: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ToolAction:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelReply:
    message: str
    actions: list[ToolAction] = field(default_factory=list)
    done: bool = False


@dataclass(slots=True)
class ToolResult:
    name: str
    success: bool
    output: str


@dataclass(slots=True)
class ExecutionSummary:
    final_message: str
    tool_results: list[ToolResult]
