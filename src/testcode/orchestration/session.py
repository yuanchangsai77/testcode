from __future__ import annotations

from dataclasses import dataclass, field

from ..types import ToolDefinition, ToolResult, UserRequest


@dataclass(slots=True)
class SessionContext:
    request: UserRequest
    available_tools: list[ToolDefinition] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)

    def add_model_message(self, message: str) -> None:
        self.history.append(f"model: {message}")

    def add_tool_result(self, result: ToolResult) -> None:
        self.tool_results.append(result)
        self.history.append(f"tool:{result.name}: {result.output}")
