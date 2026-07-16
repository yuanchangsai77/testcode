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

    def summarize(self, result: ToolResult) -> str:
        """Return a user-facing run summary for a result."""


@dataclass(slots=True)
class ToolContext:
    cwd: str
    state: dict = field(default_factory=dict)
    allowed_roots: list[str] = field(default_factory=list)
    max_output_bytes: int = 32_000


@dataclass(slots=True)
class SimpleTool:
    name: str
    description: str
    arguments: dict[str, str]
    handler: Callable[[ToolAction, ToolContext], ToolResult]
    input_schema: dict = field(default_factory=dict)
    risk_level: str = "read"
    exposed: bool = True
    # User-facing run summary only. It is local display logic and must not be
    # written into ToolResult output or metadata.
    summarizer: Callable[[ToolResult], str] | None = None

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

    def summarize(self, result: ToolResult) -> str:
        if self.summarizer is None:
            return result.output
        return self.summarizer(result)
