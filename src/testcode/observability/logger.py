from __future__ import annotations

import json
import hashlib
from pathlib import Path

from ..safety.redaction import redact, redact_text
from ..types import EvidenceRecord, RuntimeBlocker, SessionRunTrace, SessionTurnTrace, TaskCheckpoint
from .events import Event


class InMemoryLogger:
    def __init__(self, base_dir: str | None = None) -> None:
        self.events: list[Event] = []
        self.base_dir = Path(base_dir or ".testcode/runs")
        self.run_dir: Path | None = None
        self.run_id: str | None = None
        self.last_run_id: str | None = None
        self.last_run_summary: SessionRunTrace | None = None
        self._artifact_count = 0
        self._large_value_artifacts: dict[str, str] = {}

    def _compact_text(self, text: str, limit: int = 240) -> str:
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return f"{compact[: limit - 3]}..."

    def record(self, name: str, payload: dict) -> None:
        event = Event(name=name, payload=redact(self._externalize_large_values(payload, name)))
        self.events.append(event)
        self._append_event(event)

    def _externalize_large_values(self, value: object, event_name: str) -> object:
        if isinstance(value, dict):
            return {
                str(key): self._externalize_large_values(item, event_name)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._externalize_large_values(item, event_name) for item in value]
        if isinstance(value, str) and len(value) > 16_000 and self.run_dir is not None:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            artifact_ref = self._large_value_artifacts.get(digest)
            if artifact_ref is None:
                artifact_ref = self.write_artifact(
                    f"{event_name}-large-value",
                    {"content": value},
                )
                if artifact_ref:
                    self._large_value_artifacts[digest] = artifact_ref
            if artifact_ref:
                return {
                    "$type": "artifact_ref",
                    "schema_version": 1,
                    "artifact_ref": artifact_ref,
                    "chars": len(value),
                    "sha256": digest,
                }
        return value

    def start_run(self, request, registered_skills: list[str] | None = None) -> None:
        if self.run_dir is not None:
            return

        self.events = []
        self.last_run_summary = None
        self._artifact_count = 0
        self._large_value_artifacts = {}
        timestamp = Event(name="run.init", payload={}).timestamp
        safe_timestamp = timestamp.replace(":", "-")
        self.run_id = safe_timestamp
        self.last_run_id = safe_timestamp
        self.run_dir = self.base_dir / safe_timestamp
        self.run_dir.mkdir(parents=True, exist_ok=True)
        metadata = dict(request.metadata)
        # Session traces are persisted by SessionStore. Logging them again on
        # every run would copy the full history into each subsequent run log.
        metadata.pop("session_trace", None)
        self.record(
            "run.start",
            {
                "run_id": safe_timestamp,
                "prompt": redact_text(request.prompt),
                "cwd": request.cwd,
                "metadata": redact(metadata),
                "registered_skills": registered_skills or [],
            },
        )

    def write_artifact(self, kind: str, payload: object) -> str | None:
        if self.run_dir is None:
            return None
        self._artifact_count += 1
        safe_kind = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in kind)
        artifact_dir = self.run_dir / "artifacts"
        path = artifact_dir / f"{self._artifact_count:04d}-{safe_kind[:80] or 'artifact'}.json"
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(redact(payload), ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        except (OSError, RecursionError, TypeError, ValueError):
            return None
        return str(path)


    def finalize(self, request, summary) -> None:
        if self.run_dir is None:
            self.start_run(request)

        self.record(
            "run.finish",
            {
                "run_id": self.run_id,
                "final_message": redact_text(summary.final_message),
                "tool_results": [
                    {
                        "name": result.name,
                        "success": result.success,
                        "output": redact_text(result.output),
                        "error_code": result.error_code,
                        "metadata": redact(result.metadata),
                    }
                    for result in summary.tool_results
                ],
                "outcome": getattr(summary, "outcome", "completed"),
                "blockers": [self._blocker_payload(item) for item in getattr(summary, "blockers", [])],
                "checkpoint": self._checkpoint_payload(getattr(summary, "checkpoint", None)),
            },
        )
        self.last_run_summary = self._build_run_summary(request, summary)
        self._write_details_log(request, summary)
        self.run_dir = None
        self.run_id = None

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
                    default=repr,
                )
            )
            handle.write("\n")

    def _write_details_log(self, request, summary) -> None:
        if self.run_dir is None:
            return

        turns = self._group_turns()
        active_instructions = getattr(summary, "active_instructions", [])

        lines = [
            "Overview",
            f"- run_id: {self.run_id}",
            f"- prompt: {redact_text(request.prompt)}",
        ]

        if active_instructions:
            instruction_text = ", ".join(
                f"{item.name} (v{item.version})" if item.version else item.name
                for item in active_instructions
            )
            lines.append(f"- active workflow instructions: {instruction_text}")

        lines.extend([
            f"- cwd: {request.cwd}",
            f"- total events: {len(self.events)}",
            f"- turns: {len(turns)}",
            "",
        ])


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
                f"- message: {redact_text(summary.final_message)}",
            ]
        )

        if summary.tool_results:
            lines.append("- tool results:")
            for result in summary.tool_results:
                lines.append(f"  - {result.name}: {'ok' if result.success else 'blocked'}")
                for line in redact_text(str(result.output)).splitlines():
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
                if current is not None and current["reply"] is not None:
                    turns.append(current)
                    current = None
                if current is not None:
                    current["request"] = event.payload
                    current["attempt_count"] += 1
                    continue
                current = {
                    "turn": len(turns) + 1,
                    "request": event.payload,
                    "attempt_count": 1,
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

    def _build_run_summary(self, request, summary) -> SessionRunTrace:
        turns = self._group_turns()
        started_at = self.events[0].timestamp if self.events else ""
        completed_at = self.events[-1].timestamp if self.events else started_at
        tool_names = list(dict.fromkeys(result.name for result in summary.tool_results))
        turn_summaries: list[SessionTurnTrace] = []
        for turn in turns:
            message = ""
            parsed_reply = turn.get("parsed_reply") or {}
            if isinstance(parsed_reply.get("message"), str):
                message = redact_text(parsed_reply["message"])
            elif isinstance(turn.get("reply"), dict) and isinstance(turn["reply"].get("message"), str):
                message = redact_text(turn["reply"]["message"])
            tool_results = []
            action_details = []
            for result in turn.get("tool_results", []):
                name = result.get("name", "tool")
                status = "ok" if result.get("success") else result.get("error_code") or "failed"
                tool_results.append(f"{name}:{status}")
            requested_actions = parsed_reply.get("actions")
            if not isinstance(requested_actions, list):
                requested_actions = turn.get("tool_executes", [])
            for action in requested_actions:
                if not isinstance(action, dict):
                    continue
                name = action.get("name", "tool")
                arguments = action.get("arguments", {})
                try:
                    args_text = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
                except TypeError:
                    args_text = repr(arguments)
                action_details.append(f"{name} args={self._compact_text(args_text, 180)}")
            tool_result_details = []
            for result in turn.get("tool_results", []):
                name = result.get("name", "tool")
                status = "ok" if result.get("success") else result.get("error_code") or "failed"
                output = self._compact_text(str(result.get("output", "")), 320)
                tool_result_details.append(f"{name} [{status}] {output}")
            turn_summaries.append(
                SessionTurnTrace(
                    turn=turn["turn"],
                    message=message,
                    actions=[
                        action.get("name", "")
                        for action in requested_actions
                        if isinstance(action, dict)
                        and isinstance(action.get("name"), str)
                        and action.get("name")
                    ],
                    tool_results=tool_results,
                    action_details=action_details,
                    tool_result_details=tool_result_details,
                )
            )

        outcome = getattr(summary, "outcome", "completed")
        if any(event.name == "run.error" for event in self.events):
            outcome = "runtime_error"
        elif "Interrupted" == summary.final_message:
            outcome = "interrupted"

        return SessionRunTrace(
            run_id=self.run_id or "",
            started_at=started_at,
            completed_at=completed_at,
            prompt=redact_text(request.prompt),
            final_message=redact_text(summary.final_message),
            outcome=outcome,
            event_count=len(self.events),
            turn_count=len(turns),
            tool_names=tool_names,
            turns=turn_summaries,
            blockers=self._safe_blockers(getattr(summary, "blockers", [])),
            checkpoint=self._safe_checkpoint(getattr(summary, "checkpoint", None)),
        )

    def _safe_blockers(self, blockers) -> list[RuntimeBlocker]:
        return [
            RuntimeBlocker(
                error_code=str(getattr(item, "error_code", "")),
                summary=redact_text(str(getattr(item, "summary", ""))),
                source=str(getattr(item, "source", "runtime")),
                tool=str(getattr(item, "tool", "")),
                retryability=str(getattr(item, "retryability", "conditional")),
                required_action=str(getattr(item, "required_action", "resume")),
            )
            for item in blockers
        ]

    def _safe_checkpoint(self, checkpoint) -> TaskCheckpoint:
        if checkpoint is None:
            return TaskCheckpoint()
        return TaskCheckpoint(
            objective=redact_text(str(getattr(checkpoint, "objective", ""))),
            schema_version=int(getattr(checkpoint, "schema_version", 2)),
            task_id=str(getattr(checkpoint, "task_id", "")),
            workspace_root=str(getattr(checkpoint, "workspace_root", "")),
            workspace_revision=int(getattr(checkpoint, "workspace_revision", 0)),
            phase=str(getattr(checkpoint, "phase", "executing")),
            completed_actions=[str(item) for item in getattr(checkpoint, "completed_actions", [])],
            artifacts=[str(item) for item in getattr(checkpoint, "artifacts", [])],
            evidence=[
                EvidenceRecord(
                    kind=str(getattr(item, "kind", "")),
                    producer=str(getattr(item, "producer", "")),
                    task_id=str(getattr(item, "task_id", "")),
                    workspace_revision=int(getattr(item, "workspace_revision", 0)),
                    artifact_refs=[str(ref) for ref in getattr(item, "artifact_refs", [])],
                    source_task_ids=[str(value) for value in getattr(item, "source_task_ids", [])],
                )
                for item in getattr(checkpoint, "evidence", [])
                if getattr(item, "kind", "")
            ],
            required_evidence=[str(item) for item in getattr(checkpoint, "required_evidence", [])],
            unmet_deliverables=[str(item) for item in getattr(checkpoint, "unmet_deliverables", [])],
            blockers=self._safe_blockers(getattr(checkpoint, "blockers", [])),
            runtime_state={
                str(key): str(value)
                for key, value in dict(getattr(checkpoint, "runtime_state", {})).items()
            },
        )

    def _blocker_payload(self, blocker) -> dict[str, object]:
        return {
            "error_code": getattr(blocker, "error_code", ""),
            "summary": redact_text(getattr(blocker, "summary", "")),
            "source": getattr(blocker, "source", "runtime"),
            "tool": getattr(blocker, "tool", ""),
            "retryability": getattr(blocker, "retryability", "conditional"),
            "required_action": getattr(blocker, "required_action", "resume"),
        }

    def _checkpoint_payload(self, checkpoint) -> dict[str, object]:
        if checkpoint is None:
            return {}
        return {
            "objective": redact_text(getattr(checkpoint, "objective", "")),
            "schema_version": getattr(checkpoint, "schema_version", 2),
            "task_id": getattr(checkpoint, "task_id", ""),
            "workspace_root": getattr(checkpoint, "workspace_root", ""),
            "workspace_revision": getattr(checkpoint, "workspace_revision", 0),
            "phase": getattr(checkpoint, "phase", ""),
            "completed_actions": list(getattr(checkpoint, "completed_actions", [])),
            "artifacts": list(getattr(checkpoint, "artifacts", [])),
            "evidence": [
                {
                    "kind": getattr(item, "kind", ""),
                    "producer": getattr(item, "producer", ""),
                    "task_id": getattr(item, "task_id", ""),
                    "workspace_revision": getattr(item, "workspace_revision", 0),
                    "artifact_refs": list(getattr(item, "artifact_refs", [])),
                    "source_task_ids": list(getattr(item, "source_task_ids", [])),
                }
                for item in getattr(checkpoint, "evidence", [])
            ],
            "required_evidence": list(getattr(checkpoint, "required_evidence", [])),
            "unmet_deliverables": list(getattr(checkpoint, "unmet_deliverables", [])),
            "blockers": [self._blocker_payload(item) for item in getattr(checkpoint, "blockers", [])],
            "runtime_state": dict(getattr(checkpoint, "runtime_state", {})),
        }
