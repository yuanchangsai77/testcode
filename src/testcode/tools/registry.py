from __future__ import annotations

from ..types import ToolAction, ToolDefinition, ToolResult
from .base import ToolContext


class ToolRegistry:
    def __init__(self, logger) -> None:
        self._tools = {}
        self._logger = logger

    def register(self, tool) -> None:
        self._tools[tool.name] = tool

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition() for tool in self._tools.values() if getattr(tool, "exposed", True)]

    def definition_for(self, name: str) -> ToolDefinition | None:
        tool = self._tools.get(name)
        if tool is None:
            return None
        return tool.definition()

    def execute(self, action: ToolAction, *, cwd: str = ".") -> ToolResult:
        tool = self._tools.get(action.name)
        if tool is None:
            result = ToolResult(
                name=action.name,
                success=False,
                output=f"unknown tool: {action.name}",
                error_code="unknown_tool",
            )
            self._record_result(result)
            return result

        validation_error = self._validate(action, getattr(tool, "input_schema", {}))
        if validation_error is not None:
            self._record_result(validation_error)
            return validation_error

        self._logger.record("tool.execute", {"name": action.name, "arguments": action.arguments})
        result = tool.run(action, ToolContext(cwd=cwd))
        self._record_result(result)
        return result

    def _validate(self, action: ToolAction, schema: dict) -> ToolResult | None:
        required = set(schema.get("required", []))
        properties = set(schema.get("properties", {}).keys())
        additional = schema.get("additionalProperties", True)

        missing = sorted(name for name in required if name not in action.arguments)
        if missing:
            return ToolResult(
                name=action.name,
                success=False,
                output=f"missing required argument(s): {', '.join(missing)}",
                error_code="missing_argument",
                metadata={"missing": missing},
            )

        if additional is False:
            unknown = sorted(name for name in action.arguments if name not in properties)
            if unknown:
                return ToolResult(
                    name=action.name,
                    success=False,
                    output=f"unknown argument(s): {', '.join(unknown)}",
                    error_code="unknown_argument",
                    metadata={"unknown": unknown},
                )

        return None

    def _record_result(self, result: ToolResult) -> None:
        self._logger.record(
            "tool.result",
            {
                "name": result.name,
                "success": result.success,
                "output": result.output,
                "error_code": result.error_code,
                "metadata": result.metadata,
            },
        )
