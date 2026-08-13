from __future__ import annotations

import time
from dataclasses import dataclass

from ..types import EvidenceRecord, RuntimeBlocker, TaskCheckpoint, ToolResult, UserRequest
from .session import SessionContext


CUMULATIVE_EVIDENCE_KINDS = frozenset({"workspace_change", "artifact"})


def valid_evidence_records(checkpoint: TaskCheckpoint) -> list[EvidenceRecord]:
    """Return task evidence that remains valid at the current workspace revision."""
    return [
        record
        for record in checkpoint.evidence
        if record.task_id == checkpoint.task_id
        and (
            record.kind in CUMULATIVE_EVIDENCE_KINDS
            or record.workspace_revision == checkpoint.workspace_revision
        )
    ]


def valid_evidence_kinds(checkpoint: TaskCheckpoint) -> set[str]:
    return {record.kind for record in valid_evidence_records(checkpoint)}


@dataclass(frozen=True, slots=True)
class RunBudgetPolicy:
    max_model_attempts: int = 120
    max_consecutive_model_timeouts: int = 8
    max_run_seconds: float = 900.0

    def problem(
        self,
        run_started: float,
        model_attempts: int,
        consecutive_model_timeouts: int,
    ) -> RuntimeBlocker | None:
        elapsed = time.monotonic() - run_started
        if elapsed >= self.max_run_seconds:
            return RuntimeBlocker(
                error_code="run_time_budget_exhausted",
                summary=f"Run stopped after reaching its {self.max_run_seconds:g}-second wall-clock budget.",
                source="runtime",
                retryability="conditional",
                required_action="resume",
            )
        if model_attempts >= self.max_model_attempts:
            return RuntimeBlocker(
                error_code="model_attempt_budget_exhausted",
                summary=f"Run stopped after {model_attempts} model attempts without completion.",
                source="runtime",
                retryability="conditional",
                required_action="resume",
            )
        if consecutive_model_timeouts >= self.max_consecutive_model_timeouts:
            return RuntimeBlocker(
                error_code="model_timeout_circuit_open",
                summary=f"Run stopped after {consecutive_model_timeouts} consecutive model timeouts.",
                source="runtime",
                retryability="retryable",
                required_action="resume",
            )
        return None

    @staticmethod
    def is_timeout(error: BaseException) -> bool:
        return isinstance(error, TimeoutError) or type(error).__name__ == "ModelTimeoutError"


class CompletionPolicy:
    invalid_placeholders = frozenset(
        {"dictionary", "dict", "object", "array", "string", "null", "undefined", "none"}
    )
    no_change_phrases = frozenset(
        {
            "no change is needed",
            "no changes are needed",
            "already exists",
            "already satisfied",
            "无需修改",
            "不需要修改",
            "已经满足",
            "已存在",
        }
    )

    def required_evidence(self, request: UserRequest, request_intent) -> list[str]:
        required: list[str] = []
        if request_intent.file_changes:
            required.append("workspace_change")
        explicit = request.metadata.get("completion_requirements", [])
        if isinstance(explicit, list):
            required.extend(item for item in explicit if isinstance(item, str) and item)
        delegated = request.metadata.get("delegated_task")
        if isinstance(delegated, dict):
            delegated_required = delegated.get("required_evidence", [])
            if isinstance(delegated_required, list):
                required.extend(
                    "workspace_change" if item == "write" else item
                    for item in delegated_required
                    if isinstance(item, str) and item
                )
        return list(dict.fromkeys(required))

    def unmet_evidence(self, session: SessionContext) -> list[str]:
        checkpoint = session.checkpoint
        valid_kinds = valid_evidence_kinds(checkpoint)
        evidence = {
            "workspace_change": "workspace_change" in valid_kinds,
            "test": "test" in valid_kinds,
            "artifact": "artifact" in valid_kinds,
            "read": "read" in valid_kinds,
            "response": True,
        }
        return [
            item
            for item in session.checkpoint.required_evidence
            if not evidence.get(item, False)
        ]

    def completion_problem(
        self,
        message: str,
        session: SessionContext | None,
        unresolved: list[ToolResult],
    ) -> str:
        normalized = " ".join(message.split()).strip().lower()
        if normalized in self.invalid_placeholders:
            return (
                "Model completion was rejected because it contained only a protocol placeholder. "
                "Provide a meaningful final answer grounded in the runtime checkpoint."
            )
        if session is None or not session.checkpoint.unmet_deliverables:
            return ""
        if unresolved:
            return ""
        unmet = list(session.checkpoint.unmet_deliverables)
        if (
            unmet == ["workspace_change"]
            and self.explains_no_change(message)
            and self._has_current_evidence(session, "read")
        ):
            session.checkpoint.unmet_deliverables = []
            return ""
        return (
            "Model completion was rejected because runtime evidence is still missing: "
            f"{', '.join(unmet)}. Complete the deliverable or explain why no change is required."
        )

    def explains_no_change(self, message: str) -> bool:
        normalized = " ".join(message.casefold().split())
        return any(phrase in normalized for phrase in self.no_change_phrases)

    @staticmethod
    def _has_current_evidence(session: SessionContext, kind: str) -> bool:
        checkpoint = session.checkpoint
        return any(
            record.kind == kind
            and record.task_id == checkpoint.task_id
            and record.workspace_revision == checkpoint.workspace_revision
            for record in checkpoint.evidence
        )
