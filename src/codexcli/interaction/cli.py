from __future__ import annotations

from ..types import ExecutionSummary, UserRequest
from .presenter import ConsolePresenter


class CLI:
    """Thin interaction shell that delegates work to the execution engine."""

    def __init__(self, engine, presenter: ConsolePresenter, logger=None) -> None:
        self.engine = engine
        self.presenter = presenter
        self.logger = logger

    def run(self, request: UserRequest) -> ExecutionSummary:
        if self.logger is not None:
            self.logger.start_run(request)
        self.presenter.show_start(request)
        summary = self.engine.execute(request)
        self.presenter.show_summary(summary)
        if self.logger is not None:
            self.logger.finalize(request, summary)
        return summary
