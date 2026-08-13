from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

from ..types import EvidenceRecord, RuntimeBlocker, TaskCheckpoint, ToolDefinition, ToolResult, UserRequest

if TYPE_CHECKING:
    from ..capabilities.model import InstructionContent
    from ..context import ExplicitContextItem, ProjectRule, WorkspaceSummary


@dataclass(slots=True)
class SessionContext:
    request: UserRequest
    available_tools: list[ToolDefinition] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    active_instructions: list[InstructionContent] = field(default_factory=list)
    project_rules: list[ProjectRule] = field(default_factory=list)
    workspace_summary: WorkspaceSummary | None = None
    explicit_context: list[ExplicitContextItem] = field(default_factory=list)
    external_tool_statuses: list[dict[str, object]] = field(default_factory=list)
    checkpoint: TaskCheckpoint = field(default_factory=TaskCheckpoint)


    def add_model_message(self, message: str) -> None:
        self.history.append(f"model: {message}")

    def add_tool_result(self, result: ToolResult) -> None:
        self.tool_results.append(result)
        status = "ok" if result.success else f"error:{result.error_code or 'tool_failed'}"
        arguments = result.metadata.get("action_arguments")
        argument_text = ""
        if arguments:
            visible_arguments = self._bounded_arguments(arguments)
            argument_text = f" args={json.dumps(visible_arguments, ensure_ascii=False, sort_keys=True)}"
        action_artifact_ref = result.metadata.get("action_artifact_ref")
        if isinstance(action_artifact_ref, str) and action_artifact_ref:
            argument_text += f" args_ref={action_artifact_ref}"
        self.history.append(f"tool:{result.name}:{status}{argument_text}: {result.output}")
        artifact_refs = self._record_artifacts(result)
        self._record_evidence(result, artifact_refs)
        if result.success:
            self.checkpoint.completed_actions.append(result.name)
            self.checkpoint.completed_actions = self.checkpoint.completed_actions[-50:]
        else:
            self.checkpoint.blockers.append(
                RuntimeBlocker(
                    error_code=result.error_code or "tool_failed",
                    summary=self._bounded_text(result.output),
                    source="tool",
                    tool=result.name,
                    retryability=str(result.metadata.get("retryability", "conditional")),
                    required_action=str(result.metadata.get("required_action", "change_strategy")),
                )
            )
        cwd = result.metadata.get("cwd")
        if isinstance(cwd, str) and cwd:
            self.checkpoint.runtime_state["shell_cwd"] = cwd

    def _record_artifacts(self, result: ToolResult) -> list[str]:
        values: list[str] = []
        artifact_ref = result.metadata.get("artifact_ref")
        if isinstance(artifact_ref, str) and artifact_ref:
            values.append(artifact_ref)
        artifact_refs = result.metadata.get("artifact_refs")
        if isinstance(artifact_refs, list):
            values.extend(item for item in artifact_refs if isinstance(item, str) and item)
        for value in values:
            if value not in self.checkpoint.artifacts:
                self.checkpoint.artifacts.append(value)
        self.checkpoint.artifacts = self.checkpoint.artifacts[-50:]
        return list(dict.fromkeys(values))

    def _record_evidence(self, result: ToolResult, artifact_refs: list[str]) -> None:
        invalidated = result.metadata.get("invalidates_evidence", [])
        if isinstance(invalidated, list):
            invalidated_kinds = {item for item in invalidated if isinstance(item, str) and item}
            self.checkpoint.evidence = [
                item
                for item in self.checkpoint.evidence
                if not (
                    item.task_id == self.checkpoint.task_id
                    and item.workspace_revision == self.checkpoint.workspace_revision
                    and item.kind in invalidated_kinds
                )
            ]
        raw = result.metadata.get("evidence", [])
        kinds = [item for item in raw if isinstance(item, str) and item] if isinstance(raw, list) else []
        kinds = list(dict.fromkeys(kinds))
        if "workspace_change" in kinds or (
            result.metadata.get("invalidates_workspace_state") is True
            and "workspace_change" not in kinds
        ):
            self.checkpoint.workspace_revision += 1
        if not kinds:
            return
        revision = self.checkpoint.workspace_revision
        raw_sources = result.metadata.get("evidence_sources", {})
        for kind in kinds:
            sources = raw_sources.get(kind, []) if isinstance(raw_sources, dict) else []
            record = EvidenceRecord(
                kind=kind,
                producer=result.name,
                task_id=self.checkpoint.task_id,
                workspace_revision=revision,
                artifact_refs=list(artifact_refs),
                source_task_ids=[
                    item for item in sources if isinstance(item, str) and item
                ] if isinstance(sources, list) else [],
            )
            self.checkpoint.evidence = [
                item
                for item in self.checkpoint.evidence
                if not (
                    item.kind == record.kind
                    and item.producer == record.producer
                    and item.workspace_revision == record.workspace_revision
                )
            ]
            self.checkpoint.evidence.append(record)
        self.checkpoint.evidence = self.checkpoint.evidence[-100:]

    def _bounded_arguments(self, value: object) -> object:
        if isinstance(value, dict):
            return {str(key): self._bounded_arguments(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._bounded_arguments(item) for item in value[:50]]
        if isinstance(value, str) and len(value) > 500:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
            return f"<omitted {len(value)} chars; sha256:{digest}>"
        return value

    def _bounded_text(self, value: str, limit: int = 1_000) -> str:
        if len(value) <= limit:
            return value
        return f"{value[: limit - 3]}..."
