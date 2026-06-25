from __future__ import annotations

from typing import TYPE_CHECKING
from ..orchestration.ext import ContextLoader
from .registry import SkillRegistry

if TYPE_CHECKING:
    from ..types import UserRequest
    from ..orchestration.session import SessionContext


class SkillContextLoader(ContextLoader):
    """Extension hook to match and load active skills before ExecutionEngine starts."""

    def __init__(self, registry: SkillRegistry, logger) -> None:
        self.registry = registry
        self.logger = logger

    def load_context(self, request: UserRequest, session: SessionContext) -> None:
        # Scan metadata to ensure we have the latest skills
        from pathlib import Path
        project_dir = Path(request.cwd) / ".testcode/skills"
        self.registry.scan_metadata(project_dir=project_dir)

        # Get existing skills from request.metadata
        existing_names = request.metadata.get("active_skills", [])
        existing_skills = []
        for name in existing_names:
            skill = self.registry.get_skill(name)
            if skill:
                existing_skills.append(skill)

        # Match current prompt against the registry
        matched = self.registry.match_skills(request.prompt)

        # Log matched skills if there are any
        if matched and self.logger is not None:
            matched_payload = []
            for s in matched:
                matched_payload.append({
                    "name": s.metadata.name,
                    "version": s.metadata.version,
                    "matched_trigger": s.matched_trigger,
                })
            self.logger.record("skills.matched", {"matched_skills": matched_payload})

        # Merge: keep unique by name, prioritizing newly matched
        merged_skills = list(existing_skills)
        existing_set = {s.metadata.name for s in merged_skills}
        for s in matched:
            if s.metadata.name not in existing_set:
                merged_skills.append(s)
                existing_set.add(s.metadata.name)

        session.active_skills = merged_skills
