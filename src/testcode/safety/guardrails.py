from __future__ import annotations

from ..types import ToolAction, ToolDefinition


class Guardrails:
    def __init__(self, policy, logger) -> None:
        self.policy = policy
        self.logger = logger

    def check(self, action: ToolAction, definition: ToolDefinition | None = None):
        decision = self.policy.evaluate(action, definition)
        self.logger.record(
            "safety.check",
            {
                "tool": action.name,
                "risk_level": decision.risk_level,
                "mode": getattr(self.policy, "mode", None),
                "allowed": decision.allowed,
                "requires_confirmation": decision.requires_confirmation,
                "reason": decision.reason,
            },
        )
        return decision
