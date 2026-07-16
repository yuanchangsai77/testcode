from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..orchestration.ext import ContextLoader
from ..orchestration.session import SessionContext
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
ENGLISH_STRONG_PROJECT_REQUEST_TERMS = {
    "inspect",
    "workspace",
    "repository",
    "repo",
    "bug",
    "implement",
    "refactor",
    "git",
    "commit",
    "pull request",
}
ENGLISH_PROJECT_ACTION_TERMS = {
    "build",
    "debug",
    "edit",
    "fix",
    "review",
    "test",
    "update",
    "write",
}
ENGLISH_PROJECT_TARGET_TERMS = {
    "changes",
    "code",
    "diff",
    "directory",
    "docs",
    "document",
    "file",
    "folder",
    "project",
    "source",
    "tests",
}
CHINESE_STRONG_PROJECT_REQUEST_TERMS = {
    "工作区",
    "仓库",
}
CHINESE_PROJECT_ACTION_TERMS = {
    "编辑",
    "构建",
    "检查",
    "审查",
    "调试",
    "修复",
    "实现",
    "重构",
    "测试",
    "提交",
    "修改",
}
CHINESE_PROJECT_TARGET_TERMS = {
    "变更",
    "差异",
    "代码",
    "源码",
    "文档",
    "项目",
    "文件",
    "目录",
}
CODE_PATH_RE = re.compile(
    r"(?:^|[\s'\"`])(?:\.?\.?/)?[^\s'\"`]+\.(?:py|js|ts|tsx|jsx|go|rs|java|kt|toml|yaml|yml|json|md)(?:$|[\s'\"`])",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ProjectSignal:
    language: str
    marker: str
    test_commands: list[str] = field(default_factory=list)


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
    ) -> None:
        self.logger = logger
        self.max_tree_entries = max(1, int(max_tree_entries))
        self.max_tree_depth = max(1, int(max_tree_depth))

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
        override = request.metadata.get("include_workspace_context")
        if isinstance(override, bool):
            return override
        context_paths = request.metadata.get("context_paths", [])
        if isinstance(context_paths, list) and any(isinstance(path, str) and path for path in context_paths):
            return True

        prompt = request.prompt.casefold()
        if any(term in prompt for term in CHINESE_STRONG_PROJECT_REQUEST_TERMS):
            return True
        if any(
            re.search(rf"\b{re.escape(term)}\b", prompt)
            for term in ENGLISH_STRONG_PROJECT_REQUEST_TERMS
        ):
            return True
        has_english_action = any(
            re.search(rf"\b{re.escape(term)}\b", prompt)
            for term in ENGLISH_PROJECT_ACTION_TERMS
        )
        has_english_target = any(
            re.search(rf"\b{re.escape(term)}\b", prompt)
            for term in ENGLISH_PROJECT_TARGET_TERMS
        )
        has_chinese_action = any(term in prompt for term in CHINESE_PROJECT_ACTION_TERMS)
        has_chinese_target = any(term in prompt for term in CHINESE_PROJECT_TARGET_TERMS)
        if (has_english_action or has_chinese_action) and (
            has_english_target or has_chinese_target
        ):
            return True
        return CODE_PATH_RE.search(request.prompt) is not None

    def _project_signals(self, root: Path) -> list[ProjectSignal]:
        signals = []
        marker_map = [
            ("pyproject.toml", "Python", ["python -m pytest"]),
            ("package.json", "Node.js", ["npm test"]),
            ("Cargo.toml", "Rust", ["cargo test"]),
            ("go.mod", "Go", ["go test ./..."]),
        ]
        for marker, language, commands in marker_map:
            if (root / marker).is_file():
                signals.append(ProjectSignal(language=language, marker=marker, test_commands=commands))
        return signals

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
