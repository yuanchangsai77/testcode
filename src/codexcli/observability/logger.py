from __future__ import annotations

from .events import Event


class InMemoryLogger:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def record(self, name: str, payload: dict) -> None:
        self.events.append(Event(name=name, payload=payload))
