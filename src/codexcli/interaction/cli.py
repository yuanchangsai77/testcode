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
        return self._run_once(request)

    def chat(self, cwd: str, initial_prompt: str | None = None) -> None:
        conversation: list[dict[str, str]] = []
        prompt = initial_prompt

        while True:
            if prompt is None:
                try:
                    prompt = input("codexcli> ").strip()
                except KeyboardInterrupt:
                    print()
                    return
                except EOFError:
                    print()
                    return

            if not prompt:
                prompt = None
                continue

            if prompt.lower() in {"exit", "quit"}:
                return

            request = UserRequest(
                prompt=prompt,
                cwd=cwd,
                metadata={"conversation": list(conversation)},
            )
            summary = self._run_once(request)
            conversation.append({"role": "user", "content": prompt})
            conversation.append({"role": "assistant", "content": summary.final_message})
            prompt = None

    def _run_once(self, request: UserRequest) -> ExecutionSummary:
        if self.logger is not None:
            self.logger.start_run(request)
        self.presenter.show_start(request)
        summary = self.engine.execute(request)
        self.presenter.show_summary(summary)
        if self.logger is not None:
            self.logger.finalize(request, summary)
        return summary
