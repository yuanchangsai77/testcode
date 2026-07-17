from __future__ import annotations

from .builtins import (
    apply_change_tool,
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
    registry = ToolRegistry(logger=logger, max_output_bytes=max_output_bytes)
    for factory in (
        list_dir_tool,
        read_file_tool,
        file_info_tool,
        find_files_tool,
        search_text_tool,
        shell_exec_tool,
        run_tests_tool,
        git_status_tool,
        git_diff_tool,
        git_show_tool,
        patch_tool,
        apply_change_tool,
    ):
        registry.register(factory())
    return registry


from .builtin_provider import BuiltinToolProvider

