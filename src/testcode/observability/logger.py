from __future__ import annotations

import json
from pathlib import Path

from .events import Event


class InMemoryLogger:
    def __init__(self, base_dir: str | None = None) -> None:
        self.events: list[Event] = []
        self.base_dir = Path(base_dir or ".testcode/runs")
        self.run_dir: Path | None = None

    def record(self, name: str, payload: dict) -> None:
        event = Event(name=name, payload=payload)
        self.events.append(event)
        self._append_event(event)

    def start_run(self, request) -> None:
        if self.run_dir is not None:
            return

        timestamp = Event(name="run.init", payload={}).timestamp
        safe_timestamp = timestamp.replace(":", "-")
        self.run_dir = self.base_dir / safe_timestamp
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.record(
            "run.start",
            {
                "prompt": request.prompt,
                "cwd": request.cwd,
                "metadata": request.metadata,
            },
        )

    def finalize(self, request, summary) -> None:
        if self.run_dir is None:
            self.start_run(request)

        self.record(
            "run.finish",
            {
                "final_message": summary.final_message,
                "tool_results": [
                    {
                        "name": result.name,
                        "success": result.success,
                        "output": result.output,
                        "error_code": result.error_code,
                        "metadata": result.metadata,
                    }
                    for result in summary.tool_results
                ],
            },
        )
        self._write_details_log(request, summary)

    def _append_event(self, event: Event) -> None:
        if self.run_dir is None:
            return

        events_path = self.run_dir / "events.jsonl"
        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": event.timestamp,
                        "name": event.name,
                        "payload": event.payload,
                    },
                    ensure_ascii=False,
                )
            )
            handle.write("\n")

    def _write_details_log(self, request, summary) -> None:
        if self.run_dir is None:
            return

        turns = self._group_turns()
        lines = [
            "Overview",
            f"- prompt: {request.prompt}",
            f"- cwd: {request.cwd}",
            f"- total events: {len(self.events)}",
            f"- turns: {len(turns)}",
            "",
        ]

        if turns:
            lines.append("Turns")
            for turn in turns:
                lines.append(f"- turn {turn['turn']}")
                if turn["request"] is not None:
                    lines.append("  model request:")
                    for line in self._format_payload(turn["request"]).splitlines():
                        lines.append(f"    {line}")
                if turn["raw_response"] is not None:
                    lines.append("  model raw response:")
                    for line in self._format_payload(turn["raw_response"]).splitlines():
                        lines.append(f"    {line}")
                if turn["parsed_reply"] is not None:
                    lines.append("  model parsed reply:")
                    for line in self._format_payload(turn["parsed_reply"]).splitlines():
                        lines.append(f"    {line}")
                for check in turn["safety_checks"]:
                    lines.append("  safety check:")
                    for line in self._format_payload(check).splitlines():
                        lines.append(f"    {line}")
                for action in turn["tool_executes"]:
                    lines.append("  tool execute:")
                    for line in self._format_payload(action).splitlines():
                        lines.append(f"    {line}")
                for result in turn["tool_results"]:
                    lines.append("  tool result:")
                    for line in self._format_payload(result).splitlines():
                        lines.append(f"    {line}")
                if turn["reply"] is not None:
                    lines.append("  turn decision:")
                    for line in self._format_payload(turn["reply"]).splitlines():
                        lines.append(f"    {line}")
                lines.append("")

        lines.append("Timeline")
        for event in self.events:
            lines.append(f"- {event.timestamp} {event.name}")
            payload_text = self._format_payload(event.payload)
            if payload_text:
                for line in payload_text.splitlines():
                    lines.append(f"  {line}")

        lines.extend(
            [
                "",
                "Final",
                f"- message: {summary.final_message}",
            ]
        )

        if summary.tool_results:
            lines.append("- tool results:")
            for result in summary.tool_results:
                lines.append(f"  - {result.name}: {'ok' if result.success else 'blocked'}")
                for line in str(result.output).splitlines():
                    lines.append(f"    {line}")

        details_path = self.run_dir / "details.log"
        details_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _format_payload(self, payload: dict) -> str:
        if not payload:
            return ""

        try:
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except TypeError:
            return repr(payload)

    def _group_turns(self) -> list[dict]:
        turns: list[dict] = []
        current: dict | None = None

        for event in self.events:
            if event.name == "model.request":
                if current is not None:
                    turns.append(current)
                current = {
                    "turn": len(turns) + 1,
                    "request": event.payload,
                    "raw_response": None,
                    "parsed_reply": None,
                    "safety_checks": [],
                    "tool_executes": [],
                    "tool_results": [],
                    "reply": None,
                }
                continue

            if current is None:
                continue

            if event.name == "model.response":
                current["raw_response"] = event.payload
            elif event.name == "model.parsed_reply":
                current["parsed_reply"] = event.payload
            elif event.name == "safety.check":
                current["safety_checks"].append(event.payload)
            elif event.name == "tool.execute":
                current["tool_executes"].append(event.payload)
            elif event.name == "tool.result":
                current["tool_results"].append(event.payload)
            elif event.name == "model.reply":
                current["reply"] = event.payload

        if current is not None:
            turns.append(current)

        return turns
