from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class UserRequest:
    prompt: str
    cwd: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolDefinition:
    """Model-visible tool contract used for prompting, API tool schema, and policy checks."""

    name: str
    description: str
    arguments: dict[str, str] = field(default_factory=dict)
    input_schema: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "read"


@dataclass(slots=True)
class ToolAction:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelReply:
    message: str
    actions: list[ToolAction] = field(default_factory=list)
    done: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolResult:
    """Tool execution result.

    `output` is model-visible session history. Ordinary `metadata` is structured
    runtime/log/test data and is not prompt-visible, except keys explicitly copied
    by the session layer such as `action_arguments`.
    """

    name: str
    success: bool
    output: str
    error_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .skills.model import Skill


@dataclass(slots=True)
class ExecutionSummary:
    final_message: str
    tool_results: list[ToolResult]
    active_skills: list[Skill] = field(default_factory=list)



@dataclass(slots=True)
class SessionRecord:
    session_id: str
    cwd: str
    created_at: str
    updated_at: str
    status: str
    message_count: int
    preview: str


@dataclass(slots=True)
class StoredSession:
    session_id: str
    cwd: str
    created_at: str
    updated_at: str
    status: str
    messages: list[dict[str, str]] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    active_skills: list[str] = field(default_factory=list)

