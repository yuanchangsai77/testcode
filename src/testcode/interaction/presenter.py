from __future__ import annotations

import json

from ..types import ExecutionSummary, SessionRecord, StoredSession, UserRequest


class ConsolePresenter:
    max_tool_output = 120

    def show_start(self, request: UserRequest) -> None:
        print(f"[testcode] task: {request.prompt}")
        print(f"[testcode] cwd: {request.cwd}")

    def show_summary(self, summary: ExecutionSummary) -> None:
        print("[testcode] result:")
        print(summary.final_message)
        if summary.tool_results:
            print("[testcode] tool results:")
            for result in summary.tool_results:
                status = "ok" if result.success else "blocked"
                output = self._summarize_tool_output(result.output)
                print(f"- {result.name}: {status} -> {output}")

    def confirm_tool_action(self, action, reason: str) -> bool:
        print(f"[testcode] approval required: {action.name}")
        print(f"[testcode] reason: {reason}")
        if action.arguments:
            arguments = json.dumps(action.arguments, ensure_ascii=False, sort_keys=True)
            print(f"[testcode] arguments: {self._summarize_tool_output(arguments)}")
        answer = input("Allow this tool call? [y/N] ").strip().lower()
        return answer in {"y", "yes"}

    def _summarize_tool_output(self, output: str) -> str:
        single_line = " ".join(str(output).split())
        if len(single_line) <= self.max_tool_output:
            return single_line
        return f"{single_line[: self.max_tool_output - 3]}..."

    def show_session_state(self, session: StoredSession, resumed: bool) -> None:
        action = "resumed" if resumed else "started"
        print(f"[testcode] session {action}: {session.session_id}")
        print(f"[testcode] session cwd: {session.cwd}")

    def show_session_list(self, sessions: list[SessionRecord]) -> None:
        if not sessions:
            print("[testcode] no saved sessions")
            return

        print("[testcode] saved sessions:")
        for index, session in enumerate(sessions, start=1):
            preview = session.preview or "(no user messages yet)"
            print(
                f"{index}. {session.session_id} | {session.status} | "
                f"{session.updated_at} | {session.message_count} messages"
            )
            print(f"  cwd: {session.cwd}")
            print(f"  preview: {preview}")
