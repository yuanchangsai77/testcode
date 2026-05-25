from __future__ import annotations

import json

from ..types import ExecutionSummary, ToolResult, UserRequest
from .session import SessionContext


class ExecutionEngine:
    """Coordinates the model-think and tool-execute loop."""

    non_retryable_error_codes = {"approval_required", "blocked_by_policy", "path_outside_workspace"}

    def __init__(self, model, tools, guardrails, logger, approval_callback=None) -> None:
        self.model = model
        self.tools = tools
        self.guardrails = guardrails
        self.logger = logger
        self.approval_callback = approval_callback
        self.max_turns = 100

    def execute(self, request: UserRequest) -> ExecutionSummary:
        if hasattr(self.tools, "reset_state"):
            self.tools.reset_state()
        session = SessionContext(request=request, available_tools=self.tools.definitions())
        consecutive_non_retryable_turns = 0
        consecutive_failed_test_turns = 0
        approved_risk_groups: set[tuple[str, str]] = set()
        completed_actions: dict[str, ToolResult] = {}

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
                return ExecutionSummary(final_message=reply.message, tool_results=session.tool_results)

            turn_results: list[ToolResult] = []
            for action in reply.actions:
                action_key = self._action_key(action)
                if action_key in completed_actions:
                    result = self._duplicate_result(action, completed_actions[action_key])
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
                        result = self.tools.execute(action, cwd=request.cwd)
                        self._attach_action_metadata(result, action)
                        session.add_tool_result(result)
                        turn_results.append(result)
                        if result.success:
                            if decision.risk_level == "write":
                                completed_actions.clear()
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

                result = self.tools.execute(action, cwd=request.cwd)
                self._attach_action_metadata(result, action)
                session.add_tool_result(result)
                turn_results.append(result)
                if result.success:
                    if decision.risk_level == "write":
                        completed_actions.clear()
                    completed_actions[action_key] = result

            if reply.done:
                return ExecutionSummary(final_message=reply.message, tool_results=session.tool_results)

            if self._has_failed_test_result(turn_results):
                consecutive_failed_test_turns += 1
                if consecutive_failed_test_turns >= 3:
                    return ExecutionSummary(
                        final_message="Stopping after 3 consecutive failing test runs. Review the latest test output before retrying.",
                        tool_results=session.tool_results,
                    )
            elif turn_results:
                consecutive_failed_test_turns = 0

            if turn_results and self._all_non_retryable(turn_results):
                consecutive_non_retryable_turns += 1
                if consecutive_non_retryable_turns >= 2:
                    return ExecutionSummary(
                        final_message=self._non_retryable_failure_message(turn_results),
                        tool_results=session.tool_results,
                    )
            else:
                consecutive_non_retryable_turns = 0

        return ExecutionSummary(
            final_message="Model exceeded the maximum number of turns without producing a final answer.",
            tool_results=session.tool_results,
        )

    def _all_non_retryable(self, results: list[ToolResult]) -> bool:
        return all(
            not result.success and result.error_code in self.non_retryable_error_codes
            for result in results
        )

    def _has_failed_test_result(self, results: list[ToolResult]) -> bool:
        return any(result.name == "run_tests" and not result.success for result in results)

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

    def _action_key(self, action) -> str:
        return json.dumps(
            {"name": action.name, "arguments": action.arguments},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _attach_action_metadata(self, result: ToolResult, action) -> None:
        result.metadata.setdefault("action_arguments", dict(action.arguments))

    def _duplicate_result(self, action, previous: ToolResult) -> ToolResult:
        return ToolResult(
            name=action.name,
            success=True,
            output=f"duplicate tool call skipped; previous result: {previous.output}",
            metadata={
                "duplicate": True,
                "skipped": True,
                "action_arguments": dict(action.arguments),
                "previous_output": previous.output,
            },
        )

    def _non_retryable_failure_message(self, results: list[ToolResult]) -> str:
        reasons = []
        for result in results:
            if result.error_code == "path_outside_workspace":
                reasons.append("requested path is outside the current workspace")
            elif result.error_code == "approval_required":
                reasons.append(f"tool '{result.name}' requires explicit approval")
            else:
                reasons.append(result.output)
        unique_reasons = list(dict.fromkeys(reasons))
        return "Cannot continue with the available tool permissions: " + "; ".join(unique_reasons) + "."
