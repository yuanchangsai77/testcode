from __future__ import annotations

from typing import Any, Protocol

from ..types import ToolAction, ToolResult


class ProgressReporter(Protocol):
    """Optional execution progress sink.

    ExecutionEngine emits these events without depending on a terminal UI.
    Implementations can render a TUI, collect telemetry, or ignore events.
    """

    def model_started(self) -> Any:
        ...

    def model_finished(self, handle: Any) -> None:
        ...

    def tool_started(self, action_name: str) -> Any:
        ...

    def tool_finished(self, handle: Any, action: ToolAction, result: ToolResult) -> None:
        ...

    def tool_aborted(self, handle: Any) -> None:
        ...

    def tool_skipped(self, action: ToolAction, reason: str) -> None:
        ...
