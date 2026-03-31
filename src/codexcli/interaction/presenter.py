from __future__ import annotations

from ..types import ExecutionSummary, SessionRecord, StoredSession, UserRequest


class ConsolePresenter:
    max_tool_output = 120

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
                output = self._summarize_tool_output(result.output)
                print(f"- {result.name}: {status} -> {output}")

    def _summarize_tool_output(self, output: str) -> str:
        single_line = " ".join(str(output).split())
        if len(single_line) <= self.max_tool_output:
            return single_line
        return f"{single_line[: self.max_tool_output - 3]}..."

    def show_session_state(self, session: StoredSession, resumed: bool) -> None:
        action = "resumed" if resumed else "started"
        print(f"[codexcli] session {action}: {session.session_id}")
        print(f"[codexcli] session cwd: {session.cwd}")

    def show_session_list(self, sessions: list[SessionRecord]) -> None:
        if not sessions:
            print("[codexcli] no saved sessions")
            return

        print("[codexcli] saved sessions:")
        for index, session in enumerate(sessions, start=1):
            preview = session.preview or "(no user messages yet)"
            print(
                f"{index}. {session.session_id} | {session.status} | "
                f"{session.updated_at} | {session.message_count} messages"
            )
            print(f"  cwd: {session.cwd}")
            print(f"  preview: {preview}")
