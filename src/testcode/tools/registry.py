from __future__ import annotations

import math

from ..types import ToolAction, ToolDefinition, ToolResult
from .base import ToolContext


class ToolRegistry:
    def __init__(
        self,
        logger,
        max_output_bytes: int = 32_000,
        interceptors: list | None = None,
    ) -> None:
        self._tools = {}
        self._logger = logger
        self._state = {}
        self._persistent_state_names: set[str] = set()
        self._providers = []
        self._provider_tool_owners: dict[str, int] = {}
        self._max_output_bytes = max(1, int(max_output_bytes))
        self._interceptors = list(interceptors or [])

    def register_interceptor(self, interceptor) -> None:
        self._interceptors.append(interceptor)

    def register(self, tool) -> bool:
        if tool.name in self._tools:
            self._logger.record(
                "tool.registration_conflict",
                {"name": tool.name, "error_code": "duplicate_tool_name"},
            )
            return False
        self._tools[tool.name] = tool
        return True

    def unregister(self, name: str) -> bool:
        if name not in self._tools:
            return False
        self._tools.pop(name, None)
        self._provider_tool_owners.pop(name, None)
        return True

    def state_for(self, name: str, default=None):
        return self._state.get(name, default)

    def attach_state(self, name: str, value, *, persistent: bool = False) -> None:
        self._state[name] = value
        if persistent:
            self._persistent_state_names.add(name)

    def attach_provider(self, provider) -> None:
        self._providers.append(provider)

    def refresh_providers(self) -> None:
        for provider in self._providers:
            provider_id = id(provider)
            discovered = {}
            for tool in provider.get_tools():
                if tool.name in discovered:
                    self._logger.record(
                        "tool.registration_conflict",
                        {"name": tool.name, "error_code": "duplicate_provider_tool_name"},
                    )
                    continue
                discovered[tool.name] = tool

            owned_names = {
                name for name, owner in self._provider_tool_owners.items() if owner == provider_id
            }
            for name in owned_names - discovered.keys():
                self._tools.pop(name, None)
                self._provider_tool_owners.pop(name, None)

            for name, tool in discovered.items():
                owner = self._provider_tool_owners.get(name)
                if owner == provider_id:
                    self._tools[name] = tool
                    continue
                if not self.register(tool):
                    continue
                self._provider_tool_owners[name] = provider_id

    def reset_state(self) -> None:
        for value in self._state.values():
            close = getattr(value, "close", None)
            if callable(close):
                close()
        self._state = {
            name: value
            for name, value in self._state.items()
            if name in self._persistent_state_names
        }

    def definitions(self) -> list[ToolDefinition]:
        self.refresh_providers()
        return [tool.definition() for tool in self._tools.values() if getattr(tool, "exposed", True)]

    def provider_statuses(self) -> list[dict[str, object]]:
        statuses: list[dict[str, object]] = []
        for provider in self._providers:
            get_statuses = getattr(provider, "get_statuses", None)
            if callable(get_statuses):
                statuses.extend(get_statuses())
        return statuses

    def definition_for(self, name: str) -> ToolDefinition | None:
        tool = self._tools.get(name)
        if tool is None:
            return None
        return tool.definition()

    def execute(self, action: ToolAction, *, cwd: str = ".", allowed_roots: list[str] | None = None) -> ToolResult:
        blocked = self.preflight(action, cwd=cwd, allowed_roots=allowed_roots)
        if blocked is not None:
            self._record_result(blocked)
            return blocked
        tool = self._tools[action.name]
        context = self._context(cwd, allowed_roots)
        self._logger.record("tool.execute", {"name": action.name, "arguments": action.arguments})
        result = tool.run(action, context)
        self._record_result(result)
        return result

    def preflight(
        self,
        action: ToolAction,
        *,
        cwd: str = ".",
        allowed_roots: list[str] | None = None,
    ) -> ToolResult | None:
        tool = self._tools.get(action.name)
        if tool is None:
            return ToolResult(
                name=action.name,
                success=False,
                output=f"unknown tool: {action.name}",
                error_code="unknown_tool",
            )

        validation_error = self._validate(action, getattr(tool, "input_schema", {}))
        if validation_error is not None:
            return validation_error

        context = self._context(cwd, allowed_roots)
        definition = tool.definition()
        for interceptor in self._interceptors:
            blocked = interceptor.before_execute(action, definition, context)
            if blocked is not None:
                return blocked
        return None

    def _context(
        self,
        cwd: str,
        allowed_roots: list[str] | None,
    ) -> ToolContext:
        return ToolContext(
            cwd=cwd,
            state=self._state,
            allowed_roots=list(allowed_roots or []),
            max_output_bytes=self._max_output_bytes,
        )

    def summarize_result(self, result: ToolResult) -> str:
        tool = self._tools.get(result.name)
        if tool is None:
            return result.output
        return tool.summarize(result)

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

        property_schemas = schema.get("properties", {})
        if isinstance(property_schemas, dict):
            for name, value in action.arguments.items():
                field_schema = property_schemas.get(name)
                if not isinstance(field_schema, dict):
                    continue
                expected = field_schema.get("type")
                if isinstance(expected, str) and not self._matches_type(value, expected):
                    return ToolResult(
                        name=action.name,
                        success=False,
                        output=(
                            f"argument '{name}' must have type {expected}; "
                            f"received {type(value).__name__}"
                        ),
                        error_code="invalid_argument_type",
                        metadata={
                            "argument": name,
                            "expected": expected,
                            "actual": type(value).__name__,
                        },
                    )
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if not math.isfinite(float(value)):
                        return self._invalid_number(action, name, "must be finite")
                    minimum = field_schema.get("minimum")
                    maximum = field_schema.get("maximum")
                    if isinstance(minimum, (int, float)) and value < minimum:
                        return self._invalid_number(
                            action,
                            name,
                            f"must be at least {minimum}",
                        )
                    if isinstance(maximum, (int, float)) and value > maximum:
                        return self._invalid_number(
                            action,
                            name,
                            f"must be at most {maximum}",
                        )

        return None

    def _matches_type(self, value, expected: str) -> bool:
        checks = {
            "array": lambda item: isinstance(item, list),
            "boolean": lambda item: isinstance(item, bool),
            "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
            "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
            "object": lambda item: isinstance(item, dict),
            "string": lambda item: isinstance(item, str),
        }
        check = checks.get(expected)
        return True if check is None else check(value)

    def _invalid_number(
        self,
        action: ToolAction,
        name: str,
        reason: str,
    ) -> ToolResult:
        return ToolResult(
            name=action.name,
            success=False,
            output=f"argument '{name}' {reason}",
            error_code="invalid_argument_value",
            metadata={"argument": name, "reason": reason},
        )

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
