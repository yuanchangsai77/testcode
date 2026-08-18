from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
import threading
import time
from typing import Any


class TUIEventKind(StrEnum):
    RUN_STARTED = "run.started"
    RUN_CANCELLING = "run.cancelling"
    RUN_FINISHED = "run.finished"
    RUN_FAILED = "run.failed"
    MODEL_STARTED = "model.started"
    MODEL_RETRYING = "model.retrying"
    MODEL_STREAM_DELTA = "model.stream_delta"
    MODEL_FINISHED = "model.finished"
    TOOL_STARTED = "tool.started"
    TOOL_FINISHED = "tool.finished"
    TOOL_ABORTED = "tool.aborted"
    TOOL_SKIPPED = "tool.skipped"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_SELECTED = "approval.selected"
    APPROVAL_RESOLVED = "approval.resolved"
    RESIZED = "terminal.resized"
    TICK = "timer.tick"


@dataclass(frozen=True, slots=True)
class TUIEvent:
    kind: TUIEventKind
    entity_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)

    @property
    def coalesce_key(self) -> tuple[TUIEventKind, str | None] | None:
        if self.kind in {
            TUIEventKind.TICK,
            TUIEventKind.RESIZED,
            TUIEventKind.MODEL_RETRYING,
            TUIEventKind.MODEL_STREAM_DELTA,
        }:
            return self.kind, self.entity_id
        return None


class TUIEventQueue:
    """Bounded queue that coalesces refresh-only events without dropping outcomes."""

    def __init__(self, max_events: int = 256) -> None:
        self.max_events = max(8, max_events)
        self._events: deque[TUIEvent] = deque()
        self._condition = threading.Condition()

    def publish(self, event: TUIEvent) -> None:
        with self._condition:
            key = event.coalesce_key
            if key is not None:
                for index in range(len(self._events) - 1, -1, -1):
                    if self._events[index].coalesce_key == key:
                        self._events[index] = event
                        return

            while len(self._events) >= self.max_events:
                removable = next(
                    (index for index, queued in enumerate(self._events) if queued.coalesce_key is not None),
                    None,
                )
                if removable is not None:
                    del self._events[removable]
                    break
                else:
                    # Apply backpressure rather than dropping lifecycle,
                    # approval, completion, or error events.
                    self._condition.wait()
            self._events.append(event)

    def drain(self) -> list[TUIEvent]:
        with self._condition:
            events = list(self._events)
            self._events.clear()
            self._condition.notify_all()
            return events

    def __len__(self) -> int:
        with self._condition:
            return len(self._events)
