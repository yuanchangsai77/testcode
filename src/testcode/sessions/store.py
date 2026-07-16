from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..types import SessionRecord, SessionResumeState, SessionRunTrace, SessionTurnTrace, StoredSession


class SessionStore:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
        self.base_dir = root / ".testcode" / "sessions"

    def create(self, cwd: str, messages: list[dict[str, str]] | None = None) -> StoredSession:
        now = self._timestamp()
        session = StoredSession(
            session_id=self._build_session_id(now),
            cwd=cwd,
            created_at=now,
            updated_at=now,
            status="active",
            messages=list(messages or []),
            run_ids=[],
            trace=[],
            resume_state=SessionResumeState(),
        )
        self.save(session)
        return session

    def save(self, session: StoredSession) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        updated_at = self._timestamp()
        session.updated_at = updated_at
        session.resume_state = self._build_resume_state(session)
        payload = {
            "session_id": session.session_id,
            "cwd": session.cwd,
            "created_at": session.created_at,
            "updated_at": updated_at,
            "status": session.status,
            "messages": session.messages,
            "run_ids": session.run_ids,
            "active_skills": list(getattr(session, "active_skills", [])),
            "active_capability_ids": list(getattr(session, "active_capability_ids", [])),
            "trace": [self._trace_to_payload(item) for item in getattr(session, "trace", [])],
            "resume_state": self._resume_state_to_payload(session.resume_state),
        }
        path = self.base_dir / f"{session.session_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._write_trace_log(session)
        replay_path = self.base_dir / f"{session.session_id}.replay.log"
        if replay_path.exists():
            replay_path.unlink()

    def load(self, session_id: str) -> StoredSession | None:
        if not self._valid_session_id(session_id):
            return None

        path = self.base_dir / f"{session_id}.json"
        if not path.exists():
            return None

        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        messages = self._normalize_messages(payload.get("messages", []))
        return StoredSession(
            session_id=str(payload["session_id"]),
            cwd=str(payload["cwd"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            status=str(payload.get("status", "active")),
            messages=messages,
            run_ids=self._normalize_run_ids(payload.get("run_ids", [])),
            active_skills=self._string_list(payload.get("active_skills", [])),
            active_capability_ids=self._string_list(payload.get("active_capability_ids", [])),
            trace=self._normalize_trace(payload.get("trace", [])),
            resume_state=self._normalize_resume_state(payload.get("resume_state", {})),
        )

    def list_sessions(self) -> list[SessionRecord]:
        if not self.base_dir.exists():
            return []

        sessions: list[SessionRecord] = []
        for path in sorted(self.base_dir.glob("*.json")):
            try:
                session = self.load(path.stem)
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue

            if session is None:
                continue

            preview = ""
            for message in session.messages:
                if message.get("role") == "user":
                    preview = self._preview(message.get("content", ""))
                    break

            sessions.append(
                SessionRecord(
                    session_id=session.session_id,
                    cwd=session.cwd,
                    created_at=session.created_at,
                    updated_at=session.updated_at,
                    status=session.status,
                    message_count=len(session.messages),
                    preview=preview,
                )
            )

        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return sessions

    def latest(self) -> StoredSession | None:
        sessions = self.list_sessions()
        if not sessions:
            return None
        return self.load(sessions[0].session_id)

    def _normalize_messages(self, messages: object) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        if not isinstance(messages, list):
            return normalized

        for item in messages:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if isinstance(role, str) and isinstance(content, str):
                normalized.append({"role": role, "content": content})
        return normalized

    def _normalize_run_ids(self, run_ids: object) -> list[str]:
        if not isinstance(run_ids, list):
            return []
        return [item for item in run_ids if isinstance(item, str) and item]

    def _normalize_trace(self, trace: object) -> list[SessionRunTrace]:
        if not isinstance(trace, list):
            return []

        normalized: list[SessionRunTrace] = []
        for item in trace:
            if not isinstance(item, dict):
                continue
            turns_payload = item.get("turns", [])
            turns: list[SessionTurnTrace] = []
            if isinstance(turns_payload, list):
                for turn in turns_payload:
                    if not isinstance(turn, dict):
                        continue
                    turns.append(
                        SessionTurnTrace(
                            turn=self._safe_int(turn.get("turn"), 0),
                            message=str(turn.get("message", "")),
                            actions=self._string_list(turn.get("actions")),
                            tool_results=self._string_list(turn.get("tool_results")),
                            action_details=self._string_list(turn.get("action_details")),
                            tool_result_details=self._string_list(turn.get("tool_result_details")),
                        )
                    )
            normalized.append(
                SessionRunTrace(
                    run_id=str(item.get("run_id", "")),
                    started_at=str(item.get("started_at", "")),
                    completed_at=str(item.get("completed_at", "")),
                    prompt=str(item.get("prompt", "")),
                    final_message=str(item.get("final_message", "")),
                    outcome=str(item.get("outcome", "completed")),
                    event_count=self._safe_int(item.get("event_count"), 0),
                    turn_count=self._safe_int(item.get("turn_count"), len(turns)),
                    tool_names=self._string_list(item.get("tool_names")),
                    turns=turns,
                )
            )
        return normalized

    def _normalize_resume_state(self, payload: object) -> SessionResumeState:
        if not isinstance(payload, dict):
            return SessionResumeState()
        return SessionResumeState(
            last_run_id=str(payload.get("last_run_id", "")),
            last_user_prompt=str(payload.get("last_user_prompt", "")),
            last_assistant_message=str(payload.get("last_assistant_message", "")),
            last_outcome=str(payload.get("last_outcome", "")),
            last_tool_names=self._string_list(payload.get("last_tool_names")),
            open_issue=str(payload.get("open_issue", "")),
            recovery_hint=str(payload.get("recovery_hint", "")),
        )

    def _string_list(self, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str) and item]

    def _safe_int(self, value: object, default: int) -> int:
        if isinstance(value, bool):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _trace_to_payload(self, trace: SessionRunTrace) -> dict[str, object]:
        return {
            "run_id": trace.run_id,
            "started_at": trace.started_at,
            "completed_at": trace.completed_at,
            "prompt": trace.prompt,
            "final_message": trace.final_message,
            "outcome": trace.outcome,
            "event_count": trace.event_count,
            "turn_count": trace.turn_count,
            "tool_names": list(trace.tool_names),
            "turns": [
                {
                    "turn": turn.turn,
                    "message": turn.message,
                    "actions": list(turn.actions),
                    "tool_results": list(turn.tool_results),
                    "action_details": list(turn.action_details),
                    "tool_result_details": list(turn.tool_result_details),
                }
                for turn in trace.turns
            ],
        }

    def _resume_state_to_payload(self, state: SessionResumeState) -> dict[str, object]:
        return {
            "last_run_id": state.last_run_id,
            "last_user_prompt": state.last_user_prompt,
            "last_assistant_message": state.last_assistant_message,
            "last_outcome": state.last_outcome,
            "last_tool_names": list(state.last_tool_names),
            "open_issue": state.open_issue,
            "recovery_hint": state.recovery_hint,
        }

    def _write_trace_log(self, session: StoredSession) -> None:
        trace_path = self.base_dir / f"{session.session_id}.trace.log"
        lines = [
            "Session Trace Summary",
            f"- session_id: {session.session_id}",
            f"- cwd: {session.cwd}",
            f"- status: {session.status}",
            f"- messages: {len(session.messages)}",
            f"- runs: {len(session.trace)}",
            "",
        ]
        state = session.resume_state
        if state.last_run_id or state.open_issue or state.recovery_hint:
            lines.extend(
                [
                    "Resume State",
                    f"- last_run_id: {state.last_run_id or '-'}",
                    f"- last_outcome: {state.last_outcome or '-'}",
                    (
                        f"- last_tools: {', '.join(state.last_tool_names)}"
                        if state.last_tool_names
                        else "- last_tools: -"
                    ),
                    f"- open_issue: {state.open_issue or '-'}",
                    f"- recovery_hint: {state.recovery_hint or '-'}",
                    "",
                ]
            )
        for trace in session.trace:
            lines.append(f"Run {trace.run_id}")
            lines.append(f"- prompt: {trace.prompt}")
            lines.append(f"- outcome: {trace.outcome}")
            lines.append(f"- turns: {trace.turn_count}")
            lines.append(f"- events: {trace.event_count}")
            if trace.tool_names:
                lines.append(f"- tools: {', '.join(trace.tool_names)}")
            lines.append(f"- final: {trace.final_message}")
            lines.append("")
        trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _build_resume_state(self, session: StoredSession) -> SessionResumeState:
        trace = session.trace[-1] if session.trace else None
        last_user_prompt = ""
        last_assistant_message = ""
        for item in reversed(session.messages):
            if not last_assistant_message and item.get("role") == "assistant":
                last_assistant_message = item.get("content", "")
            elif not last_user_prompt and item.get("role") == "user":
                last_user_prompt = item.get("content", "")
            if last_user_prompt and last_assistant_message:
                break

        if trace is None:
            return SessionResumeState(
                last_user_prompt=last_user_prompt,
                last_assistant_message=last_assistant_message,
            )

        open_issue = ""
        recovery_hint = ""
        if trace.outcome != "completed":
            open_issue = trace.final_message
        elif "Model API is unavailable right now." in trace.final_message:
            open_issue = trace.final_message
        elif any("approval_denied" in result for turn in trace.turns for result in turn.tool_results):
            open_issue = "A tool action was declined by the user."
        elif any("approval_required" in result for turn in trace.turns for result in turn.tool_results):
            open_issue = "A tool was blocked waiting for approval."
        elif any("duplicate_tool_call" in result for turn in trace.turns for result in turn.tool_results):
            open_issue = "The last run repeated an earlier tool call instead of progressing."

        if open_issue:
            recovery_hint = (
                "Reuse the last successful result when possible, avoid repeating identical reads, "
                "and resolve the blocking issue before exploring more context."
            )
        elif trace.tool_names:
            recovery_hint = "Continue from the latest completed state and only call tools needed for the next step."

        return SessionResumeState(
            last_run_id=trace.run_id,
            last_user_prompt=trace.prompt or last_user_prompt,
            last_assistant_message=trace.final_message or last_assistant_message,
            last_outcome=trace.outcome,
            last_tool_names=list(trace.tool_names),
            open_issue=open_issue,
            recovery_hint=recovery_hint,
        )

    def _valid_session_id(self, session_id: str) -> bool:
        if not session_id or session_id in {".", ".."}:
            return False
        return Path(session_id).name == session_id

    def _preview(self, text: str, limit: int = 60) -> str:
        single_line = " ".join(text.split())
        if len(single_line) <= limit:
            return single_line
        return f"{single_line[: limit - 3]}..."

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _build_session_id(self, timestamp: str) -> str:
        compact = (
            timestamp.replace("-", "")
            .replace(":", "")
            .replace("T", "")
            .replace("Z", "")
            .replace(".", "")
        )
        return f"{compact}-{uuid4().hex[:8]}"
