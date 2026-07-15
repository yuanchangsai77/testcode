from __future__ import annotations

from typing import Protocol
from ..types import ResourceContent, ResourceDescriptor, UserRequest
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


class ResourceProvider(Protocol):
    """Interface to provide indexed, on-demand context sources."""

    def list_resources(self) -> list[ResourceDescriptor]:
        """Return resource descriptors without loading full resource bodies."""
        ...

    def read_resource(self, resource_id: str) -> ResourceContent:
        """Load one concrete resource body for downstream filtering and packaging."""
        ...
