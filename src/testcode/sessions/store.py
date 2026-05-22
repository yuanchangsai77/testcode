from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..types import SessionRecord, StoredSession


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
        )
        self.save(session)
        return session

    def save(self, session: StoredSession) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        updated_at = self._timestamp()
        session.updated_at = updated_at
        payload = {
            "session_id": session.session_id,
            "cwd": session.cwd,
            "created_at": session.created_at,
            "updated_at": updated_at,
            "status": session.status,
            "messages": session.messages,
            "run_ids": session.run_ids,
        }
        path = self.base_dir / f"{session.session_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def load(self, session_id: str) -> StoredSession | None:
        if not self._valid_session_id(session_id):
            return None

        path = self.base_dir / f"{session_id}.json"
        if not path.exists():
            return None

        payload = json.loads(path.read_text(encoding="utf-8"))
        messages = self._normalize_messages(payload.get("messages", []))
        return StoredSession(
            session_id=str(payload["session_id"]),
            cwd=str(payload["cwd"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            status=str(payload.get("status", "active")),
            messages=messages,
            run_ids=self._normalize_run_ids(payload.get("run_ids", [])),
        )

    def list_sessions(self) -> list[SessionRecord]:
        if not self.base_dir.exists():
            return []

        sessions: list[SessionRecord] = []
        for path in sorted(self.base_dir.glob("*.json")):
            try:
                session = self.load(path.stem)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
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
