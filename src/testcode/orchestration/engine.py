from __future__ import annotations

import json
from pathlib import Path

from ..types import ExecutionSummary, ToolAction, ToolResult, UserRequest
from .permissions import PermissionContext
from .session import SessionContext


class ExecutionEngine:
    """Coordinates the model-think and tool-execute loop."""

    non_retryable_error_codes = {
        "approval_required",
        "blocked_by_policy",
        "duplicate_tool_call",
        "missing_argument",
        "path_outside_workspace",
        "path_not_found",
        "unknown_argument",
    }

    def __init__(self, model, tools, guardrails, logger, approval_callback=None) -> None:
        self.model = model
        self.tools = tools
        self.guardrails = guardrails
        self.logger = logger
        self.approval_callback = approval_callback
        self.max_turns = 100
        self.max_duplicate_skips = 3
        self._tool_state_session_key: str | None = None
        self._keep_tool_state = False

    def execute(self, request: UserRequest) -> ExecutionSummary:
        session_key = self._session_key(request)
        self._prepare_tool_state(session_key)
        self._keep_tool_state = session_key is not None
        permissions = PermissionContext()
        session = SessionContext(request=request, available_tools=self.tools.definitions())
        consecutive_non_retryable_turns = 0
        consecutive_failed_test_turns = 0
        approved_risk_groups: set[tuple[str, str]] = set()
        completed_actions: dict[str, ToolResult] = {}
        duplicate_counts: dict[str, int] = {}
        progress_recovery_sent = False

        for turn in range(1, self.max_turns + 1):
            reply = self.model.respond(session)
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
                return self._finish(ExecutionSummary(final_message=reply.message, tool_results=session.tool_results))

            turn_results: list[ToolResult] = []
            for action in reply.actions:
                action_key = self._action_key(action)
                if action_key in completed_actions:
                    duplicate_counts[action_key] = duplicate_counts.get(action_key, 0) + 1
                    result = self._duplicate_result(action, completed_actions[action_key], duplicate_counts[action_key])
                    self._record_synthetic_tool_result(result)
                    session.add_tool_result(result)
                    turn_results.append(result)
                    continue

                definition = self.tools.definition_for(action.name)
                decision = self.guardrails.check(action, definition)
                if decision.requires_confirmation:
                    approval_key = (action.name, decision.risk_level)
                    if self._approval_remembered(approval_key, approved_risk_groups) or self._approved(action, decision.reason):
                        if decision.risk_level != "destructive":
                            approved_risk_groups.add(approval_key)
                        result = self._execute_action(action, request.cwd, permissions)
                        session.add_tool_result(result)
                        turn_results.append(result)
                        if result.success or result.error_code in self.non_retryable_error_codes:
                            if decision.risk_level == "write":
                                completed_actions.clear()
                                duplicate_counts.clear()
                            completed_actions[action_key] = result
                        continue

                    result = ToolResult(
                        name=action.name,
                        success=False,
                        output=decision.reason,
                        error_code="approval_required",
                    )
                    self._attach_action_metadata(result, action)
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
                    self._attach_action_metadata(result, action)
                    session.add_tool_result(result)
                    turn_results.append(result)
                    continue

                result = self._execute_action(action, request.cwd, permissions)
                session.add_tool_result(result)
                turn_results.append(result)
                if result.success or result.error_code in self.non_retryable_error_codes:
                    if decision.risk_level == "write":
                        completed_actions.clear()
                        duplicate_counts.clear()
                    completed_actions[action_key] = result

            if reply.done:
                return self._finish(ExecutionSummary(final_message=reply.message, tool_results=session.tool_results))

            if self._has_failed_test_result(turn_results):
                consecutive_failed_test_turns += 1
                if consecutive_failed_test_turns >= 3:
                    return self._finish(
                        ExecutionSummary(
                            final_message="Stopping after 3 consecutive failing test runs. Review the latest test output before retrying.",
                            tool_results=session.tool_results,
                        )
                    )
            elif turn_results:
                consecutive_failed_test_turns = 0

            if (
                not progress_recovery_sent
                and self._should_send_progress_recovery(request.prompt, turn_results)
            ):
                progress_result = self._progress_recovery_result(turn_results)
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
                        )
                    )
            else:
                consecutive_non_retryable_turns = 0

        return self._finish(
            ExecutionSummary(
                final_message="Model exceeded the maximum number of turns without producing a final answer.",
                tool_results=session.tool_results,
            )
        )

    def _finish(self, summary: ExecutionSummary) -> ExecutionSummary:
        if not self._keep_tool_state:
            close_state = getattr(self.tools, "reset_state", None)
            if callable(close_state):
                close_state()
        return summary

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

    def _should_send_progress_recovery(self, prompt: str, results: list[ToolResult]) -> bool:
        if not self._request_implies_file_changes(prompt):
            return False
        return any(
            result.error_code == "duplicate_tool_call" and result.name in self._read_context_tools()
            for result in results
        )

    def _request_implies_file_changes(self, prompt: str) -> bool:
        lowered = prompt.lower()
        change_words = (
            "add",
            "build",
            "change",
            "create",
            "edit",
            "fix",
            "generate",
            "implement",
            "modify",
            "patch",
            "scaffold",
            "update",
            "write",
            "修改",
            "创建",
            "生成",
            "实现",
            "新增",
            "修复",
            "升级",
        )
        return any(word in lowered for word in change_words)

    def _read_context_tools(self) -> set[str]:
        return {
            "file_info",
            "find_files",
            "git_diff",
            "git_show",
            "git_status",
            "list_dir",
            "read_file",
            "search_text",
        }

    def _approval_remembered(self, approval_key: tuple[str, str], approved_risk_groups: set[tuple[str, str]]) -> bool:
        return approval_key[1] != "destructive" and approval_key in approved_risk_groups

    def _approved(self, action, reason: str) -> bool:
        if self.approval_callback is None:
            return False
        approved = self.approval_callback(action, reason)
        self.logger.record(
            "safety.approval",
            {"tool": action.name, "approved": approved, "reason": reason},
        )
        return bool(approved)

    def _execute_action(self, action, cwd: str, permissions: PermissionContext) -> ToolResult:
        result = self.tools.execute(action, cwd=cwd, allowed_roots=permissions.workspace_roots(scopes={"run"}))
        self._attach_action_metadata(result, action)
        if result.error_code != "path_outside_workspace":
            return result

        grant_path = self._workspace_grant_path(result)
        if grant_path is None:
            return result

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
        if not self._approved(approval, reason):
            return ToolResult(
                name=action.name,
                success=False,
                output=reason,
                error_code="approval_required",
                metadata={
                    "action_arguments": dict(action.arguments),
                    "path": grant_path,
                    "requested_by": action.name,
                },
            )

        grant = permissions.grant_workspace_path(grant_path, scope=scope)
        self.logger.record(
            "workspace.grant",
            {"path": grant.path, "scope": grant.scope, "requested_by": action.name},
        )
        retried = self.tools.execute(action, cwd=cwd, allowed_roots=permissions.workspace_roots(scopes={"run"}))
        self._attach_action_metadata(retried, action)
        retried.metadata["workspace_grant"] = grant.path
        retried.metadata["workspace_grant_scope"] = grant.scope
        return retried

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

    def _progress_recovery_result(self, results: list[ToolResult]) -> ToolResult:
        repeated = [
            result.metadata.get("action_arguments", {"tool": result.name})
            for result in results
            if result.error_code == "duplicate_tool_call"
        ]
        return ToolResult(
            name="progress_guard",
            success=False,
            output=(
                "The last read-only tool call repeated context that is already available. "
                "Use the previous result in session history. If the user requested file changes, "
                "stop inspecting and make the next action a patch or a final answer explaining why no change is needed."
            ),
            error_code="progress_required",
            metadata={"repeated_actions": repeated},
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
