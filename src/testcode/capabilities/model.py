from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..skills.model import Skill
    from ..tools.base import Tool


@dataclass(frozen=True, slots=True)
class CapabilityEntry:
    id: str
    name: str
    kind: str
    source: str
    description: str
    tags: tuple[str, ...] = ()
    configured: bool = True
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ManifestItem:
    id: str
    toolbox_id: str
    name: str
    kind: str
    description: str
    risk_level: str = "read"
    parameter_names: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    toolbox_id: str
    name: str
    source: str
    state: str
    items: tuple[ManifestItem, ...] = ()
    origin: str = "live"
    refreshed_at: float = 0.0
    error_code: str | None = None
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ActivatedCapability:
    id: str
    toolbox_id: str
    kind: str
    tool: Tool | None = None
    skill: Skill | None = None


@dataclass(slots=True)
class ActivationRecord:
    capability_id: str
    toolbox_id: str
    kind: str
    scope: str
    reason: str
    activated_at: float
    last_used_at: float
    schema_chars: int = 0
    tool_name: str = ""
    skill_name: str = ""
    state: str = "activated"
    use_count: int = 0
    last_success: bool | None = None
    last_error_code: str | None = None
