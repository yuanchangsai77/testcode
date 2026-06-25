from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SkillMetadata:
    name: str
    description: str
    triggers: list[str]
    version: str
    path: str  # Path to the SKILL.md file


@dataclass(slots=True)
class Skill:
    metadata: SkillMetadata
    content: str  # Markdown text instructions under the frontmatter
    matched_trigger: str | None = None
