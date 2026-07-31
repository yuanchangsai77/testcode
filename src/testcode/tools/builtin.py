from __future__ import annotations

from ..project import ProjectDetector
from ..safety.content import build_content_safety_interceptors
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
from .registry import ToolRegistry


def build_builtin_registry(logger, *, max_output_bytes: int = 32_000) -> ToolRegistry:
    project_detector = ProjectDetector()
    registry = ToolRegistry(
        logger=logger,
        max_output_bytes=max_output_bytes,
        interceptors=build_content_safety_interceptors(logger),
    )
    for builtin_tool in (
        list_dir_tool(),
        read_file_tool(),
        file_info_tool(),
        find_files_tool(),
        search_text_tool(),
        shell_exec_tool(),
        run_tests_tool(project_detector),
        git_status_tool(),
        git_diff_tool(),
        git_show_tool(),
        patch_tool(),
    ):
        registry.register(builtin_tool)
    return registry


from .builtin_provider import BuiltinToolProvider
