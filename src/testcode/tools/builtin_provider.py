from __future__ import annotations

from .base import Tool
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


class BuiltinToolProvider:
    def __init__(self, logger) -> None:
        self.logger = logger

    def get_tools(self) -> list[Tool]:
        return [
            list_dir_tool(),
            read_file_tool(),
            file_info_tool(),
            find_files_tool(),
            search_text_tool(),
            shell_exec_tool(),
            run_tests_tool(),
            git_status_tool(),
            git_diff_tool(),
            git_show_tool(),
            patch_tool(),
            apply_change_tool(),
        ]
