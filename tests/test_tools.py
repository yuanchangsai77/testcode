from __future__ import annotations

import subprocess

from testcode.observability.logger import InMemoryLogger
from testcode.tools.builtin import build_builtin_registry
from testcode.types import ToolAction


def make_registry():
    return build_builtin_registry(logger=InMemoryLogger())


def test_registry_rejects_unknown_tool_and_arguments(tmp_path):
    registry = make_registry()

    unknown = registry.execute(ToolAction(name="nope"), cwd=str(tmp_path))
    extra = registry.execute(
        ToolAction(name="file_info", arguments={"path": ".", "extra": True}),
        cwd=str(tmp_path),
    )
    missing = registry.execute(ToolAction(name="read_file"), cwd=str(tmp_path))

    assert unknown.error_code == "unknown_tool"
    assert extra.error_code == "unknown_argument"
    assert missing.error_code == "missing_argument"


def test_default_definitions_hide_apply_change():
    definitions = make_registry().definitions()

    assert "apply_change" not in {definition.name for definition in definitions}
    assert all(definition.input_schema for definition in definitions)
    assert all(definition.risk_level for definition in definitions)


def test_workspace_paths_are_relative_and_bounded(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "hello.txt").write_text("hello", encoding="utf-8")
    registry = make_registry()

    relative = registry.execute(ToolAction(name="read_file", arguments={"path": "nested/hello.txt"}), cwd=str(tmp_path))
    parent = registry.execute(ToolAction(name="file_info", arguments={"path": ".."}), cwd=str(tmp_path))
    missing = registry.execute(ToolAction(name="file_info", arguments={"path": "missing.txt"}), cwd=str(tmp_path))
    directory_as_file = registry.execute(ToolAction(name="read_file", arguments={"path": "nested"}), cwd=str(tmp_path))

    assert relative.success is True
    assert relative.output == "hello"
    assert parent.error_code == "path_outside_workspace"
    assert missing.error_code == "path_not_found"
    assert directory_as_file.error_code == "path_not_file"


def test_file_tools_handle_empty_directory_large_and_binary_files(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    large = tmp_path / "large.txt"
    large.write_text("abcdef", encoding="utf-8")
    binary = tmp_path / "data.bin"
    binary.write_bytes(b"a\0b")
    registry = make_registry()

    listing = registry.execute(ToolAction(name="list_dir", arguments={"path": "empty"}), cwd=str(tmp_path))
    clipped = registry.execute(ToolAction(name="read_file", arguments={"path": "large.txt", "max_bytes": 3}), cwd=str(tmp_path))
    refused = registry.execute(ToolAction(name="read_file", arguments={"path": "data.bin"}), cwd=str(tmp_path))

    assert listing.output == "empty directory"
    assert clipped.output == "abc"
    assert clipped.metadata["truncated"] is True
    assert refused.error_code == "binary_file"


def test_search_tools_return_matches_no_matches_and_truncation(tmp_path):
    (tmp_path / "a.py").write_text("needle\nneedle\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("hay", encoding="utf-8")
    registry = make_registry()

    files = registry.execute(ToolAction(name="find_files", arguments={"pattern": "*.py"}), cwd=str(tmp_path))
    matches = registry.execute(
        ToolAction(name="search_text", arguments={"query": "needle", "max_results": 1}),
        cwd=str(tmp_path),
    )
    none = registry.execute(ToolAction(name="search_text", arguments={"query": "absent"}), cwd=str(tmp_path))

    assert files.output == "a.py"
    assert "needle" in matches.output
    assert matches.metadata["count"] == 1
    assert none.output == "no matches"


def test_shell_exec_reports_success_failure_and_timeout(tmp_path):
    registry = make_registry()

    success = registry.execute(ToolAction(name="shell_exec", arguments={"command": "printf ok"}), cwd=str(tmp_path))
    failure = registry.execute(ToolAction(name="shell_exec", arguments={"command": "exit 7"}), cwd=str(tmp_path))
    timeout = registry.execute(
        ToolAction(name="shell_exec", arguments={"command": "sleep 1", "timeout": 0.01}),
        cwd=str(tmp_path),
    )

    assert success.success is True
    assert success.metadata["exit_code"] == 0
    assert "ok" in success.output
    assert failure.error_code == "nonzero_exit"
    assert failure.metadata["exit_code"] == 7
    assert timeout.error_code == "timeout"


def test_patch_applies_unified_diff_and_rejects_bad_context(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("before\n", encoding="utf-8")
    registry = make_registry()
    diff = """--- a/file.txt
+++ b/file.txt
@@ -1 +1 @@
-before
+after
"""
    bad_diff = """--- a/file.txt
+++ b/file.txt
@@ -1 +1 @@
-missing
+after
"""

    applied = registry.execute(ToolAction(name="patch", arguments={"diff": diff}), cwd=str(tmp_path))
    rejected = registry.execute(ToolAction(name="patch", arguments={"diff": bad_diff}), cwd=str(tmp_path))

    assert applied.success is True
    assert applied.metadata["changed_files"] == ["file.txt"]
    assert target.read_text(encoding="utf-8") == "after\n"
    assert rejected.error_code == "patch_context_mismatch"


def test_git_read_tools_report_non_git_and_dirty_repo(tmp_path):
    registry = make_registry()

    non_git = registry.execute(ToolAction(name="git_status"), cwd=str(tmp_path))
    assert non_git.error_code == "not_git_repository"

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "tracked.txt").write_text("two\n", encoding="utf-8")

    status = registry.execute(ToolAction(name="git_status"), cwd=str(tmp_path))
    diff = registry.execute(ToolAction(name="git_diff"), cwd=str(tmp_path))

    assert status.success is True
    assert "M tracked.txt" in status.output
    assert "-one" in diff.output
    assert "+two" in diff.output
