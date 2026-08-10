from __future__ import annotations

from ..project import ProjectDetector
from ..safety.content import build_content_safety_interceptors
from .base import Tool
from .registry import ToolRegistry
from .builtins import (
    file_info_tool,
    find_files_tool,
    git_diff_tool,
    git_show_tool,
    git_status_tool,
    list_dir_tool,
    patch_tool,
    read_file_tool,
    run_tests_tool,
    search_text_tool,
    shell_exec_tool,
)


class BuiltinToolProvider:
    def __init__(
        self,
        logger,
        limits=None,
        project_detector: ProjectDetector | None = None,
        *,
        include_specialized: bool = True,
    ) -> None:
        self.logger = logger
        self.limits = limits
        self.project_detector = project_detector or ProjectDetector()
        self.include_specialized = include_specialized

    def get_tools(self) -> list[Tool]:
        tools = self.get_core_tools()
        if self.include_specialized:
            tools.extend(
                tool
                for skill_tools in self.get_skill_tools().values()
                for tool in skill_tools
            )
        return tools

    def get_core_tools(self) -> list[Tool]:
        return [
            list_dir_tool(getattr(self.limits, "list_dir_entries", 200)),
            read_file_tool(getattr(self.limits, "read_file_bytes", 64_000)),
            file_info_tool(),
            find_files_tool(getattr(self.limits, "search_results", 200)),
            search_text_tool(getattr(self.limits, "search_results", 200)),
            git_status_tool(),
            git_diff_tool(),
            shell_exec_tool(),
            patch_tool(),
        ]

    def get_skill_tools(self) -> dict[str, tuple[Tool, ...]]:
        return {
            "git-helper": (git_show_tool(),),
            "pytest-helper": (run_tests_tool(self.project_detector),),
        }


def build_builtin_registry(logger, *, max_output_bytes: int = 32_000) -> ToolRegistry:
    """Canonical standalone registry factory used by tests and embedded runtimes."""
    registry = ToolRegistry(
        logger=logger,
        max_output_bytes=max_output_bytes,
        interceptors=build_content_safety_interceptors(logger),
    )
    provider = BuiltinToolProvider(logger)
    for builtin_tool in provider.get_tools():
        registry.register(builtin_tool)
    return registry
