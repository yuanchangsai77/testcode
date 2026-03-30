from __future__ import annotations

from dataclasses import dataclass

from ..types import ToolAction


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    reason: str


class DefaultPolicy:
    """Static placeholder policy for the initial scaffold."""

    blocked_tools = {"shell_exec", "delete_path"}

    def evaluate(self, action: ToolAction) -> PolicyDecision:
        if action.name in self.blocked_tools:
            return PolicyDecision(allowed=False, reason=f"tool '{action.name}' requires explicit approval")
        return PolicyDecision(allowed=True, reason="allowed")
