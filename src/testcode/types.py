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


@dataclass(slots=True)
class ResourceDescriptor:
    id: str
    name: str
    description: str = ""
    source: str = ""
    mime_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ResourceContent:
    id: str
    text: str
    mime_type: str = "text/plain"
    metadata: dict[str, Any] = field(default_factory=dict)


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .capabilities.model import InstructionContent


@dataclass(slots=True)
class ExecutionSummary:
    final_message: str
    tool_results: list[ToolResult]
    active_instructions: list[InstructionContent] = field(default_factory=list)
    active_capability_ids: list[str] = field(default_factory=list)
    outcome: str = "completed"



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
class SessionTurnTrace:
    turn: int
    message: str
    actions: list[str] = field(default_factory=list)
    tool_results: list[str] = field(default_factory=list)
    action_details: list[str] = field(default_factory=list)
    tool_result_details: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SessionRunTrace:
    run_id: str
    started_at: str
    completed_at: str
    prompt: str
    final_message: str
    outcome: str
    event_count: int
    turn_count: int
    tool_names: list[str] = field(default_factory=list)
    turns: list[SessionTurnTrace] = field(default_factory=list)


@dataclass(slots=True)
class SessionResumeState:
    last_run_id: str = ""
    last_user_prompt: str = ""
    last_assistant_message: str = ""
    last_outcome: str = ""
    last_tool_names: list[str] = field(default_factory=list)
    open_issue: str = ""
    recovery_hint: str = ""


@dataclass(slots=True)
class StoredSession:
    session_id: str
    cwd: str
    created_at: str
    updated_at: str
    status: str
    messages: list[dict[str, str]] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    active_capability_ids: list[str] = field(default_factory=list)
    trace: list[SessionRunTrace] = field(default_factory=list)
    resume_state: SessionResumeState = field(default_factory=SessionResumeState)
    parent_session_id: str = ""
    cluster_id: str = ""
    session_role: str = "primary"
    launch_source: str = "direct"
    session_image_id: str = ""
    revision: int = 0
