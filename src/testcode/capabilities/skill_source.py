from __future__ import annotations

from dataclasses import dataclass

from ..skills.registry import SkillRegistry
from .model import ActivatedCapability, CapabilityEntry, CapabilityManifest, ManifestItem


@dataclass(slots=True)
class SkillToolboxSource:
    registry: SkillRegistry
    logger: object | None = None
    source_name: str = "skill"

    def catalog_entries(self) -> list[CapabilityEntry]:
        return [
            CapabilityEntry(
                id=f"skill:{metadata.name}",
                name=metadata.name,
                kind="toolbox",
                source="skill",
                description=metadata.description or f"Skill toolbox '{metadata.name}'.",
                tags=tuple(dict.fromkeys(("skill", *metadata.triggers))),
                metadata={"version": metadata.version},
            )
            for metadata in self.registry.metadata_items()
        ]

    def owns_toolbox(self, toolbox_id: str) -> bool:
        return toolbox_id.startswith("skill:") and any(
            toolbox_id == f"skill:{metadata.name}"
            for metadata in self.registry.metadata_items()
        )

    def open_toolbox(self, toolbox_id: str) -> CapabilityManifest:
        name = toolbox_id.split(":", 1)[1] if ":" in toolbox_id else ""
        metadata = next(
            (item for item in self.registry.metadata_items() if item.name == name),
            None,
        )
        if metadata is None:
            raise KeyError(f"unknown Skill toolbox: {toolbox_id}")
        return CapabilityManifest(
            toolbox_id=toolbox_id,
            name=metadata.name,
            source="skill",
            state="ready",
            items=(
                ManifestItem(
                    id=f"{toolbox_id}:instructions",
                    toolbox_id=toolbox_id,
                    name="instructions",
                    kind="skill_instructions",
                    description=(
                        f"Activate the instructions for {metadata.name}. "
                        f"{metadata.description}"
                    ).strip(),
                    metadata={
                        "version": metadata.version,
                        "triggers": list(metadata.triggers),
                    },
                ),
            ),
            origin="local",
            metadata={"version": metadata.version},
        )

    def activate(self, capability_id: str) -> ActivatedCapability:
        parts = capability_id.split(":")
        if len(parts) != 3 or parts[0] != "skill" or parts[2] != "instructions":
            raise KeyError(f"unknown Skill capability: {capability_id}")
        skill = self.registry.get_skill(parts[1])
        if skill is None:
            raise KeyError(f"Skill is unavailable: {parts[1]}")
        return ActivatedCapability(
            id=capability_id,
            toolbox_id=f"skill:{parts[1]}",
            kind="skill_instructions",
            skill=skill,
        )
