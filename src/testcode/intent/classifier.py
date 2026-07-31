from __future__ import annotations

import re
from dataclasses import dataclass

ENGLISH_STRONG_PROJECT_TERMS = {
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
ENGLISH_FILE_CHANGE_TERMS = {
    "add",
    "build",
    "change",
    "create",
    "delete",
    "edit",
    "fix",
    "generate",
    "implement",
    "modify",
    "move",
    "patch",
    "remove",
    "rename",
    "replace",
    "refactor",
    "scaffold",
    "update",
    "upgrade",
    "write",
}
ENGLISH_PROJECT_ACTION_TERMS = ENGLISH_FILE_CHANGE_TERMS | {
    "debug",
    "review",
    "test",
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
CHINESE_STRONG_PROJECT_TERMS = {
    "工作区",
    "仓库",
}
CHINESE_PROJECT_ACTION_TERMS = {
    "编辑",
    "构建",
    "创建",
    "删除",
    "检查",
    "审查",
    "调试",
    "生成",
    "修复",
    "实现",
    "新增",
    "重构",
    "测试",
    "提交",
    "修改",
    "移动",
    "移除",
    "替换",
    "重命名",
    "升级",
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
CHINESE_FILE_CHANGE_TERMS = {
    "编辑",
    "构建",
    "创建",
    "删除",
    "生成",
    "实现",
    "新增",
    "修改",
    "移动",
    "移除",
    "替换",
    "重命名",
    "修复",
    "升级",
    "重构",
}
CODE_PATH_RE = re.compile(
    r"(?:^|[\s'\"`])(?:\.?\.?/)?[^\s'\"`]+\.(?:py|js|ts|tsx|jsx|go|rs|java|kt|toml|yaml|yml|json|md)(?:$|[\s'\"`])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RequestIntent:
    project_request: bool
    file_changes: bool


class RequestIntentClassifier:
    """Classify project and file-change intent from one shared rule set."""

    def classify(self, prompt: str, metadata: dict | None = None) -> RequestIntent:
        metadata = metadata or {}
        project_override = metadata.get("include_workspace_context")
        change_override = metadata.get("requires_file_changes")

        project_request = (
            project_override
            if isinstance(project_override, bool)
            else self._is_project_request(prompt, metadata)
        )
        file_changes = (
            change_override
            if isinstance(change_override, bool)
            else project_request and self._contains_change_action(prompt)
        )
        return RequestIntent(
            project_request=bool(project_request),
            file_changes=bool(file_changes),
        )

    def _is_project_request(self, prompt: str, metadata: dict) -> bool:
        context_paths = metadata.get("context_paths", [])
        if isinstance(context_paths, list) and any(
            isinstance(path, str) and path for path in context_paths
        ):
            return True

        lowered = prompt.casefold()
        if self._contains_chinese(lowered, CHINESE_STRONG_PROJECT_TERMS):
            return True
        if self._contains_english(lowered, ENGLISH_STRONG_PROJECT_TERMS):
            return True

        has_action = (
            self._contains_english(lowered, ENGLISH_PROJECT_ACTION_TERMS)
            or self._contains_chinese(lowered, CHINESE_PROJECT_ACTION_TERMS)
        )
        has_target = (
            self._contains_english(lowered, ENGLISH_PROJECT_TARGET_TERMS)
            or self._contains_chinese(lowered, CHINESE_PROJECT_TARGET_TERMS)
        )
        return (has_action and has_target) or CODE_PATH_RE.search(prompt) is not None

    def _contains_change_action(self, prompt: str) -> bool:
        lowered = prompt.casefold()
        return (
            self._contains_english(lowered, ENGLISH_FILE_CHANGE_TERMS)
            or self._contains_chinese(lowered, CHINESE_FILE_CHANGE_TERMS)
        )

    def _contains_english(self, prompt: str, terms: set[str]) -> bool:
        return any(
            re.search(rf"\b{re.escape(term)}\b", prompt)
            for term in terms
        )

    def _contains_chinese(self, prompt: str, terms: set[str]) -> bool:
        return any(term in prompt for term in terms)
