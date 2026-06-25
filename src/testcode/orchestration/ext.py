from __future__ import annotations

from typing import Protocol
from ..types import UserRequest
from ..tools.base import Tool
from .session import SessionContext


class ContextLoader(Protocol):
    """Extension hook to inject custom instructions, metadata, or workspace context before execution."""

    def load_context(self, request: UserRequest, session: SessionContext) -> None:
        """Executed once at the beginning of the ExecutionEngine run."""
        ...


class ToolProvider(Protocol):
    """Interface to provide a collection of executable tools to the runtime."""

    def get_tools(self) -> list[Tool]:
        """Discovers and returns a list of tools to be registered (implementing the Tool protocol)."""
        ...
