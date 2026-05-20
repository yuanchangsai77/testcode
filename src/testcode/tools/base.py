from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..types import ToolAction, ToolDefinition, ToolResult


class Tool(Protocol):
    name: str
    description: str
    arguments: dict[str, str]
    input_schema: dict
    risk_level: str
    exposed: bool

    def run(self, action: ToolAction, context: "ToolContext") -> ToolResult:
        """Execute a structured tool action."""

    def definition(self) -> ToolDefinition:
        """Return the normalized tool definition."""


@dataclass(slots=True)
class ToolContext:
    cwd: str


@dataclass(slots=True)
class SimpleTool:
    name: str
    description: str
    arguments: dict[str, str]
    handler: Callable[[ToolAction, ToolContext], ToolResult]
    input_schema: dict = field(default_factory=dict)
    risk_level: str = "read"
    exposed: bool = True

    def run(self, action: ToolAction, context: ToolContext) -> ToolResult:
        return self.handler(action, context)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            arguments=dict(self.arguments),
            input_schema=dict(self.input_schema),
            risk_level=self.risk_level,
        )
