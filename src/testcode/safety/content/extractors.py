from __future__ import annotations

import re
from typing import Protocol

from ...types import ToolAction, ToolDefinition
from .models import ContentMutation

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<line>\d+)(?:,\d+)? @@")


class MutationExtractor(Protocol):
    def supports(self, action: ToolAction, definition: ToolDefinition) -> bool:
        ...

    def extract(self, action: ToolAction) -> list[ContentMutation]:
        ...


class PatchMutationExtractor:
    def supports(self, action: ToolAction, definition: ToolDefinition) -> bool:
        return action.name == "patch" and definition.risk_level == "write"

    def extract(self, action: ToolAction) -> list[ContentMutation]:
        diff = action.arguments.get("diff")
        if not isinstance(diff, str):
            return []

        path = ""
        new_line: int | None = None
        mutations: list[ContentMutation] = []
        block_lines: list[str] = []
        block_start: int | None = None

        def flush_block() -> None:
            nonlocal block_lines, block_start
            if path and path != "/dev/null" and block_lines:
                mutations.append(
                    ContentMutation(
                        path=path,
                        added_text="\n".join(block_lines),
                        source=action.name,
                        line=block_start,
                    )
                )
            block_lines = []
            block_start = None

        for raw_line in diff.splitlines():
            if raw_line.startswith("+++ "):
                flush_block()
                path = self._normalize_path(raw_line[4:].strip())
                continue

            hunk = HUNK_RE.match(raw_line)
            if hunk is not None:
                flush_block()
                new_line = int(hunk.group("line"))
                continue

            if not path or path == "/dev/null" or new_line is None:
                continue
            if raw_line.startswith("+"):
                if block_start is None:
                    block_start = new_line
                block_lines.append(raw_line[1:])
                new_line += 1
            elif raw_line.startswith("-"):
                flush_block()
                continue
            elif raw_line.startswith("\\"):
                continue
            else:
                flush_block()
                new_line += 1
        flush_block()
        return mutations

    def _normalize_path(self, path: str) -> str:
        return path[2:] if path.startswith("b/") else path


class FullContentMutationExtractor:
    def supports(self, action: ToolAction, definition: ToolDefinition) -> bool:
        return action.name == "apply_change" and definition.risk_level == "write"

    def extract(self, action: ToolAction) -> list[ContentMutation]:
        content = action.arguments.get("content")
        path = action.arguments.get("path")
        if not isinstance(content, str) or not isinstance(path, str):
            return []
        return [
            ContentMutation(
                path=path,
                added_text=content,
                source=action.name,
                line=1,
            )
        ]


class ShellCommandMutationExtractor:
    """Scan literal shell command text as a best-effort supplemental guard."""

    def supports(self, action: ToolAction, definition: ToolDefinition) -> bool:
        return action.name == "shell_exec" and definition.risk_level == "execute"

    def extract(self, action: ToolAction) -> list[ContentMutation]:
        command = action.arguments.get("command")
        if not isinstance(command, str):
            return []
        return [
            ContentMutation(
                path="<shell command>",
                added_text=command,
                source=action.name,
                line=1,
            )
        ]
