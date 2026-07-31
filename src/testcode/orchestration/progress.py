from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..intent import RequestIntent
from ..types import ToolAction, ToolResult


class ProgressReporter(Protocol):
    """Optional execution progress sink.

    ExecutionEngine emits these events without depending on a terminal UI.
    Implementations can render a TUI, collect telemetry, or ignore events.
    """

    def model_started(self, message: str = "Model is thinking…") -> Any:
        ...


    def model_finished(self, handle: Any) -> None:
        ...

    def model_retrying(
        self,
        handle: Any,
        retry: int,
        max_retries: int,
        status: str,
        delay_seconds: float,
    ) -> None:
        ...

    def tool_started(self, action_name: str) -> Any:
        ...

    def tool_finished(self, handle: Any, action: ToolAction, result: ToolResult) -> None:
        ...

    def tool_aborted(self, handle: Any) -> None:
        ...

    def tool_skipped(self, action: ToolAction, reason: str) -> None:
        ...


@dataclass(frozen=True, slots=True)
class ProgressContext:
    intent: RequestIntent
    results: list[ToolResult]
    recovery_sent: bool


@dataclass(frozen=True, slots=True)
class ProgressSignal:
    repeated_actions: list[dict[str, object]]


class ProgressPolicy(Protocol):
    def evaluate(self, context: ProgressContext) -> ProgressSignal | None:
        ...


class DefaultProgressPolicy:
    read_context_tools = {
        "file_info",
        "find_files",
        "git_diff",
        "git_show",
        "git_status",
        "list_dir",
        "read_file",
        "search_text",
    }

    def evaluate(self, context: ProgressContext) -> ProgressSignal | None:
        if context.recovery_sent or not context.intent.file_changes:
            return None
        repeated = [
            result.metadata.get("action_arguments", {"tool": result.name})
            for result in context.results
            if result.name in self.read_context_tools
            and result.metadata.get("duplicate") is True
            and int(result.metadata.get("duplicate_count", 0)) >= 1
        ]
        if not repeated:
            return None
        return ProgressSignal(repeated_actions=repeated)
