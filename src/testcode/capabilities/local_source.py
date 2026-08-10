from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..skills.registry import SkillRegistry
from ..tools.base import Tool
from ..tools.subagents import build_subagent_tools
from .model import (
    ActivatedCapability,
    CapabilityEntry,
    CapabilityManifest,
    InstructionContent,
    ManifestItem,
)


@dataclass(frozen=True, slots=True)
class LocalInstructionCapability:
    id: str
    name: str
    description: str
    loader: Callable[[], InstructionContent]
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LocalToolCapability:
    id: str
    tool: Tool


@dataclass(frozen=True, slots=True)
class LocalToolboxSpec:
    id: str
    name: str
    source: str
    description: str
    tags: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)
    instructions: tuple[LocalInstructionCapability, ...] = ()
    tools: tuple[LocalToolCapability, ...] = ()


@dataclass(slots=True)
class LocalToolboxSource:
    specs: tuple[LocalToolboxSpec, ...]
    source_name: str = "local"

    def catalog_entries(self) -> list[CapabilityEntry]:
        return [
            CapabilityEntry(
                id=spec.id,
                name=spec.name,
                kind="toolbox",
                source=spec.source,
                description=spec.description,
                tags=spec.tags,
                metadata=dict(spec.metadata),
            )
            for spec in self.specs
        ]

    def owns_toolbox(self, toolbox_id: str) -> bool:
        return any(spec.id == toolbox_id for spec in self.specs)

    def open_toolbox(self, toolbox_id: str) -> CapabilityManifest:
        spec = self._spec(toolbox_id)
        items = [
            ManifestItem(
                id=item.id,
                toolbox_id=spec.id,
                name=item.name,
                kind="workflow_instructions",
                description=item.description,
                metadata=dict(item.metadata),
            )
            for item in spec.instructions
        ]
        items.extend(
            ManifestItem(
                id=item.id,
                toolbox_id=spec.id,
                name=item.tool.name,
                kind="local_tool",
                description=item.tool.description,
                risk_level=item.tool.risk_level,
                parameter_names=tuple(item.tool.arguments),
            )
            for item in spec.tools
        )
        return CapabilityManifest(
            toolbox_id=spec.id,
            name=spec.name,
            source=spec.source,
            state="ready",
            items=tuple(items),
            origin="local",
            metadata=dict(spec.metadata),
        )

    def activate(self, capability_id: str) -> ActivatedCapability:
        for spec in self.specs:
            for item in spec.instructions:
                if item.id == capability_id:
                    return ActivatedCapability(
                        id=item.id,
                        toolbox_id=spec.id,
                        kind="workflow_instructions",
                        instruction=item.loader(),
                    )
            for item in spec.tools:
                if item.id == capability_id:
                    return ActivatedCapability(
                        id=item.id,
                        toolbox_id=spec.id,
                        kind="local_tool",
                        tool=item.tool,
                    )
        raise KeyError(f"unknown local capability: {capability_id}")

    def _spec(self, toolbox_id: str) -> LocalToolboxSpec:
        spec = next((item for item in self.specs if item.id == toolbox_id), None)
        if spec is None:
            raise KeyError(f"unknown local toolbox: {toolbox_id}")
        return spec


def skill_toolbox_specs(
    registry: SkillRegistry,
    tools_by_skill: dict[str, tuple[Tool, ...]] | None = None,
) -> tuple[LocalToolboxSpec, ...]:
    assigned_tools = tools_by_skill or {}
    specs = []
    for metadata in registry.metadata_items():
        toolbox_id = f"skill:{metadata.name}"
        instruction_id = f"{toolbox_id}:instructions"

        def load_instruction(name=metadata.name, capability_id=instruction_id) -> InstructionContent:
            skill = registry.get_skill(name)
            if skill is None:
                raise KeyError(f"workflow instructions unavailable: {name}")
            return InstructionContent(
                id=capability_id,
                name=skill.metadata.name,
                content=skill.content,
                version=skill.metadata.version,
                source="skill",
                metadata={"path": skill.metadata.path},
            )

        specs.append(
            LocalToolboxSpec(
                id=toolbox_id,
                name=metadata.name,
                source="skill",
                description=metadata.description or f"Local workflow toolbox '{metadata.name}'.",
                tags=tuple(dict.fromkeys(("workflow", *metadata.triggers))),
                metadata={"version": metadata.version},
                instructions=(
                    LocalInstructionCapability(
                        id=instruction_id,
                        name="instructions",
                        description=f"Activate the workflow instructions for {metadata.name}. {metadata.description}".strip(),
                        loader=load_instruction,
                        metadata={"version": metadata.version, "triggers": list(metadata.triggers)},
                    ),
                ),
                tools=tuple(
                    LocalToolCapability(id=f"{toolbox_id}:tool:{tool.name}", tool=tool)
                    for tool in assigned_tools.get(metadata.name, ())
                ),
            )
        )
    return tuple(specs)


def subagent_toolbox_spec() -> LocalToolboxSpec:
    tools = build_subagent_tools()
    return LocalToolboxSpec(
        id="local:subagents",
        name="subagents",
        source="local",
        description="Delegate independent work to isolated child sessions and inspect their bounded handoffs.",
        tags=("subagent", "delegation", "parallel work", "child session"),
        tools=tuple(
            LocalToolCapability(id=f"local:subagents:{tool.name}", tool=tool)
            for tool in tools
        ),
    )
