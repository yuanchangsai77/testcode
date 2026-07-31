from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProjectProfile:
    language: str
    marker: str
    test_commands: list[str] = field(default_factory=list)
    root: str = ""
    source_layout: str | None = None
    virtual_environment: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedTestCommand:
    command: str
    project_root: str
    command_source: str
    environment_source: str
