from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContentMutation:
    path: str
    added_text: str
    source: str
    line: int | None = None


@dataclass(frozen=True, slots=True)
class SafetyFinding:
    policy_id: str
    category: str
    path: str
    line: int | None = None
