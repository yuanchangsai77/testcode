from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Callable, Protocol

from ..intent import RequestIntentClassifier
from ..types import ExecutionSummary, StoredSession, UserRequest
from .subagents import SubagentCoordinator


_GRANT_ISSUER = object()


class SubagentRuntime(Protocol):
    def run_background(self, request: UserRequest) -> ExecutionSummary: ...

    def persist_run(
        self,
        session: StoredSession,
        prompt: str,
        summary: ExecutionSummary,
        *,
        status: str = "active",
        close_runtime: bool = False,
    ) -> None: ...


@dataclass(slots=True)
class SubagentRunResult:
    session_id: str
    state: str
    final_message: str
    changed_files: list[str] | None = None
    verifications: list[dict[str, object]] | None = None
    artifact_refs: list[str] | None = None


@dataclass(frozen=True, slots=True)
class SubagentExecutionGrant:
    cluster_id: str
    session_id: str
    parent_session_id: str
    attempt: int
    workspace_root: str
    _issuer: object | None = None

    def is_runner_issued(self) -> bool:
        return self._issuer is _GRANT_ISSUER


def _issue_subagent_grant(
    *,
    cluster_id: str,
    session_id: str,
    parent_session_id: str,
    attempt: int,
    workspace_root: str,
) -> SubagentExecutionGrant:
    return SubagentExecutionGrant(
        cluster_id=cluster_id,
        session_id=session_id,
        parent_session_id=parent_session_id,
        attempt=attempt,
        workspace_root=workspace_root,
        _issuer=_GRANT_ISSUER,
    )


class SubagentRunner:
    """Runs ready child sessions with isolated runtimes and reports through public state."""

    def __init__(
        self,
        coordinator: SubagentCoordinator,
        runtime_factory: Callable[[StoredSession, SubagentExecutionGrant], SubagentRuntime],
        *,
        max_workers: int = 4,
    ) -> None:
        self.coordinator = coordinator
        self.runtime_factory = runtime_factory
        self.max_workers = max(1, max_workers)
        self._active_lock = Lock()
        self._active_runtimes: dict[str, SubagentRuntime] = {}
        self._cancelled_sessions: set[str] = set()
        self._intent_classifier = RequestIntentClassifier()

    def run_ready(self, requester: StoredSession) -> list[SubagentRunResult]:
        snapshot = self.coordinator.snapshot(requester)
        ready_ids = [
            member.session_id
            for member in snapshot.members
            if member.role == "subagent" and member.state == "ready"
        ]
        if not ready_ids:
            return []
        worker_count = min(self.max_workers, len(ready_ids))
        pool = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="testcode-subagent")
        futures: list[Future[SubagentRunResult]] = [
            pool.submit(self._run_one, snapshot.cluster_id, session_id)
            for session_id in ready_ids
        ]
        try:
            results = [future.result() for future in futures]
        except KeyboardInterrupt:
            self.cancel_running(snapshot.cluster_id, ready_ids)
            for future in futures:
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)
            return results

    def cancel_running(self, cluster_id: str, session_ids: list[str] | None = None) -> None:
        """Cancel active runtimes and make cancellation win over late worker results."""
        selected = set(session_ids or [])
        with self._active_lock:
            active = {
                session_id: runtime
                for session_id, runtime in self._active_runtimes.items()
                if not selected or session_id in selected
            }
            candidates = selected or set(active)
            self._cancelled_sessions.update(candidates)
        for session_id in candidates:
            runtime = active.get(session_id)
            if runtime is not None:
                engine = getattr(runtime, "engine", None)
                cancel = getattr(engine, "cancel_current_run", None)
                if callable(cancel):
                    cancel()
            cluster = self.coordinator.cluster_store.load(cluster_id)
            member = next(
                (item for item in cluster.members if item.session_id == session_id),
                None,
            ) if cluster is not None else None
            if member is None or member.state not in {"ready", "running"}:
                continue
            child = self.coordinator.session_store.load(session_id)
            if child is None:
                continue
            committed = self.coordinator.finish_attempt(
                child,
                member.attempt,
                "cancelled",
                "blocker",
                "Subagent run was interrupted by the user.",
                expected_states=frozenset({"ready", "running"}),
                metadata={"outcome": "cancelled", "attempt": member.attempt},
            )
            if committed is not None:
                child.status = "cancelled"
                self.coordinator.session_store.save(child)

    def _run_one(self, cluster_id: str, session_id: str) -> SubagentRunResult:
        with self._active_lock:
            self._cancelled_sessions.discard(session_id)
        if not self.coordinator.cluster_store.claim_ready_member(cluster_id, session_id):
            return SubagentRunResult(session_id=session_id, state="skipped", final_message="already claimed")
        child = self.coordinator.session_store.load(session_id)
        if child is None:
            return self._fail_missing_session(cluster_id, session_id)

        cluster = self.coordinator.cluster_store.load(cluster_id)
        member = next(
            (item for item in cluster.members if item.session_id == session_id),
            None,
        ) if cluster is not None else None
        task = member.task_summary.strip() if member is not None else ""
        if not task:
            return self._fail(child, "Subagent has no delegated task.", member.attempt if member else 1)

        request = UserRequest(
            prompt=task,
            cwd=child.cwd,
            metadata={
                "conversation": [*child.messages, *self._public_context(cluster, session_id)],
                "session_id": child.session_id,
                "active_skills": list(child.active_skills),
                "active_capability_ids": list(child.active_capability_ids),
                "session_trace": list(child.trace[-6:]),
                "resume_state": child.resume_state,
                "subagent": {
                    "cluster_id": cluster_id,
                    "parent_session_id": child.parent_session_id,
                    "role": "subagent",
                    "attempt": member.attempt if member is not None else 1,
                },
            },
        )
        try:
            attempt = member.attempt if member is not None else 1
            grant = _issue_subagent_grant(
                cluster_id=cluster_id,
                session_id=child.session_id,
                parent_session_id=child.parent_session_id,
                attempt=attempt,
                workspace_root=str(Path(child.cwd).resolve()),
            )
            runtime = self.runtime_factory(child, grant)
            with self._active_lock:
                self._active_runtimes[session_id] = runtime
                cancelled_before_start = session_id in self._cancelled_sessions
            if cancelled_before_start:
                return SubagentRunResult(child.session_id, "cancelled", "Subagent run was interrupted.")
            summary = runtime.run_background(request)
            with self._active_lock:
                cancelled = session_id in self._cancelled_sessions
            if cancelled:
                return SubagentRunResult(child.session_id, "cancelled", "Subagent run was interrupted.")
            outcome = getattr(summary, "outcome", "completed")
            completion_problem = self._completion_problem(task, summary)
            if outcome == "completed" and completion_problem:
                summary.outcome = "stalled"
                summary.final_message = completion_problem
                outcome = "stalled"
            state = self._member_state(outcome)
            result_text = _bounded_summary(summary.final_message)
            changed_files, verifications, artifact_refs = self._handoff_evidence(summary)
            committed = self.coordinator.finish_attempt(
                child,
                attempt,
                state,
                "status" if state == "completed" else "blocker",
                result_text,
                artifact_ref=artifact_refs[0] if artifact_refs else "",
                metadata={
                    "outcome": outcome,
                    "tool_result_count": len(summary.tool_results),
                    "successful_tool_results": sum(result.success for result in summary.tool_results),
                    "attempt": attempt,
                    "changed_files": changed_files,
                    "verifications": verifications,
                    "artifact_refs": artifact_refs,
                    "handoff_policy": (
                        "accept_summary_unless_targeted_verification_is_required"
                        if state == "completed"
                        else "resume_same_session_with_failure_evidence"
                    ),
                },
            )
            if committed is None:
                latest = self.coordinator.snapshot(child)
                latest_member = next(item for item in latest.members if item.session_id == child.session_id)
                return SubagentRunResult(
                    child.session_id,
                    latest_member.state,
                    "Subagent result was superseded by a newer attempt or cancellation.",
                    changed_files=[],
                    verifications=[],
                    artifact_refs=[],
                )
            runtime.persist_run(child, task, summary, status=state, close_runtime=True)
            return SubagentRunResult(
                child.session_id,
                state,
                summary.final_message,
                changed_files=changed_files,
                verifications=verifications,
                artifact_refs=artifact_refs,
            )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return self._fail(
                child,
                str(error) or type(error).__name__,
                member.attempt if member is not None else 1,
                expected_states=frozenset({"running", "completed", "blocked"}),
            )
        finally:
            with self._active_lock:
                self._active_runtimes.pop(session_id, None)

    def _completion_problem(self, task: str, summary: ExecutionSummary) -> str:
        final_message = summary.final_message.strip()
        if not final_message:
            return "Subagent did not provide a completion summary; resume this session."
        orchestration_names = {"subagent_spawn", "subagent_run_ready", "subagent_resume"}
        if not any(name in task for name in orchestration_names) and any(
            name in final_message for name in orchestration_names
        ):
            return (
                "Subagent completion did not address the delegated task and discussed unavailable "
                "orchestration tools instead; resume this session with the task evidence."
            )
        intent = self._intent_classifier.classify(task)
        if intent.file_changes and not any(
            result.success and result.name == "patch"
            for result in summary.tool_results
        ):
            return (
                "Subagent claimed completion for a file-change task without a successful write; "
                "resume this session with the missing delivery evidence."
            )
        return ""

    def _member_state(self, outcome: str) -> str:
        if outcome == "completed":
            return "completed"
        if outcome in {"blocked", "stalled", "exhausted"}:
            return "blocked"
        return "failed"

    def _handoff_evidence(
        self,
        summary: ExecutionSummary,
    ) -> tuple[list[str], list[dict[str, object]], list[str]]:
        changed_files: set[str] = set()
        artifact_refs: set[str] = set()
        verifications: list[dict[str, object]] = []
        for result in summary.tool_results:
            metadata = result.metadata if isinstance(result.metadata, dict) else {}
            if result.success:
                files = metadata.get("changed_files", [])
                if isinstance(files, list):
                    changed_files.update(item for item in files if isinstance(item, str) and item)
            artifact_ref = metadata.get("artifact_ref")
            if isinstance(artifact_ref, str) and self._safe_artifact_ref(artifact_ref):
                artifact_refs.add(artifact_ref)
            refs = metadata.get("artifact_refs", [])
            if isinstance(refs, list):
                artifact_refs.update(
                    item for item in refs if isinstance(item, str) and self._safe_artifact_ref(item)
                )
            if result.name == "run_tests":
                verifications.append(
                    {
                        "tool": result.name,
                        "success": result.success,
                        "error_code": result.error_code or "",
                        "command": _bounded_field(str(metadata.get("command", "")), 500),
                        "duration_seconds": metadata.get("duration_seconds"),
                    }
                )
        return sorted(changed_files)[:20], verifications[-10:], sorted(artifact_refs)[:20]

    def _safe_artifact_ref(self, value: str) -> bool:
        if not value or len(value) > 500:
            return False
        if value.startswith("artifact:"):
            return True
        path = PurePosixPath(value.replace("\\", "/"))
        return not path.is_absolute() and ".." not in path.parts

    def _fail_missing_session(self, cluster_id: str, session_id: str) -> SubagentRunResult:
        cluster = self.coordinator.cluster_store.load(cluster_id)
        member = next(
            (item for item in cluster.members if item.session_id == session_id),
            None,
        ) if cluster is not None else None
        if member is not None:
            self.coordinator.cluster_store.finish_member_attempt(
                cluster_id,
                session_id,
                member.attempt,
                "failed",
                "blocker",
                "Subagent session record is missing.",
                metadata={"outcome": "failed", "attempt": member.attempt},
            )
        return SubagentRunResult(session_id, "failed", "Subagent session record is missing.")

    def _fail(
        self,
        child: StoredSession,
        message: str,
        attempt: int,
        *,
        expected_states: frozenset[str] = frozenset({"running"}),
    ) -> SubagentRunResult:
        committed = self.coordinator.finish_attempt(
            child,
            attempt,
            "failed",
            "blocker",
            _bounded_summary(message),
            expected_states=expected_states,
            metadata={"outcome": "failed", "attempt": attempt},
        )
        if committed is None:
            snapshot = self.coordinator.snapshot(child)
            member = next(item for item in snapshot.members if item.session_id == child.session_id)
            return SubagentRunResult(child.session_id, member.state, message)
        child.status = "failed"
        self.coordinator.session_store.save(child)
        return SubagentRunResult(child.session_id, "failed", message)

    def _public_context(self, cluster, session_id: str) -> list[dict[str, str]]:
        if cluster is None or not cluster.shared_state:
            return []
        lines = ["Session cluster public state (structured shared space; not direct messages):"]
        for entry in cluster.shared_state[-20:]:
            lines.append(f"- [{entry.kind}] {entry.author_session_id}: {entry.summary}")
        return [{"role": "system", "content": "\n".join(lines)}]


def _bounded_summary(value: str, limit: int = 2000) -> str:
    normalized = " ".join(value.split()) or "Subagent finished without a textual result."
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _bounded_field(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."
