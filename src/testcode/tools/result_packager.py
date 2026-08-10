from __future__ import annotations

import hashlib

from ..types import ToolResult


TRUNCATION_MARKER = "\n...truncated by tool result budget..."


class ToolResultPackager:
    """Apply the runtime-wide model-visible output budget to tool results."""

    def __init__(self, max_output_bytes: int) -> None:
        self.max_output_bytes = max(1, int(max_output_bytes))

    def package(self, result: ToolResult) -> ToolResult:
        original = result.output if isinstance(result.output, str) else str(result.output)
        encoded = original.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        clipped = len(encoded) > self.max_output_bytes
        visible = self._clip(encoded) if clipped else original
        visible_bytes = len(visible.encode("utf-8"))

        result.output = visible
        result.metadata = dict(result.metadata)
        result.metadata["output_original_bytes"] = len(encoded)
        result.metadata["output_visible_bytes"] = visible_bytes
        result.metadata["output_sha256"] = digest
        result.metadata["truncated"] = bool(result.metadata.get("truncated")) or clipped
        return result

    def _clip(self, encoded: bytes) -> str:
        marker = TRUNCATION_MARKER.encode("utf-8")
        if self.max_output_bytes <= len(marker):
            return marker[: self.max_output_bytes].decode("utf-8", errors="ignore")
        prefix = encoded[: self.max_output_bytes - len(marker)].decode(
            "utf-8", errors="ignore"
        )
        return prefix + TRUNCATION_MARKER
