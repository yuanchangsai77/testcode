from __future__ import annotations

from typing import Protocol

from .model import ActivatedCapability, CapabilityEntry, CapabilityManifest


class CapabilitySource(Protocol):
    source_name: str

    def catalog_entries(self) -> list[CapabilityEntry]:
        """Return outer catalog entries without opening remote toolboxes."""

    def owns_toolbox(self, toolbox_id: str) -> bool:
        """Return whether this source owns the toolbox id."""

    def open_toolbox(self, toolbox_id: str) -> CapabilityManifest:
        """Return a bounded manifest, performing discovery only when requested."""

    def activate(self, capability_id: str) -> ActivatedCapability:
        """Build one selected leaf capability after its toolbox is open."""
