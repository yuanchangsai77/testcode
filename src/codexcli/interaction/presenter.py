from __future__ import annotations

from ..types import ExecutionSummary, UserRequest


class ConsolePresenter:
    def show_start(self, request: UserRequest) -> None:
        print(f"[codexcli] task: {request.prompt}")
        print(f"[codexcli] cwd: {request.cwd}")

    def show_summary(self, summary: ExecutionSummary) -> None:
        print("[codexcli] result:")
        print(summary.final_message)
        if summary.tool_results:
            print("[codexcli] tool results:")
            for result in summary.tool_results:
                status = "ok" if result.success else "blocked"
                print(f"- {result.name}: {status} -> {result.output}")
