from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
import time

from .tui_events import TUIEvent, TUIEventKind


class RunStatus(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    CANCELLING = "cancelling"


class ToolStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ToolView:
    tool_id: str
    name: str
    status: ToolStatus = ToolStatus.RUNNING
    summary: str = "Executing"
    started_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True, slots=True)
class ApprovalView:
    approval_id: str
    action_name: str
    reason: str
    arguments: str = ""
    selected: int = 0


@dataclass(frozen=True, slots=True)
class TUIState:
    model_name: str = "StubModel"
    cwd: str = ""
    run_status: RunStatus = RunStatus.IDLE
    request_summary: str = ""
    run_started_at: float | None = None
    model_status: str = ""
    model_stream_message: str = ""
    model_stream_thinking: str = ""
    model_stream_needs_separator: bool = False
    tools: tuple[ToolView, ...] = ()
    approval: ApprovalView | None = None
    terminal_width: int = 80
    terminal_height: int = 24
    revision: int = 0

    def elapsed(self, now: float | None = None) -> float:
        if self.run_started_at is None:
            return 0.0
        return max(0.0, (time.monotonic() if now is None else now) - self.run_started_at)


def reduce_tui_state(state: TUIState, event: TUIEvent) -> TUIState:
    next_state = state
    payload = event.payload

    if event.kind == TUIEventKind.RUN_STARTED:
        next_state = replace(
            state,
            model_name=str(payload.get("model_name", state.model_name)),
            cwd=str(payload.get("cwd", state.cwd)),
            run_status=RunStatus.WORKING,
            request_summary=str(payload.get("prompt", "")),
            run_started_at=event.created_at,
            model_status="",
            model_stream_message="",
            model_stream_thinking="",
            model_stream_needs_separator=False,
            tools=(),
            approval=None,
        )
    elif event.kind == TUIEventKind.RUN_CANCELLING:
        next_state = replace(state, run_status=RunStatus.CANCELLING)
    elif event.kind in {TUIEventKind.RUN_FINISHED, TUIEventKind.RUN_FAILED}:
        next_state = replace(
            state,
            run_status=RunStatus.IDLE,
            model_status="",
            model_stream_message="",
            model_stream_thinking="",
            model_stream_needs_separator=False,
            approval=None,
        )
    elif event.kind == TUIEventKind.MODEL_STARTED:
        msg = str(payload.get("message", "Model is thinking…"))
        next_state = replace(
            state,
            model_status=msg,
            model_stream_message="",
            model_stream_thinking="",
            model_stream_needs_separator=bool(payload.get("needs_separator", False)),
        )

    elif event.kind == TUIEventKind.MODEL_RETRYING:
        next_state = replace(
            state,
            model_status=str(payload.get("message", "Retrying…")),
            model_stream_message="",
            model_stream_thinking="",
        )
    elif event.kind == TUIEventKind.MODEL_STREAM_DELTA:
        next_state = replace(
            state,
            model_stream_message=str(payload.get("message", state.model_stream_message)),
            model_stream_thinking=str(payload.get("thinking", state.model_stream_thinking)),
            model_status="Receiving model stream…",
        )
    elif event.kind == TUIEventKind.MODEL_FINISHED:
        next_state = replace(
            state,
            model_status="",
            model_stream_message="",
            model_stream_thinking="",
            model_stream_needs_separator=False,
        )
    elif event.kind == TUIEventKind.TOOL_STARTED:
        tool = ToolView(tool_id=event.entity_id or "", name=str(payload.get("name", "tool")))
        next_state = replace(state, tools=state.tools + (tool,))
    elif event.kind in {
        TUIEventKind.TOOL_FINISHED,
        TUIEventKind.TOOL_ABORTED,
        TUIEventKind.TOOL_SKIPPED,
    }:
        status = {
            TUIEventKind.TOOL_ABORTED: ToolStatus.ABORTED,
            TUIEventKind.TOOL_SKIPPED: ToolStatus.SKIPPED,
        }.get(event.kind)
        if status is None:
            status = ToolStatus.SUCCEEDED if payload.get("success") else ToolStatus.FAILED
        summary = str(payload.get("summary", status.value))
        tools = tuple(
            replace(tool, status=status, summary=summary)
            if tool.tool_id == event.entity_id
            else tool
            for tool in state.tools
        )
        if event.kind == TUIEventKind.TOOL_SKIPPED and not any(
            tool.tool_id == event.entity_id for tool in state.tools
        ):
            tools += (
                ToolView(
                    tool_id=event.entity_id or "",
                    name=str(payload.get("name", "tool")),
                    status=status,
                    summary=summary,
                ),
            )
        next_state = replace(state, tools=tools)
    elif event.kind == TUIEventKind.APPROVAL_REQUESTED:
        next_state = replace(
            state,
            approval=ApprovalView(
                approval_id=event.entity_id or "",
                action_name=str(payload.get("action_name", "tool")),
                reason=str(payload.get("reason", "")),
                arguments=str(payload.get("arguments", "")),
            ),
        )
    elif event.kind == TUIEventKind.APPROVAL_SELECTED and state.approval is not None:
        next_state = replace(
            state,
            approval=replace(state.approval, selected=int(payload.get("selected", 0)) % 2),
        )
    elif event.kind == TUIEventKind.APPROVAL_RESOLVED:
        next_state = replace(state, approval=None)
    elif event.kind == TUIEventKind.RESIZED:
        next_state = replace(
            state,
            terminal_width=max(1, int(payload.get("width", state.terminal_width))),
            terminal_height=max(1, int(payload.get("height", state.terminal_height))),
        )

    if next_state is state:
        return state
    return replace(next_state, revision=state.revision + 1)
