from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from ..types import ToolAction, ToolDefinition


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    requires_confirmation: bool
    reason: str
    risk_level: str
    error_code: str = ""


class DefaultPolicy:
    """Mode-based safety policy for tool execution."""

    valid_modes = {"readonly", "confirm", "auto"}
    read_risks = {"read"}
    confirm_risks = {"write", "execute", "test", "network", "destructive", "confirm"}
    auto_allowed_risks = {"read", "write"}
    auto_confirm_risks = {"execute", "test", "network", "destructive", "confirm"}

    def __init__(self, mode: str = "confirm", *, allowed_effects: set[str] | None = None) -> None:
        if mode not in self.valid_modes:
            raise ValueError(f"unknown safety mode: {mode}")
        self.mode = mode
        self.allowed_effects = frozenset(allowed_effects) if allowed_effects is not None else None

    def evaluate(self, action: ToolAction, definition: ToolDefinition | None = None) -> PolicyDecision:
        risk_level = self._effective_risk(action, definition)

        if self.allowed_effects is not None and risk_level not in self.allowed_effects:
            return PolicyDecision(
                False,
                False,
                f"tool '{action.name}' has effect '{risk_level}' outside the delegated task contract",
                risk_level,
                "delegated_effect_not_allowed",
            )

        if self.mode == "readonly":
            if risk_level in self.read_risks:
                return PolicyDecision(True, False, "allowed", risk_level)
            return PolicyDecision(
                False,
                False,
                f"tool '{action.name}' has risk '{risk_level}' and is blocked in readonly mode",
                risk_level,
            )

        if self.mode == "confirm":
            if risk_level in self.read_risks:
                return PolicyDecision(True, False, "allowed", risk_level)
            if risk_level in self.confirm_risks:
                return PolicyDecision(
                    False,
                    True,
                    f"tool '{action.name}' has risk '{risk_level}' and requires explicit approval",
                    risk_level,
                )
            return PolicyDecision(False, False, f"tool '{action.name}' has unknown risk '{risk_level}'", risk_level)

        if risk_level in self.auto_allowed_risks:
            return PolicyDecision(True, False, "allowed", risk_level)
        if risk_level in self.auto_confirm_risks:
            return PolicyDecision(
                False,
                True,
                f"tool '{action.name}' has risk '{risk_level}' and requires explicit approval",
                risk_level,
            )
        return PolicyDecision(False, False, f"tool '{action.name}' has unknown risk '{risk_level}'", risk_level)

    def _effective_risk(self, action: ToolAction, definition: ToolDefinition | None) -> str:
        risk_level = definition.risk_level if definition is not None else "read"
        if action.name == "shell_exec" and self._is_destructive_shell_command(action.arguments.get("command")):
            return "destructive"
        return risk_level

    def _is_destructive_shell_command(self, command: object) -> bool:
        if not isinstance(command, str) or not command.strip():
            return False

        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()

        if self._matches_rm_rf(tokens):
            return True
        if self._matches_git_reset_hard(tokens):
            return True
        if self._matches_git_clean_force_dirs(tokens):
            return True
        return self._redirects_to_sensitive_path(command)

    def _matches_rm_rf(self, tokens: list[str]) -> bool:
        if not tokens or tokens[0] != "rm":
            return False
        for token in tokens[1:]:
            if token.startswith("-") and "r" in token and "f" in token:
                return True
        return False

    def _matches_git_reset_hard(self, tokens: list[str]) -> bool:
        return len(tokens) >= 3 and tokens[0] == "git" and tokens[1] == "reset" and "--hard" in tokens[2:]

    def _matches_git_clean_force_dirs(self, tokens: list[str]) -> bool:
        if len(tokens) < 3 or tokens[0] != "git" or tokens[1] != "clean":
            return False
        has_force = False
        has_dirs = False
        for token in tokens[2:]:
            if not token.startswith("-"):
                continue
            has_force = has_force or "f" in token
            has_dirs = has_dirs or "d" in token
        return has_force and has_dirs

    def _redirects_to_sensitive_path(self, command: str) -> bool:
        return re.search(r"(?:^|\s)(?:>|>>|1>|2>)\s*(?:/etc/|/usr/|/bin/|/sbin/|/var/)", command) is not None
