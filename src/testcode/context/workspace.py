from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..intent import RequestIntentClassifier
from ..orchestration.ext import ContextLoader
from ..orchestration.session import SessionContext
from ..project import ProjectDetector, ProjectProfile
from ..types import UserRequest

IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".testcode",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
}
MAX_TREE_ENTRIES = 80
MAX_TREE_DEPTH = 2


ProjectSignal = ProjectProfile


@dataclass(slots=True)
class GitSummary:
    branch: str | None = None
    status: str | None = None
    recent_commit: str | None = None


@dataclass(slots=True)
class WorkspaceSummary:
    root: str
    project_signals: list[ProjectSignal] = field(default_factory=list)
    git: GitSummary | None = None
    tree: list[str] = field(default_factory=list)
    tree_truncated: bool = False


class WorkspaceSummaryLoader(ContextLoader):
    """Collect bounded project, git, and directory context before model execution."""

    def __init__(
        self,
        logger=None,
        max_tree_entries: int = MAX_TREE_ENTRIES,
        max_tree_depth: int = MAX_TREE_DEPTH,
        intent_classifier: RequestIntentClassifier | None = None,
        project_detector: ProjectDetector | None = None,
    ) -> None:
        self.logger = logger
        self.max_tree_entries = max(1, int(max_tree_entries))
        self.max_tree_depth = max(1, int(max_tree_depth))
        self.intent_classifier = intent_classifier or RequestIntentClassifier()
        self.project_detector = project_detector or ProjectDetector()

    def load_context(self, request: UserRequest, session: SessionContext) -> None:
        if not self._is_workspace_request(request):
            session.workspace_summary = None
            if self.logger is not None:
                self.logger.record(
                    "context.workspace_summary.skipped",
                    {"reason": "non_project_request"},
                )
            return

        root = Path(request.cwd).expanduser().resolve()
        summary = WorkspaceSummary(
            root=str(root),
            project_signals=self._project_signals(root),
            git=self._git_summary(root),
        )
        summary.tree, summary.tree_truncated = self._tree(root)
        session.workspace_summary = summary
        if self.logger is not None:
            self.logger.record(
                "context.workspace_summary",
                {
                    "root": summary.root,
                    "project_markers": [signal.marker for signal in summary.project_signals],
                    "git_branch": summary.git.branch if summary.git else None,
                    "tree_entries": len(summary.tree),
                    "tree_truncated": summary.tree_truncated,
                },
            )

    def _is_workspace_request(self, request: UserRequest) -> bool:
        return self.intent_classifier.classify(
            request.prompt,
            request.metadata,
        ).project_request

    def _project_signals(self, root: Path) -> list[ProjectSignal]:
        return self.project_detector.detect(root)

    def _git_summary(self, root: Path) -> GitSummary | None:
        branch = self._git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
        status = self._git(root, ["status", "--short"])
        recent = self._git(root, ["log", "-1", "--pretty=%h %s"])
        if branch is None and status is None and recent is None:
            return None
        return GitSummary(
            branch=branch or None,
            status=status if status else "clean",
            recent_commit=recent or None,
        )

    def _git(self, root: Path, args: list[str]) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=root,
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

    def _tree(self, root: Path) -> tuple[list[str], bool]:
        entries: list[str] = []
        truncated = False

        def walk(directory: Path, depth: int) -> None:
            nonlocal truncated
            if truncated or depth > self.max_tree_depth:
                return
            try:
                children = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
            except OSError:
                return
            for child in children:
                if child.name in IGNORED_DIRS:
                    continue
                if len(entries) >= self.max_tree_entries:
                    truncated = True
                    return
                rel = child.relative_to(root)
                suffix = "/" if child.is_dir() else ""
                entries.append(f"{rel}{suffix}")
                if child.is_dir():
                    walk(child, depth + 1)

        walk(root, 1)
        return entries, truncated
