from __future__ import annotations

from ..types import ExecutionSummary, UserRequest
from .presenter import ConsolePresenter


class CLI:
    """Thin interaction shell that delegates work to the execution engine."""

    def __init__(self, engine, presenter: ConsolePresenter) -> None:
        self.engine = engine
        self.presenter = presenter

    def run(self, request: UserRequest) -> ExecutionSummary:
        self.presenter.show_start(request)
        summary = self.engine.execute(request)
        self.presenter.show_summary(summary)
        return summary
