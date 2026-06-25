from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..orchestration.ext import ContextLoader
from ..orchestration.session import SessionContext
from ..types import UserRequest

MAX_RULE_BYTES = 32_000
PROJECT_BOUNDARY_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")


@dataclass(slots=True)
class ProjectRule:
    path: str
    content: str
    truncated: bool = False


class ProjectRulesLoader(ContextLoader):
    """Load AGENTS.md files from the workspace path up to the project root."""

    def __init__(self, logger=None, max_bytes: int = MAX_RULE_BYTES) -> None:
        self.logger = logger
        self.max_bytes = max(1, int(max_bytes))

    def load_context(self, request: UserRequest, session: SessionContext) -> None:
        cwd = Path(request.cwd).expanduser().resolve()
        rules = self._load_rules(cwd)
        session.project_rules = rules
        if self.logger is not None and rules:
            self.logger.record(
                "context.project_rules",
                {
                    "files": [
                        {
                            "path": rule.path,
                            "bytes": len(rule.content.encode("utf-8")),
                            "truncated": rule.truncated,
                        }
                        for rule in rules
                    ]
                },
            )

    def _load_rules(self, cwd: Path) -> list[ProjectRule]:
        boundary = self._project_boundary(cwd)
        candidates = []
        for directory in (cwd, *cwd.parents):
            path = directory / "AGENTS.md"
            if path.is_file():
                candidates.append(path)
            if directory == boundary:
                break

        rules = []
        for path in reversed(candidates):
            content, truncated = self._read_text(path)
            if content.strip():
                rules.append(ProjectRule(path=str(path), content=content, truncated=truncated))
        return rules

    def _read_text(self, path: Path) -> tuple[str, bool]:
        data = path.read_bytes()
        truncated = len(data) > self.max_bytes
        chunk = data[: self.max_bytes]
        return chunk.decode("utf-8", errors="replace"), truncated

    def _project_boundary(self, cwd: Path) -> Path:
        for directory in (cwd, *cwd.parents):
            if any((directory / marker).exists() for marker in PROJECT_BOUNDARY_MARKERS):
                return directory
        return cwd
