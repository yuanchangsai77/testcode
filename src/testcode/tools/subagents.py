from __future__ import annotations

import json

from ..orchestration.subagents import SubagentLaunchSpec
from ..types import ToolAction, ToolResult
from .base import SimpleTool, ToolContext


def build_subagent_tools() -> list[SimpleTool]:
    return [_spawn_tool(), _resume_tool(), _run_tool(), _status_tool()]


def _spawn_tool() -> SimpleTool:
    def run(action: ToolAction, context: ToolContext) -> ToolResult:
        coordinator, _, parent, error = _runtime_state(context)
        if error is not None:
            return error("subagent_spawn")
        warehouse = context.state.get("capability_warehouse")
        active_capability_ids = []
        if warehouse is not None:
            active_capability_ids = [
                item for item in warehouse.active_ids()
                if not item.startswith("local:subagents:")
            ]
        requested_capabilities = action.arguments.get("active_capability_ids", [])
        if isinstance(requested_capabilities, list):
            active_capability_ids.extend(
                item for item in requested_capabilities if isinstance(item, str) and item
            )
        try:
            child = coordinator.launch_subagent(
                parent,
                SubagentLaunchSpec(
                    source=str(action.arguments.get("source", "inherit")),
                    task_summary=str(action.arguments["task"]),
                    cwd=str(action.arguments.get("cwd", "")),
                    image_id=str(action.arguments.get("image_id", "")),
                    active_capability_ids=list(dict.fromkeys(active_capability_ids)),
                    allowed_effects=list(action.arguments.get("allowed_effects", [])),
                    allowed_resources=list(action.arguments.get("allowed_resources", ["."])),
                    required_evidence=list(action.arguments.get("required_evidence", [])),
                    approval_policy=str(action.arguments.get("approval_policy", "block")),
                ),
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            return ToolResult("subagent_spawn", False, str(exc), "subagent_spawn_failed")
        return ToolResult(
            "subagent_spawn",
            True,
            json.dumps(
                {
                    "session_id": child.session_id,
                    "cluster_id": child.cluster_id,
                    "state": "ready",
                    "source": child.launch_source,
                },
                ensure_ascii=False,
            ),
        )

    return SimpleTool(
        name="subagent_spawn",
        description=(
            "Create a new independent child session for a delegated task. Do not use this for feedback "
            "or repairs owned by an existing child; continue that session with subagent_resume instead. "
            "Spawn multiple independent children first, then call subagent_run_ready once."
        ),
        arguments={
            "task": "Bounded task assigned to the child session.",
            "source": "inherit, fresh, or image (default: inherit).",
            "cwd": "Optional cwd for a fresh child.",
            "image_id": "Required when source is image.",
            "allowed_effects": "Explicit task effects: read, write, test, execute, network, destructive.",
            "allowed_resources": "Workspace-relative resources covered by the task contract.",
            "required_evidence": "Completion evidence: response, read, write, test, artifact.",
            "active_capability_ids": "Optional minimum capability ids; current run capabilities are snapshotted automatically.",
            "approval_policy": "block or parent_fallback when an effect cannot run in background.",
        },
        input_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "source": {"type": "string", "enum": ["inherit", "fresh", "image"]},
                "cwd": {"type": "string"},
                "image_id": {"type": "string"},
                "allowed_effects": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["read", "write", "test", "execute", "network", "destructive"]},
                },
                "allowed_resources": {"type": "array", "items": {"type": "string"}},
                "required_evidence": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["response", "read", "write", "test", "artifact"]},
                },
                "active_capability_ids": {"type": "array", "items": {"type": "string"}},
                "approval_policy": {"type": "string", "enum": ["block", "parent_fallback"]},
            },
            "required": ["task"],
            "additionalProperties": False,
        },
        risk_level="write",
        handler=run,
    )


def _resume_tool() -> SimpleTool:
    def run(action: ToolAction, context: ToolContext) -> ToolResult:
        coordinator, _, parent, error = _runtime_state(context)
        if error is not None:
            return error("subagent_resume")
        try:
            child = coordinator.resume_subagent(
                parent,
                str(action.arguments["session_id"]),
                str(action.arguments["task"]),
            )
            cluster = coordinator.snapshot(parent)
            member = next(item for item in cluster.members if item.session_id == child.session_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            return ToolResult("subagent_resume", False, str(exc), "subagent_resume_failed")
        return ToolResult(
            "subagent_resume",
            True,
            json.dumps(
                {
                    "session_id": child.session_id,
                    "cluster_id": child.cluster_id,
                    "state": member.state,
                    "attempt": member.attempt,
                },
                ensure_ascii=False,
            ),
        )

    return SimpleTool(
        name="subagent_resume",
        description=(
            "Continue a completed, blocked, failed, or cancelled direct child session with a follow-up "
            "task. This preserves its conversation, run trace, capabilities, and session id. Call "
            "subagent_run_ready after resuming it."
        ),
        arguments={
            "session_id": "Existing direct child session id.",
            "task": "Follow-up task including the new feedback or failure evidence.",
        },
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "task": {"type": "string"},
            },
            "required": ["session_id", "task"],
            "additionalProperties": False,
        },
        risk_level="write",
        handler=run,
    )


def _run_tool() -> SimpleTool:
    def run(_action: ToolAction, context: ToolContext) -> ToolResult:
        coordinator, runner, parent, error = _runtime_state(context)
        if error is not None:
            return error("subagent_run_ready")
        try:
            results = runner.run_ready(parent)
        except (KeyError, RuntimeError, ValueError) as exc:
            return ToolResult("subagent_run_ready", False, str(exc), "subagent_run_failed")
        snapshot = coordinator.snapshot(parent)
        unresolved_members = [
            member for member in snapshot.members
            if member.role == "subagent" and member.state in {"ready", "running", "blocked", "failed"}
        ]
        completed = [result for result in results if result.state in {"completed", "skipped"}]
        blocked = [result for result in results if result.state == "blocked"]
        failed = [result for result in results if result.state not in {"completed", "skipped", "blocked"}]
        if completed and (blocked or failed or unresolved_members):
            outcome = "partial"
        elif blocked or any(member.state == "blocked" for member in unresolved_members):
            outcome = "blocked"
        elif failed or any(member.state == "failed" for member in unresolved_members):
            outcome = "failed"
        else:
            outcome = "completed"
        succeeded = outcome == "completed"
        error_code = None if succeeded else f"subagent_{outcome}"
        result_payload = [
            {
                "session_id": result.session_id,
                "state": result.state,
                "summary": _clip(result.final_message, 1000),
                "changed_files": list(result.changed_files or []),
                "verifications": list(result.verifications or []),
                "artifact_refs": list(result.artifact_refs or []),
                "blocker": dict(result.blocker or {}),
                "unresolved_requirements": list(result.unresolved_requirements or []),
                "output_validation": result.output_validation,
                "next_action": (
                    "accept_handoff_without_rereading_artifacts"
                    if result.state == "completed"
                    else "resume_same_session_with_feedback"
                ),
            }
            for result in results
        ]
        return ToolResult(
            "subagent_run_ready",
            succeeded,
            json.dumps(
                {
                    "outcome": outcome,
                    "results": result_payload,
                    "groups": {
                        "completed": [item["session_id"] for item in result_payload if item["state"] == "completed"],
                        "blocked": [member.session_id for member in snapshot.members if member.role == "subagent" and member.state == "blocked"],
                        "failed": [member.session_id for member in snapshot.members if member.role == "subagent" and member.state == "failed"],
                    },
                    "unresolved": [
                        {"session_id": member.session_id, "state": member.state, "task": member.task_summary}
                        for member in unresolved_members
                    ],
                },
                ensure_ascii=False,
            ),
            error_code,
        )

    return SimpleTool(
        name="subagent_run_ready",
        description=(
            "Execute every ready child session concurrently with isolated model/tool runtimes, then publish "
            "their bounded results to the session cluster public state."
        ),
        arguments={},
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk_level="execute",
        handler=run,
    )


def _status_tool() -> SimpleTool:
    def run(_action: ToolAction, context: ToolContext) -> ToolResult:
        coordinator, _, parent, error = _runtime_state(context)
        if error is not None:
            return error("subagent_status")
        if not parent.cluster_id:
            return ToolResult(
                "subagent_status",
                True,
                json.dumps({"cluster_id": "", "revision": 0, "members": [], "public_state": []}),
            )
        try:
            cluster = coordinator.snapshot(parent)
        except (KeyError, RuntimeError, ValueError) as exc:
            return ToolResult("subagent_status", False, str(exc), "subagent_status_failed")
        return ToolResult(
            "subagent_status",
            True,
            json.dumps(
                {
                    "cluster_id": cluster.cluster_id,
                    "revision": cluster.revision,
                    "members": [
                        {
                            "session_id": member.session_id,
                            "role": member.role,
                            "state": member.state,
                            "task": member.task_summary,
                            "attempt": member.attempt,
                            "task_id": member.task_id,
                            "allowed_effects": member.allowed_effects,
                            "required_evidence": member.required_evidence,
                        }
                        for member in cluster.members
                    ],
                    "public_state": [
                        {
                            "revision": entry.revision,
                            "author_session_id": entry.author_session_id,
                            "kind": entry.kind,
                            "summary": entry.summary,
                            "artifact_ref": entry.artifact_ref,
                            "metadata": entry.metadata,
                            "task_id": entry.task_id,
                            "attempt": entry.attempt,
                            "trust_class": entry.trust_class,
                            "validation_state": entry.validation_state,
                            "supersedes": entry.supersedes,
                        }
                        for entry in cluster.shared_state[-20:]
                    ],
                },
                ensure_ascii=False,
            ),
        )

    return SimpleTool(
        name="subagent_status",
        description="Read member lifecycle and bounded public results for the current session cluster.",
        arguments={},
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk_level="read",
        handler=run,
    )


def _runtime_state(context: ToolContext):
    coordinator = context.state.get("subagent_coordinator")
    runner = context.state.get("subagent_runner")
    session_id = context.state.get("active_session_id")

    def error(tool_name: str) -> ToolResult:
        return ToolResult(
            tool_name,
            False,
            "subagent tools require an active persisted session and configured subagent runtime",
            "subagent_runtime_unavailable",
        )

    if coordinator is None or runner is None or not isinstance(session_id, str) or not session_id:
        return coordinator, runner, None, error
    parent = coordinator.session_store.load(session_id)
    if parent is None:
        return coordinator, runner, None, error
    return coordinator, runner, parent, None


def _clip(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."
