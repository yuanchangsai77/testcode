from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ..intent import RequestIntentClassifier
from ..model.types import ModelRetryableError
from ..types import ExecutionSummary, ToolAction, ToolResult, UserRequest
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


class ExecutionEngine:
    """Coordinates the model-think and tool-execute loop."""

    max_model_retries = 7
    model_retry_delays = (0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0)

    non_retryable_error_codes = {
        "approval_denied",
        "approval_required",
        "blocked_by_security_policy",
        "blocked_by_policy",
        "duplicate_tool_call",
        "invalid_argument_type",
        "invalid_argument_value",
        "missing_argument",
        "path_outside_workspace",
        "path_not_found",
        "subagent_blocked",
        "subagent_failed",
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
        self.mcp_server_count = max(0, int(mcp_server_count))
        self.intent_classifier = intent_classifier or RequestIntentClassifier()
        self.progress_policy = progress_policy or DefaultProgressPolicy()
        self.max_duplicate_skips = 3
        self._tool_state_session_key: str | None = None
        self._keep_tool_state = False
        self._runtime_cancelled = False
        self._cancel_event = threading.Event()

    def execute(self, request: UserRequest) -> ExecutionSummary:
        self._runtime_cancelled = False
        self._cancel_event.clear()
        try:
            return self._execute(request)
        except KeyboardInterrupt:
            self.cancel_current_run()
            raise
        except Exception:
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
        session_key = self._session_key(request)
        self._prepare_tool_state(session_key)
        attach_state = getattr(self.tools, "attach_state", None)
        if callable(attach_state):
            attach_state("active_session_id", session_key)
        self._keep_tool_state = session_key is not None
        permissions = PermissionContext()
        if self.capability_warehouse is not None:
            self.capability_warehouse.restore_skills(
                request.metadata.get("active_skills", [])
            )
            self.capability_warehouse.restore_capabilities(
                request.metadata.get("active_capability_ids", [])
            )
        available_tools = self.tools.definitions()
        provider_statuses = getattr(self.tools, "provider_statuses", lambda: [])()
        session = SessionContext(
            request=request,
            available_tools=available_tools,
            external_tool_statuses=provider_statuses,
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
        request_intent = self.intent_classifier.classify(request.prompt, request.metadata)

        for turn in range(1, self.max_turns + 1):
            self._raise_if_cancelled()
            session.available_tools = self.tools.definitions()
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
            try:
                retry_count = 0
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
                        reply = self.model.respond(session)
                        self._raise_if_cancelled()
                        break
                    except (ModelRetryableError, TimeoutError) as error:
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
                if progress_handle is not None:
                    self.progress_reporter.model_finished(progress_handle)
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

            if reply.done and not reply.actions:
                return self._finish(
                    ExecutionSummary(
                        final_message=reply.message,
                        tool_results=session.tool_results,
                        active_skills=session.active_skills,
                    )
                )

            turn_results: list[ToolResult] = []
            for action in reply.actions:
                self._raise_if_cancelled()
                if action.name not in visible_tool_names:
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
                        error_code="blocked_by_policy",
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
                return self._finish(
                    ExecutionSummary(
                        final_message=reply.message,
                        tool_results=session.tool_results,
                        outcome=self._terminal_outcome(turn_results),
                        active_skills=session.active_skills,
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
                            active_skills=session.active_skills,
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
                            active_skills=session.active_skills,
                        )
                    )
            else:
                consecutive_non_retryable_turns = 0

        return self._finish(
            ExecutionSummary(
                final_message="Model exceeded the maximum number of turns without producing a final answer.",
                tool_results=session.tool_results,
                outcome="exhausted",
                active_skills=session.active_skills,
            )
        )

    def _raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise KeyboardInterrupt

    def _finish(self, summary: ExecutionSummary) -> ExecutionSummary:
        if summary.outcome == "completed" and summary.tool_results:
            summary.outcome = self._terminal_outcome([summary.tool_results[-1]])
        self.current_session = None
        if self._keep_tool_state and self.capability_warehouse is not None:
            summary.active_skills = self.capability_warehouse.persisted_skills()
            summary.active_capability_ids = self.capability_warehouse.persisted_capability_ids()
            self.capability_warehouse.release_scopes({"turn", "run"})
        if not self._keep_tool_state:
            close_state = getattr(self.tools, "reset_state", None)
            if callable(close_state):
                close_state()
        return summary

    def _may_mutate_workspace(self, risk_level: str) -> bool:
        return risk_level in {"write", "execute", "test", "destructive"}

    def _terminal_outcome(self, results: list[ToolResult]) -> str:
        if not any(not result.success for result in results):
            return "completed"
        return self._blocked_outcome(results, default="stalled")

    def _blocked_outcome(self, results: list[ToolResult], *, default: str = "stalled") -> str:
        error_codes = {result.error_code for result in results if not result.success}
        if error_codes & {
            "approval_required",
            "approval_denied",
            "blocked_by_policy",
            "blocked_by_security_policy",
            "path_outside_workspace",
            "subagent_blocked",
        }:
            return "blocked"
        if error_codes & {"duplicate_tool_call", "progress_required"}:
            return "stalled"
        return default

    def _session_key(self, request: UserRequest) -> str | None:
        session_id = request.metadata.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
        return None

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
        return any(result.name == "run_tests" and not result.success for result in results)

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
        self.logger.record(
            "tool.result",
            {
                "name": result.name,
                "success": result.success,
                "output": result.output,
                "error_code": result.error_code,
                "metadata": result.metadata,
            },
        )

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
