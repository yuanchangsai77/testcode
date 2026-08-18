from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from ..intent import RequestIntentClassifier
from ..model.types import ModelRetryableError
from ..types import EvidenceRecord, ExecutionSummary, RuntimeBlocker, TaskCheckpoint, ToolAction, ToolResult, UserRequest
from .ext import ContextLoader
from .permissions import PermissionContext
from .progress import (
    DefaultProgressPolicy,
    ProgressContext,
    ProgressPolicy,
    ProgressReporter,
    ProgressSignal,
)
from .session import SessionContext
from .control import CompletionPolicy, RunBudgetPolicy


class ExecutionEngine:
    """Coordinates the model-think and tool-execute loop."""

    non_resource_control_tools = {
        "warehouse_list",
        "toolbox_open",
        "capability_activate",
        "capability_release",
        "capability_status",
    }

    max_model_retries = 7
    model_retry_delays = (0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0)

    non_retryable_error_codes = {
        "approval_denied",
        "approval_required",
        "blocked_by_security_policy",
        "blocked_by_policy",
        "duplicate_tool_call",
        "delegated_effect_not_allowed",
        "delegated_resource_not_allowed",
        "invalid_argument_type",
        "invalid_argument_value",
        "invalid_patch",
        "missing_argument",
        "patch_syntax_error",
        "path_outside_workspace",
        "path_not_found",
        "subagent_blocked",
        "subagent_failed",
        "subagent_partial",
        "test_command_ambiguous",
        "test_command_not_detected",
        "unknown_argument",
    }

    def __init__(
        self,
        model,
        tools,
        guardrails,
        logger,
        context_loaders: list[ContextLoader] | None = None,
        capability_warehouse=None,
        approval_callback=None,
        progress_reporter: ProgressReporter | None = None,
        presenter=None,
        max_model_retries: int = 7,
        model_retry_delays: tuple[float, ...] | None = None,
        max_turns: int = 100,
        max_model_attempts: int = 120,
        max_consecutive_model_timeouts: int = 8,
        max_run_seconds: float = 900.0,
        mcp_server_count: int = 0,
        intent_classifier: RequestIntentClassifier | None = None,
        progress_policy: ProgressPolicy | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.guardrails = guardrails
        self.logger = logger
        self.context_loaders = context_loaders or []
        self.capability_warehouse = capability_warehouse
        self.approval_callback = approval_callback
        self.progress_reporter = progress_reporter or presenter
        self.max_model_retries = max(0, int(max_model_retries))
        self.model_retry_delays = tuple(model_retry_delays or self.model_retry_delays)
        self.max_turns = max(1, int(max_turns))
        self.max_model_attempts = max(1, int(max_model_attempts))
        self.max_consecutive_model_timeouts = max(1, int(max_consecutive_model_timeouts))
        self.max_run_seconds = max(1.0, float(max_run_seconds))
        self.run_budget_policy = RunBudgetPolicy(
            max_model_attempts=self.max_model_attempts,
            max_consecutive_model_timeouts=self.max_consecutive_model_timeouts,
            max_run_seconds=self.max_run_seconds,
        )
        self.completion_policy = CompletionPolicy()
        self.mcp_server_count = max(0, int(mcp_server_count))
        self.intent_classifier = intent_classifier or RequestIntentClassifier()
        self.progress_policy = progress_policy or DefaultProgressPolicy()
        self.max_duplicate_skips = 3
        self._tool_state_session_key: str | None = None
        self._keep_tool_state = False
        self._runtime_cancelled = False
        self._cancel_event = threading.Event()
        self.last_failure_summary: ExecutionSummary | None = None
        self.current_session: SessionContext | None = None

    def execute(self, request: UserRequest) -> ExecutionSummary:
        self._runtime_cancelled = False
        self._cancel_event.clear()
        self.last_failure_summary = None
        try:
            return self._execute(request)
        except KeyboardInterrupt:
            self.cancel_current_run()
            raise
        except Exception as error:
            self.last_failure_summary = self._runtime_failure_summary(error)
            self.current_session = None
            raise

    def cancel_current_run(self) -> None:
        """Stop runtime tools after an interrupted execution.

        This is idempotent because both an execution frontend and the engine
        itself may observe the same KeyboardInterrupt.
        """
        if self._runtime_cancelled:
            return
        self._runtime_cancelled = True
        self._cancel_event.set()
        self.current_session = None
        self._tool_state_session_key = None
        reset_state = getattr(self.tools, "reset_state", None)
        if callable(reset_state):
            reset_state()

    def _execute(self, request: UserRequest) -> ExecutionSummary:
        runtime_model = getattr(self.model, "model", "")
        if isinstance(runtime_model, str) and runtime_model:
            request.metadata.setdefault("runtime_model", runtime_model)
        session_key = self._session_key(request)
        self.prepare_session_state(
            session_key,
            request.metadata.get("active_capability_ids", []),
        )
        attach_state = getattr(self.tools, "attach_state", None)
        if callable(attach_state):
            attach_state("active_session_id", session_key)
        self._keep_tool_state = session_key is not None
        permissions = PermissionContext()
        available_tools = self._available_tool_definitions(request)
        provider_statuses = getattr(self.tools, "provider_statuses", lambda: [])()
        checkpoint = self._initial_checkpoint(request)
        session = SessionContext(
            request=request,
            available_tools=available_tools,
            external_tool_statuses=provider_statuses,
            checkpoint=checkpoint,
        )
        self.current_session = session

        for loader in self.context_loaders:
            loader.load_context(request, session)
        if self.capability_warehouse is not None:
            self.capability_warehouse.apply_to_session(session)

        consecutive_non_retryable_turns = 0
        consecutive_failed_test_turns = 0
        approved_risk_groups: set[tuple[str, str]] = set()
        completed_actions: dict[str, ToolResult] = {}
        duplicate_counts: dict[str, int] = {}
        progress_recovery_sent = False
        request_intent = self.intent_classifier.classify(
            self._intent_prompt(request),
            request.metadata,
        )
        session.checkpoint.required_evidence = self._required_evidence(request, request_intent)
        session.checkpoint.unmet_deliverables = self._unmet_evidence(session)
        invalid_completion_count = 0
        run_started = time.monotonic()
        model_attempts = 0
        consecutive_model_timeouts = 0

        for turn in range(1, self.max_turns + 1):
            self._raise_if_cancelled()
            budget_problem = self._run_budget_problem(
                run_started,
                model_attempts,
                consecutive_model_timeouts,
            )
            if budget_problem is not None:
                return self._finish(self._budget_summary(session, budget_problem))
            self._sync_checkpoint_blockers(session)
            session.available_tools = self._available_tool_definitions(request)
            visible_tool_names = {definition.name for definition in session.available_tools}
            expiring_turn_capabilities = (
                self.capability_warehouse.active_ids({"turn"})
                if self.capability_warehouse is not None
                else []
            )
            if self.capability_warehouse is not None:
                self.capability_warehouse.apply_to_session(session)
            progress_handle = None
            if self.progress_reporter:
                progress_handle = self.progress_reporter.model_started()
            stream_observer_setter = getattr(
                self.model,
                "set_natural_language_stream_observer",
                None,
            )
            stream_reporter = getattr(self.progress_reporter, "model_stream_delta", None)
            if callable(stream_observer_setter):
                if callable(stream_reporter):
                    stream_observer_setter(
                        lambda delta: stream_reporter(
                            progress_handle,
                            delta.channel,
                            delta.text,
                        )
                    )
                else:
                    stream_observer_setter(None)
            reply = None
            try:
                retry_count = 0
                budget_problem = None
                while True:
                    if retry_count > 0:
                        retry_reporter = getattr(self.progress_reporter, "model_retrying", None)
                        if retry_reporter is not None:
                            retry_reporter(
                                progress_handle,
                                retry_count,
                                self.max_model_retries,
                                "Sending request",
                                0.0,
                            )
                    try:
                        budget_problem = self._run_budget_problem(
                            run_started,
                            model_attempts,
                            consecutive_model_timeouts,
                        )
                        if budget_problem is not None:
                            break
                        model_attempts += 1
                        session.checkpoint.runtime_state["model_attempts"] = str(model_attempts)
                        reply = self.model.respond(session)
                        consecutive_model_timeouts = 0
                        self._raise_if_cancelled()
                        break
                    except (ModelRetryableError, TimeoutError) as error:
                        if self._is_timeout_error(error):
                            consecutive_model_timeouts += 1
                            session.checkpoint.runtime_state["consecutive_model_timeouts"] = str(
                                consecutive_model_timeouts
                            )
                        else:
                            consecutive_model_timeouts = 0
                            session.checkpoint.runtime_state["consecutive_model_timeouts"] = "0"
                        budget_problem = self._run_budget_problem(
                            run_started,
                            model_attempts,
                            consecutive_model_timeouts,
                        )
                        if budget_problem is not None:
                            break
                        if retry_count >= self.max_model_retries:
                            raise RuntimeError(
                                f"All {self.max_model_retries} retry attempts failed "
                                f"after the initial request: {error}"
                            ) from error
                        retry_count += 1
                        retry_delay = self.model_retry_delays[min(retry_count - 1, len(self.model_retry_delays) - 1)]
                        self.logger.record(
                            "model.retry",
                            {
                                "retry": retry_count,
                                "max_retries": self.max_model_retries,
                                "delay_seconds": retry_delay,
                                "reason": str(error),
                            },
                        )
                        retry_reporter = getattr(self.progress_reporter, "model_retrying", None)
                        if retry_reporter is not None:
                            retry_reporter(
                                progress_handle,
                                retry_count,
                                self.max_model_retries,
                                getattr(error, "retry_status", "Model request timed out"),
                                retry_delay,
                            )
                        if self._cancel_event.wait(retry_delay):
                            raise KeyboardInterrupt
            finally:
                if callable(stream_observer_setter):
                    stream_observer_setter(None)
                if progress_handle is not None:
                    response_ready = getattr(
                        self.progress_reporter,
                        "model_response_ready",
                        None,
                    )
                    if callable(response_ready) and reply is not None:
                        response_ready(
                            progress_handle,
                            reply.message,
                            reply.done and not reply.actions,
                        )
                    self.progress_reporter.model_finished(progress_handle)
            if budget_problem is not None:
                return self._finish(self._budget_summary(session, budget_problem))
            session.add_model_message(reply.message)
            self.logger.record(
                "model.reply",
                {
                    "turn": turn,
                    "message": reply.message,
                    "done": reply.done,
                    "actions": [action.name for action in reply.actions],
                },
            )

            turn_results: list[ToolResult] = []
            for action in reply.actions:
                self._raise_if_cancelled()
                if action.name not in visible_tool_names:
                    definition = self.tools.definition_for(action.name)
                    result = self._delegated_effect_problem(definition, request)
                    if result is None:
                        result = ToolResult(
                            name=action.name,
                            success=False,
                            output=(
                                f"tool was not visible at the start of this model turn: {action.name}. "
                                "A newly activated capability can be used on the next model turn."
                            ),
                            error_code="tool_not_visible_this_turn",
                        )
                    self._attach_action_metadata(result, action)
                    self._record_synthetic_tool_result(result)
                    session.add_tool_result(result)
                    turn_results.append(result)
                    continue
                action_key = self._action_key(action)
                if action_key in completed_actions:
                    duplicate_counts[action_key] = duplicate_counts.get(action_key, 0) + 1
                    result = self._duplicate_result(action, completed_actions[action_key], duplicate_counts[action_key])
                    if self.progress_reporter:
                        self.progress_reporter.tool_skipped(action, "duplicate skipped")
                    self._record_synthetic_tool_result(result)
                    session.add_tool_result(result)
                    turn_results.append(result)
                    continue

                preflight = getattr(self.tools, "preflight", None)
                if callable(preflight):
                    result = preflight(
                        action,
                        cwd=request.cwd,
                        allowed_roots=permissions.workspace_roots(scopes={"run"}),
                    )
                    if result is not None:
                        self._attach_action_metadata(result, action)
                        self._record_synthetic_tool_result(result)
                        if self.progress_reporter:
                            self.progress_reporter.tool_skipped(action, "blocked by preflight")
                        session.add_tool_result(result)
                        turn_results.append(result)
                        if result.error_code in self.non_retryable_error_codes:
                            completed_actions[action_key] = result
                        continue

                definition = self.tools.definition_for(action.name)
                delegated_scope_problem = self._delegated_scope_problem(
                    action,
                    definition,
                    request,
                )
                if delegated_scope_problem is not None:
                    self._attach_action_metadata(delegated_scope_problem, action)
                    self._record_synthetic_tool_result(delegated_scope_problem)
                    session.add_tool_result(delegated_scope_problem)
                    turn_results.append(delegated_scope_problem)
                    completed_actions[action_key] = delegated_scope_problem
                    continue
                decision = self.guardrails.check(action, definition)
                if decision.requires_confirmation:
                    approval_key = (action.name, decision.risk_level)
                    approval = (
                        True
                        if self._approval_remembered(approval_key, approved_risk_groups)
                        else self._approval_decision(action, decision.reason)
                    )
                    if approval is True:
                        if decision.risk_level != "destructive":
                            approved_risk_groups.add(approval_key)
                        result = self._execute_action(
                            action,
                            request.cwd,
                            permissions,
                        )
                        session.add_tool_result(result)
                        turn_results.append(result)
                        if result.success or result.error_code in self.non_retryable_error_codes:
                            if result.success and self._may_mutate_workspace(decision.risk_level):
                                completed_actions.clear()
                                duplicate_counts.clear()
                                progress_recovery_sent = False
                            completed_actions[action_key] = result
                        continue

                    result = ToolResult(
                        name=action.name,
                        success=False,
                        output=(
                            "Tool execution was declined by the user."
                            if approval is False
                            else decision.reason
                        ),
                        error_code="approval_denied" if approval is False else "approval_required",
                    )
                    if self.progress_reporter:
                        self.progress_reporter.tool_skipped(action, "denied by user")
                    self._attach_action_metadata(result, action)
                    self._record_synthetic_tool_result(result)
                    session.add_tool_result(result)
                    turn_results.append(result)
                    continue

                if not decision.allowed:
                    result = ToolResult(
                        name=action.name,
                        success=False,
                        output=decision.reason,
                        error_code=decision.error_code or "blocked_by_policy",
                    )
                    if self.progress_reporter:
                        self.progress_reporter.tool_skipped(action, f"blocked: {decision.reason}")
                    self._attach_action_metadata(result, action)
                    session.add_tool_result(result)
                    turn_results.append(result)
                    continue

                result = self._execute_action(
                    action,
                    request.cwd,
                    permissions,
                )
                session.add_tool_result(result)
                turn_results.append(result)
                if result.success or result.error_code in self.non_retryable_error_codes:
                    if result.success and self._may_mutate_workspace(decision.risk_level):
                        completed_actions.clear()
                        duplicate_counts.clear()
                        progress_recovery_sent = False
                    completed_actions[action_key] = result

            recovery_required = any(
                result.error_code
                in {
                    "blocked_by_security_policy",
                    "file_changed_since_read",
                    "file_not_read",
                }
                for result in turn_results
            )
            if reply.done and not recovery_required:
                session.checkpoint.unmet_deliverables = self._unmet_evidence(session)
                completion_problem = self._completion_problem(reply.message, session)
                if completion_problem:
                    invalid_completion_count += 1
                    result = ToolResult(
                        name="completion_gate",
                        success=False,
                        output=completion_problem,
                        error_code="model_output_invalid",
                        metadata={
                            "retryability": "conditional",
                            "required_action": "provide_meaningful_final_answer",
                        },
                    )
                    self._record_synthetic_tool_result(result)
                    session.add_tool_result(result)
                    if invalid_completion_count >= 2:
                        return self._finish(
                            ExecutionSummary(
                                final_message=completion_problem,
                                tool_results=session.tool_results,
                                outcome="stalled",
                                active_instructions=session.active_instructions,
                            )
                        )
                    continue
                if invalid_completion_count:
                    result = ToolResult(
                        name="completion_gate",
                        success=True,
                        output="Model supplied a valid replacement completion.",
                    )
                    self._record_synthetic_tool_result(result)
                    session.add_tool_result(result)
                return self._finish(
                    ExecutionSummary(
                        final_message=reply.message,
                        tool_results=session.tool_results,
                        outcome=self._terminal_outcome(turn_results),
                        active_instructions=session.active_instructions,
                    )
                )

            if expiring_turn_capabilities and self.capability_warehouse is not None:
                self.capability_warehouse.release(
                    expiring_turn_capabilities,
                    reason="turn scope consumed",
                )

            if self._has_failed_test_result(turn_results):
                consecutive_failed_test_turns += 1
                if consecutive_failed_test_turns >= 3:
                    return self._finish(
                        ExecutionSummary(
                            final_message="Stopping after 3 consecutive failing test runs. Review the latest test output before retrying.",
                            tool_results=session.tool_results,
                            outcome="stalled",
                            active_instructions=session.active_instructions,
                        )
                    )
            elif turn_results:
                consecutive_failed_test_turns = 0

            progress_signal = self.progress_policy.evaluate(
                ProgressContext(
                    intent=request_intent,
                    results=turn_results,
                    recovery_sent=progress_recovery_sent,
                )
            )
            if progress_signal is not None:
                progress_result = self._progress_recovery_result(progress_signal)
                self._record_synthetic_tool_result(progress_result)
                session.add_tool_result(progress_result)
                progress_recovery_sent = True
                consecutive_non_retryable_turns = 0
                continue

            if turn_results and self._all_non_retryable(turn_results):
                consecutive_non_retryable_turns += 1
                if consecutive_non_retryable_turns >= 2:
                    return self._finish(
                        ExecutionSummary(
                            final_message=self._non_retryable_failure_message(turn_results),
                            tool_results=session.tool_results,
                            outcome=self._blocked_outcome(turn_results),
                            active_instructions=session.active_instructions,
                        )
                    )
            else:
                consecutive_non_retryable_turns = 0

        return self._finish(
            ExecutionSummary(
                final_message="Model exceeded the maximum number of turns without producing a final answer.",
                tool_results=session.tool_results,
                outcome="exhausted",
                active_instructions=session.active_instructions,
            )
        )

    def _raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise KeyboardInterrupt

    def _finish(self, summary: ExecutionSummary) -> ExecutionSummary:
        if summary.outcome == "completed" and summary.tool_results:
            summary.outcome = self._aggregate_outcome(summary.tool_results)
        if self.current_session is not None:
            summary.checkpoint = self.current_session.checkpoint
        unresolved = self._unresolved_results(summary.tool_results)
        if summary.outcome == "completed":
            summary.blockers = []
            summary.checkpoint.blockers = []
            summary.checkpoint.phase = "completed"
        else:
            summary.blockers = (
                list(summary.blockers)
                if summary.blockers and not unresolved
                else self._runtime_blockers(unresolved, summary)
            )
            summary.checkpoint.blockers = list(summary.blockers)
            summary.checkpoint.phase = "blocked" if summary.outcome == "blocked" else "incomplete"
        self.current_session = None
        if self._keep_tool_state and self.capability_warehouse is not None:
            summary.active_instructions = self.capability_warehouse.persisted_instructions()
            summary.active_capability_ids = self.capability_warehouse.persisted_capability_ids()
            self.capability_warehouse.release_scopes({"turn", "run"})
        if not self._keep_tool_state:
            close_state = getattr(self.tools, "reset_state", None)
            if callable(close_state):
                close_state()
        return summary

    def _initial_checkpoint(self, request: UserRequest) -> TaskCheckpoint:
        workspace_root = str(Path(request.cwd).resolve())
        resume_state = request.metadata.get("resume_state")
        previous = getattr(resume_state, "checkpoint", None)
        previous_outcome = getattr(resume_state, "last_outcome", "")
        if previous is None and isinstance(resume_state, dict):
            previous = resume_state.get("checkpoint")
            previous_outcome = str(resume_state.get("last_outcome", ""))
        resumable_outcomes = {
            "blocked",
            "stalled",
            "runtime_error",
            "interrupted",
            "exhausted",
            "failed",
            "model_output_invalid",
        }
        delegated = request.metadata.get("delegated_task")
        delegated_task_id = delegated.get("task_id") if isinstance(delegated, dict) else ""
        requested_task_id = request.metadata.get("task_id") or delegated_task_id
        previous_task_id = (
            previous.task_id if isinstance(previous, TaskCheckpoint)
            else str(previous.get("task_id", "")) if isinstance(previous, dict)
            else ""
        )
        previous_root = (
            previous.workspace_root if isinstance(previous, TaskCheckpoint)
            else str(previous.get("workspace_root", "")) if isinstance(previous, dict)
            else ""
        )
        previous_objective = (
            previous.objective if isinstance(previous, TaskCheckpoint)
            else str(previous.get("objective", "")) if isinstance(previous, dict)
            else ""
        )
        explicit_resume_id = request.metadata.get("resume_task_id")
        normalized_prompt = " ".join(request.prompt.split()).casefold()
        explicit_continuation_prompts = {
            "continue",
            "resume",
            "继续",
            "这里继续",
            "接着",
            "继续处理",
            "接着做",
            "接着处理",
        }
        resume_requested = request.metadata.get("continue_task") is True or (
            isinstance(explicit_resume_id, str) and explicit_resume_id == previous_task_id
        ) or (
            isinstance(requested_task_id, str) and requested_task_id == previous_task_id
        ) or (
            normalized_prompt in explicit_continuation_prompts
        ) or (
            previous_objective
            and " ".join(previous_objective.split()).casefold()
            == normalized_prompt
        )
        can_resume = (
            previous_outcome in resumable_outcomes
            and bool(previous_task_id)
            and resume_requested
            and previous_root == workspace_root
        )
        task_id = previous_task_id if can_resume else (
            str(requested_task_id) if requested_task_id else uuid4().hex
        )
        checkpoint = TaskCheckpoint(
            objective=previous_objective if can_resume else request.prompt,
            task_id=task_id,
            workspace_root=workspace_root,
        )
        if isinstance(previous, TaskCheckpoint) and can_resume:
            checkpoint.completed_actions = list(previous.completed_actions[-50:])
            checkpoint.artifacts = list(previous.artifacts[-50:])
            checkpoint.workspace_revision = previous.workspace_revision
            checkpoint.evidence = list(previous.evidence[-100:])
            checkpoint.runtime_state = dict(previous.runtime_state)
        elif isinstance(previous, dict) and can_resume:
            completed = previous.get("completed_actions", [])
            artifacts = previous.get("artifacts", [])
            evidence = previous.get("evidence", [])
            runtime_state = previous.get("runtime_state", {})
            try:
                checkpoint.workspace_revision = max(0, int(previous.get("workspace_revision", 0)))
            except (TypeError, ValueError):
                checkpoint.workspace_revision = 0
            if isinstance(completed, list):
                checkpoint.completed_actions = [str(item) for item in completed[-50:]]
            if isinstance(artifacts, list):
                checkpoint.artifacts = [str(item) for item in artifacts[-50:]]
            if isinstance(evidence, list):
                for item in evidence[-100:]:
                    if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
                        continue
                    try:
                        revision = max(0, int(item.get("workspace_revision", 0)))
                    except (TypeError, ValueError):
                        revision = 0
                    checkpoint.evidence.append(
                        EvidenceRecord(
                            kind=item["kind"],
                            producer=str(item.get("producer", "unknown")),
                            task_id=str(item.get("task_id", task_id)),
                            workspace_revision=revision,
                            artifact_refs=[
                                str(ref) for ref in item.get("artifact_refs", [])
                                if isinstance(ref, str) and ref
                            ] if isinstance(item.get("artifact_refs", []), list) else [],
                            source_task_ids=[
                                str(value) for value in item.get("source_task_ids", [])
                                if isinstance(value, str) and value
                            ] if isinstance(item.get("source_task_ids", []), list) else [],
                        )
                    )
            if isinstance(runtime_state, dict):
                checkpoint.runtime_state = {
                    str(key): str(value) for key, value in runtime_state.items()
                }
        shell = getattr(self.tools, "state_for", lambda *_args: None)("shell_session")
        shell_cwd = getattr(shell, "cwd", None)
        if shell_cwd is not None:
            checkpoint.runtime_state["shell_cwd"] = str(shell_cwd)
        else:
            checkpoint.runtime_state["shell_cwd"] = request.cwd
        checkpoint.phase = "executing"
        checkpoint.blockers = []
        return checkpoint

    def _completion_problem(self, message: str, session: SessionContext | None = None) -> str:
        unresolved = (
            [
                result
                for result in self._unresolved_results(session.tool_results)
                if result.name != "completion_gate"
            ]
            if session is not None
            else []
        )
        return self.completion_policy.completion_problem(message, session, unresolved)

    def _required_evidence(self, request: UserRequest, request_intent) -> list[str]:
        return self.completion_policy.required_evidence(request, request_intent)

    def _unmet_evidence(self, session: SessionContext) -> list[str]:
        return self.completion_policy.unmet_evidence(session)

    def _sync_checkpoint_blockers(self, session: SessionContext) -> None:
        unresolved = self._unresolved_results(session.tool_results)
        if not unresolved:
            session.checkpoint.blockers = []
            return
        placeholder = ExecutionSummary("", session.tool_results, outcome="stalled")
        session.checkpoint.blockers = self._runtime_blockers(unresolved, placeholder)

    def _runtime_failure_summary(self, error: BaseException) -> ExecutionSummary:
        session = self.current_session
        results = list(session.tool_results) if session is not None else []
        checkpoint = session.checkpoint if session is not None else TaskCheckpoint()
        message = (
            "Model API is unavailable right now. "
            f"{error}. You can keep this session open and try again later."
        )
        blocker = RuntimeBlocker(
            error_code=self._runtime_error_code(error),
            summary=str(error) or type(error).__name__,
            source="runtime",
            retryability="retryable",
            required_action="resume",
        )
        checkpoint.phase = "incomplete"
        checkpoint.blockers = [blocker]
        return ExecutionSummary(
            final_message=message,
            tool_results=results,
            outcome="runtime_error",
            blockers=[blocker],
            checkpoint=checkpoint,
        )

    def _run_budget_problem(
        self,
        run_started: float,
        model_attempts: int,
        consecutive_model_timeouts: int,
    ) -> RuntimeBlocker | None:
        return self.run_budget_policy.problem(
            run_started,
            model_attempts,
            consecutive_model_timeouts,
        )

    def _budget_summary(
        self,
        session: SessionContext,
        blocker: RuntimeBlocker,
    ) -> ExecutionSummary:
        return ExecutionSummary(
            final_message=blocker.summary,
            tool_results=session.tool_results,
            outcome="exhausted",
            active_instructions=session.active_instructions,
            blockers=[blocker],
            checkpoint=session.checkpoint,
        )

    def _is_timeout_error(self, error: BaseException) -> bool:
        return self.run_budget_policy.is_timeout(error)

    def _runtime_error_code(self, error: BaseException) -> str:
        current: BaseException | None = error
        while current is not None:
            name = type(current).__name__
            if name == "ModelTimeoutError" or isinstance(current, TimeoutError):
                return "model_timeout"
            if name == "ModelConnectionError":
                return "model_connection_error"
            if name == "ModelServiceError":
                return "model_service_error"
            current = current.__cause__
        return "runtime_error"

    def _unresolved_results(self, results: list[ToolResult]) -> list[ToolResult]:
        later_successes: set[str] = set()
        unresolved: list[ToolResult] = []
        for result in reversed(results):
            if result.success:
                later_successes.add(self._result_identity(result))
                continue
            if result.error_code == "progress_required":
                continue
            if self._result_identity(result) in later_successes:
                continue
            unresolved.append(result)
        return list(reversed(unresolved))

    def _runtime_blockers(
        self,
        unresolved: list[ToolResult],
        summary: ExecutionSummary,
    ) -> list[RuntimeBlocker]:
        blockers = [
            RuntimeBlocker(
                error_code=result.error_code or "tool_failed",
                summary=self._bounded_blocker_summary(result.output),
                source="tool",
                tool=result.name,
                retryability=(
                    "non_retryable"
                    if result.error_code in self.non_retryable_error_codes
                    else "conditional"
                ),
                required_action="change_strategy",
            )
            for result in unresolved
        ]
        if blockers:
            return blockers[-10:]
        return [
            RuntimeBlocker(
                error_code=summary.outcome,
                summary=summary.final_message,
                source="runtime",
                retryability="conditional",
                required_action="resume",
            )
        ]

    def _bounded_blocker_summary(self, value: str, limit: int = 1_000) -> str:
        if len(value) <= limit:
            return value
        return f"{value[: limit - 3]}..."

    def _may_mutate_workspace(self, risk_level: str) -> bool:
        return risk_level in {"write", "execute", "test", "destructive"}

    def _terminal_outcome(self, results: list[ToolResult]) -> str:
        if not any(not result.success for result in results):
            return "completed"
        return self._blocked_outcome(results, default="stalled")

    def _aggregate_outcome(self, results: list[ToolResult]) -> str:
        """Resolve failures by later success of the same tool, not unrelated activity."""
        return self._terminal_outcome(self._unresolved_results(results))

    def _result_identity(self, result: ToolResult) -> str:
        arguments = result.metadata.get("action_arguments")
        return json.dumps(
            {"name": result.name, "arguments": arguments if isinstance(arguments, dict) else None},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _blocked_outcome(self, results: list[ToolResult], *, default: str = "stalled") -> str:
        error_codes = {result.error_code for result in results if not result.success}
        if error_codes & {
            "approval_required",
            "approval_denied",
            "blocked_by_policy",
            "blocked_by_security_policy",
            "delegated_effect_not_allowed",
            "delegated_resource_not_allowed",
            "path_outside_workspace",
            "subagent_blocked",
            "subagent_partial",
        }:
            return "blocked"
        if error_codes & {"duplicate_tool_call", "progress_required"}:
            return "stalled"
        return default

    def _delegated_scope_problem(self, action, definition, request: UserRequest) -> ToolResult | None:
        contract = request.metadata.get("delegated_task")
        if not isinstance(contract, dict) or definition is None:
            return None
        resources = contract.get("allowed_resources", ["."])
        if not isinstance(resources, list) or "." in resources:
            return None
        candidates: list[str] = []
        for key in ("path", "cwd"):
            value = action.arguments.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)
        if action.name == "patch":
            diff = action.arguments.get("diff")
            if isinstance(diff, str):
                for line in diff.splitlines():
                    if line.startswith(("+++ b/", "--- a/")):
                        candidates.append(line[6:].strip())
        if not candidates and action.name in self.non_resource_control_tools:
            return None
        if not candidates:
            return ToolResult(
                action.name,
                False,
                "delegated action does not identify a resource covered by its task contract",
                "delegated_resource_not_allowed",
            )

        root = Path(request.cwd).resolve()
        allowed_roots = [(root / str(value)).resolve() for value in resources if isinstance(value, str)]
        for candidate in candidates:
            resolved = Path(candidate)
            if not resolved.is_absolute():
                resolved = root / resolved
            resolved = resolved.resolve()
            if not any(resolved == allowed or resolved.is_relative_to(allowed) for allowed in allowed_roots):
                return ToolResult(
                    action.name,
                    False,
                    f"resource is outside the delegated task contract: {candidate}",
                    "delegated_resource_not_allowed",
                    metadata={"resource": candidate, "allowed_resources": list(resources)},
                )
        return None

    def _available_tool_definitions(self, request: UserRequest):
        definitions = self.tools.definitions()
        contract = request.metadata.get("delegated_task")
        if not isinstance(contract, dict):
            return definitions
        effects = contract.get("allowed_effects")
        if not isinstance(effects, list):
            return definitions
        allowed = {value for value in effects if isinstance(value, str)}
        return [definition for definition in definitions if definition.risk_level in allowed]

    def _delegated_effect_problem(self, definition, request: UserRequest) -> ToolResult | None:
        contract = request.metadata.get("delegated_task")
        if definition is None or not isinstance(contract, dict):
            return None
        effects = contract.get("allowed_effects")
        if not isinstance(effects, list) or definition.risk_level in effects:
            return None
        return ToolResult(
            definition.name,
            False,
            f"delegated task does not allow {definition.risk_level} effects",
            "delegated_effect_not_allowed",
        )

    def _session_key(self, request: UserRequest) -> str | None:
        session_id = request.metadata.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
        return None

    def _intent_prompt(self, request: UserRequest) -> str:
        if request.prompt.strip().casefold() not in {"继续", "continue", "go on", "接着"}:
            return request.prompt
        state = request.metadata.get("resume_state")
        previous = getattr(state, "last_user_prompt", "")
        if isinstance(state, dict):
            previous = state.get("last_user_prompt", "")
        return str(previous).strip() or request.prompt

    def prepare_session_state(
        self,
        session_key: str | None,
        active_capability_ids: Iterable[str] = (),
    ) -> None:
        """Switch runtime tool state to one session and restore its persisted capabilities."""
        self._prepare_tool_state(session_key)
        if self.capability_warehouse is not None:
            self.capability_warehouse.restore_capabilities(active_capability_ids)

    def _prepare_tool_state(self, session_key: str | None) -> None:
        reset_state = getattr(self.tools, "reset_state", None)
        if not callable(reset_state):
            return
        if session_key is None:
            reset_state()
            self._tool_state_session_key = None
            return
        if self._tool_state_session_key != session_key:
            reset_state()
            self._tool_state_session_key = session_key

    def _all_non_retryable(self, results: list[ToolResult]) -> bool:
        return all(
            not result.success and result.error_code in self.non_retryable_error_codes
            for result in results
        )

    def _has_failed_test_result(self, results: list[ToolResult]) -> bool:
        definition_for = getattr(self.tools, "definition_for", lambda _name: None)
        return any(
            not result.success
            and getattr(definition_for(result.name), "risk_level", "") == "test"
            for result in results
        )

    def _approval_remembered(self, approval_key: tuple[str, str], approved_risk_groups: set[tuple[str, str]]) -> bool:
        return approval_key[1] != "destructive" and approval_key in approved_risk_groups

    def _approval_decision(self, action, reason: str) -> bool | None:
        if self.approval_callback is None:
            return None
        approved = self.approval_callback(action, reason)
        self.logger.record(
            "safety.approval",
            {"tool": action.name, "approved": approved, "reason": reason},
        )
        return bool(approved)

    def _execute_action(
        self,
        action,
        cwd: str,
        permissions: PermissionContext,
    ) -> ToolResult:
        progress_handle = None
        result = None
        if self.progress_reporter:
            progress_handle = self.progress_reporter.tool_started(action.name)
        try:
            result = self.tools.execute(
                action,
                cwd=cwd,
                allowed_roots=permissions.workspace_roots(scopes={"run"}),
            )
            self._attach_action_metadata(result, action)
            if result.error_code == "path_outside_workspace":
                if progress_handle is not None:
                    self.progress_reporter.tool_finished(progress_handle, action, result)
                    progress_handle = None

                grant_path = self._workspace_grant_path(result)
                if grant_path is not None:
                    scope = "run"
                    approval = ToolAction(
                        name="workspace_access",
                        arguments={
                            "path": grant_path,
                            "scope": scope,
                            "requested_by": action.name,
                            "original_arguments": dict(action.arguments),
                        },
                    )
                    reason = (
                        f"tool '{action.name}' requested access outside the current workspace: {grant_path}. "
                        "Approve only if this path is part of the task."
                    )
                    approval_decision = self._approval_decision(approval, reason)
                    if approval_decision is not True:
                        ret_res = ToolResult(
                            name=action.name,
                            success=False,
                            output=(
                                "Access outside the workspace was declined by the user."
                                if approval_decision is False
                                else reason
                            ),
                            error_code=(
                                "approval_denied" if approval_decision is False else "approval_required"
                            ),
                            metadata={
                                "action_arguments": dict(action.arguments),
                                "path": grant_path,
                                "requested_by": action.name,
                            },
                        )
                        return ret_res

                    grant = permissions.grant_workspace_path(grant_path, scope=scope)
                    self.logger.record(
                        "workspace.grant",
                        {"path": grant.path, "scope": grant.scope, "requested_by": action.name},
                    )
                    if self.progress_reporter:
                        progress_handle = self.progress_reporter.tool_started(action.name)
                    retried = self.tools.execute(action, cwd=cwd, allowed_roots=permissions.workspace_roots(scopes={"run"}))
                    self._attach_action_metadata(retried, action)
                    retried.metadata["workspace_grant"] = grant.path
                    retried.metadata["workspace_grant_scope"] = grant.scope
                    result = retried
            if self.capability_warehouse is not None and result is not None:
                self.capability_warehouse.mark_used(
                    action.name,
                    success=result.success,
                    error_code=result.error_code,
                )
            return result
        finally:
            if progress_handle is not None:
                if result is None:
                    self.progress_reporter.tool_aborted(progress_handle)
                else:
                    self.progress_reporter.tool_finished(progress_handle, action, result)

    def _workspace_grant_path(self, result: ToolResult) -> str | None:
        raw_path = result.metadata.get("resolved_path") or result.metadata.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        return str(Path(raw_path).expanduser().resolve(strict=False))

    def _action_key(self, action) -> str:
        return json.dumps(
            {"name": action.name, "arguments": action.arguments},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _attach_action_metadata(self, result: ToolResult, action) -> None:
        if result.error_code == "blocked_by_security_policy":
            return
        result.metadata.setdefault("action_arguments", dict(action.arguments))

    def _record_synthetic_tool_result(self, result: ToolResult) -> None:
        event = self.logger.record(
            "tool.result",
            {
                "name": result.name,
                "success": result.success,
                "output": result.output,
                "error_code": result.error_code,
                "metadata": result.metadata,
            },
        )
        action_ref = getattr(event, "payload", {}).get("arguments_ref", {})
        if isinstance(action_ref, dict) and action_ref.get("artifact_ref"):
            result.metadata.setdefault("action_artifact_ref", action_ref["artifact_ref"])

    def _duplicate_result(self, action, previous: ToolResult, count: int) -> ToolResult:
        blocked = not previous.success or count > self.max_duplicate_skips
        return ToolResult(
            name=action.name,
            success=not blocked,
            output=(
                "duplicate tool call skipped because the same action already ran. "
                f"Use the previous result instead of retrying it: {previous.output}"
            ),
            error_code="duplicate_tool_call" if blocked else None,
            metadata={
                "duplicate": True,
                "duplicate_count": count,
                "duplicate_limit": self.max_duplicate_skips,
                "skipped": True,
                "action_arguments": dict(action.arguments),
                "previous_output": previous.output,
            },
        )

    def _progress_recovery_result(self, signal: ProgressSignal) -> ToolResult:
        return ToolResult(
            name="progress_guard",
            success=False,
            output=(
                "The last read-only tool call repeated context that is already available. "
                "Use the previous result in session history. If the user requested file changes, "
                "stop inspecting and make the next action a patch or a final answer explaining why no change is needed."
            ),
            error_code="progress_required",
            metadata={"repeated_actions": signal.repeated_actions},
        )

    def _non_retryable_failure_message(self, results: list[ToolResult]) -> str:
        reasons = []
        has_permission_issue = False
        for result in results:
            if result.error_code == "path_outside_workspace":
                has_permission_issue = True
                reasons.append("requested path is outside the current workspace")
            elif result.error_code == "approval_required":
                has_permission_issue = True
                reasons.append(f"tool '{result.name}' requires explicit approval")
            elif result.error_code == "approval_denied":
                reasons.append(f"tool '{result.name}' was declined by the user")
            elif result.error_code == "duplicate_tool_call":
                reasons.append(f"tool '{result.name}' repeated the same action without using the previous result")
            else:
                reasons.append(result.output)
        unique_reasons = list(dict.fromkeys(reasons))
        if has_permission_issue:
            prefix = "Cannot continue with the available tool permissions: "
        else:
            prefix = "Cannot continue because tool use stopped making progress: "
        return prefix + "; ".join(unique_reasons) + "."
