from __future__ import annotations

from ..types import ToolAction, ToolDefinition, ToolResult


class ToolRegistry:
    def __init__(self, logger) -> None:
        self._tools = {}
        self._logger = logger

    def register(self, tool) -> None:
        self._tools[tool.name] = tool

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition() for tool in self._tools.values()]

    def execute(self, action: ToolAction) -> ToolResult:
        tool = self._tools.get(action.name)
        if tool is None:
            result = ToolResult(
                name=action.name,
                success=False,
                output=f"unknown tool: {action.name}",
            )
            self._logger.record(
                "tool.result",
                {"name": result.name, "success": result.success, "output": result.output},
            )
            return result

        self._logger.record("tool.execute", {"name": action.name, "arguments": action.arguments})
        result = tool.run(action)
        self._logger.record(
            "tool.result",
            {"name": result.name, "success": result.success, "output": result.output},
        )
        return result
