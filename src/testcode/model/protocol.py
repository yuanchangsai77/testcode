from __future__ import annotations

from typing import Protocol

from ..orchestration.session import SessionContext
from ..types import ModelReply


class ModelClient(Protocol):
    def respond(self, session: SessionContext) -> ModelReply:
        """Return the next model reply for the current session."""
