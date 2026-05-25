from __future__ import annotations

import json
import re
from html import unescape

from ..types import ExecutionSummary, SessionRecord, StoredSession, ToolResult, UserRequest


class ConsolePresenter:
    max_tool_output = 120

    def __init__(self, tool_result_summarizer=None) -> None:
        self.tool_result_summarizer = tool_result_summarizer

    def show_start(self, request: UserRequest) -> None:
        print(f"[testcode] task: {request.prompt}")
        print(f"[testcode] cwd: {request.cwd}")

    def show_summary(self, summary: ExecutionSummary) -> None:
        thinking = self._extract_thinking(summary.final_message)
        if thinking:
            print("[testcode] thinking:")
            print(thinking)
        print("[testcode] result:")
        print(self._display_text(summary.final_message))
        if summary.tool_results:
            print("[testcode] tool results:")
            for result in summary.tool_results:
                status = "ok" if result.success else "blocked"
                output = self._summarize_tool_result(result)
                print(f"- {result.name}: {status} -> {output}")

    def _summarize_tool_result(self, result: ToolResult) -> str:
        if self.tool_result_summarizer is not None:
            summary = self.tool_result_summarizer(result)
            if isinstance(summary, str) and summary.strip() and summary != result.output:
                text = self._summarize_tool_output(summary)
                return f"{result.error_code}: {text}" if result.error_code else text

        if not result.success:
            if result.error_code:
                return f"{result.error_code}: {self._summarize_tool_output(result.output)}"
            return self._summarize_tool_output(result.output)

        return self._summarize_tool_output(result.output)

    def confirm_tool_action(self, action, reason: str) -> bool:
        print(f"[testcode] approval required: {action.name}")
        print(f"[testcode] reason: {reason}")
        if action.arguments:
            arguments = json.dumps(action.arguments, ensure_ascii=False, sort_keys=True)
            print(f"[testcode] arguments: {self._summarize_tool_output(arguments)}")
        if action.name == "patch" and isinstance(action.arguments.get("diff"), str):
            print("[testcode] patch preview:")
            print(action.arguments["diff"])
        answer = input("Allow this tool call? [y/N] ").strip().lower()
        return answer in {"y", "yes"}

    def _summarize_tool_output(self, output: str) -> str:
        single_line = " ".join(str(output).split())
        if len(single_line) <= self.max_tool_output:
            return single_line
        return f"{single_line[: self.max_tool_output - 3]}..."

    def _display_text(self, value: str) -> str:
        without_think = re.sub(r"<think\b[^>]*>.*?</think>", "", str(value), flags=re.DOTALL | re.IGNORECASE)
        without_parameters = re.sub(
            r"<parameter\s+name=\"[^\"]+\"\s*>.*?</parameter>",
            "",
            without_think,
            flags=re.DOTALL | re.IGNORECASE,
        )
        without_tags = re.sub(r"</?[\w:.-]+[^>]*>", "", without_parameters)
        without_tool_attrs = re.sub(r'-?\s*tool="[^"]+"\s*>?', "", without_tags)
        return " ".join(unescape(without_tool_attrs).split())

    def _extract_thinking(self, value: str) -> str:
        parts = [
            unescape(match.group(1)).strip()
            for match in re.finditer(r"<think\b[^>]*>(.*?)</think>", str(value), flags=re.DOTALL | re.IGNORECASE)
            if match.group(1).strip()
        ]
        return "\n".join(parts)

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
