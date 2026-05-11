from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from ..types import ToolAction, ToolDefinition, ToolResult


class Tool(Protocol):
    name: str
    description: str
    arguments: dict[str, str]

    def run(self, action: ToolAction) -> ToolResult:
        """Execute a structured tool action."""

    def definition(self) -> ToolDefinition:
        """Return the normalized tool definition."""


@dataclass(slots=True)
class SimpleTool:
    name: str
    description: str
    arguments: dict[str, str]
    handler: Callable[[ToolAction], ToolResult]

    def run(self, action: ToolAction) -> ToolResult:
        return self.handler(action)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            arguments=dict(self.arguments),
        )
