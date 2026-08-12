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
    "重命名",
    "替换",
    "新增",
    "修改",
    "生成",
    "删除",
    "实现",
    "重构",
    "升级",
    "修复",
    "创建",
    "构建",
    "编辑",
    "移动",
    "移除",
    "写",
}
CHINESE_NEGATED_CHANGE_TERMS = {
    "禁止修改",
    "不要修改",
    "请勿修改",
    "无需修改",
    "不能修改",
    "不得修改",
    "禁止编辑",
    "不要编辑",
    "请勿编辑",
    "无需编辑",
    "不能编辑",
    "不得编辑",
    "禁止写入",
    "不要写入",
    "禁止创建",
    "不要创建",
    "禁止删除",
    "不要删除",
    "禁止生成",
    "不要生成",
}
CHINESE_NEGATED_CHANGE_CLAUSE_RE = re.compile(
    r"(?:禁止|不要|请勿|无需|不能|不得)"
    r"(?:(?![，,。；;！!？?]|但是|但|不过|然而).)*"
)
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
        # 否定作用域覆盖同一分句中的并列动作；分句后的独立交付要求仍参与判断。
        # 例如“不要修改或删除文件，只做审查”是只读，而
        # “无需修改源码，直接写报告”仍要求产生文件结果。
        lowered = CHINESE_NEGATED_CHANGE_CLAUSE_RE.sub("", lowered)
        for term in sorted(CHINESE_NEGATED_CHANGE_TERMS, key=len, reverse=True):
            lowered = lowered.replace(term, "")
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
