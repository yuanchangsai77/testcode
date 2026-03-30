from __future__ import annotations

from ..types import ToolAction


class Guardrails:
    def __init__(self, policy, logger) -> None:
        self.policy = policy
        self.logger = logger

    def check(self, action: ToolAction):
        decision = self.policy.evaluate(action)
        self.logger.record(
            "safety.check",
            {"tool": action.name, "allowed": decision.allowed, "reason": decision.reason},
        )
        return decision
