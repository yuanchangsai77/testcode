from __future__ import annotations

import json
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

from ..types import ToolDefinition, ToolResult, UserRequest

if TYPE_CHECKING:
    from ..context import ExplicitContextItem, ProjectRule, WorkspaceSummary
    from ..skills.model import Skill


@dataclass(slots=True)
class SessionContext:
    request: UserRequest
    available_tools: list[ToolDefinition] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    active_skills: list[Skill] = field(default_factory=list)
    project_rules: list[ProjectRule] = field(default_factory=list)
    workspace_summary: WorkspaceSummary | None = None
    explicit_context: list[ExplicitContextItem] = field(default_factory=list)


    def add_model_message(self, message: str) -> None:
        self.history.append(f"model: {message}")

    def add_tool_result(self, result: ToolResult) -> None:
        self.tool_results.append(result)
        status = "ok" if result.success else f"error:{result.error_code or 'tool_failed'}"
        arguments = result.metadata.get("action_arguments")
        argument_text = ""
        if arguments:
            argument_text = f" args={json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
        self.history.append(f"tool:{result.name}:{status}{argument_text}: {result.output}")
