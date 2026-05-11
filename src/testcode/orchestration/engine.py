from __future__ import annotations

from ..types import ExecutionSummary, ToolResult, UserRequest
from .session import SessionContext


class ExecutionEngine:
    """Coordinates the model-think and tool-execute loop."""

    def __init__(self, model, tools, guardrails, logger) -> None:
        self.model = model
        self.tools = tools
        self.guardrails = guardrails
        self.logger = logger
        self.max_turns = 8

    def execute(self, request: UserRequest) -> ExecutionSummary:
        session = SessionContext(request=request, available_tools=self.tools.definitions())

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

            for action in reply.actions:
                decision = self.guardrails.check(action)
                if not decision.allowed:
                    result = ToolResult(name=action.name, success=False, output=decision.reason)
                    session.add_tool_result(result)
                    continue

                result = self.tools.execute(action)
                session.add_tool_result(result)

            if reply.done:
                return ExecutionSummary(final_message=reply.message, tool_results=session.tool_results)

        return ExecutionSummary(
            final_message="Model exceeded the maximum number of turns without producing a final answer.",
            tool_results=session.tool_results,
        )
