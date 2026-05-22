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
    assert "sha256" in clipped.metadata
    assert "mtime_ns" in clipped.metadata
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
    assert registry.summarize_result(success) == "exit 0; stdout 1"
    assert "ok" in success.output
    assert failure.error_code == "nonzero_exit"
    assert failure.metadata["exit_code"] == 7
    assert registry.summarize_result(failure) == "exit 7"
    assert timeout.error_code == "timeout"
    assert registry.summarize_result(timeout) == "timeout after 0.01s"


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

    registry.execute(ToolAction(name="read_file", arguments={"path": "file.txt"}), cwd=str(tmp_path))
    applied = registry.execute(ToolAction(name="patch", arguments={"diff": diff}), cwd=str(tmp_path))
    registry.execute(ToolAction(name="read_file", arguments={"path": "file.txt"}), cwd=str(tmp_path))
    rejected = registry.execute(ToolAction(name="patch", arguments={"diff": bad_diff}), cwd=str(tmp_path))

    assert applied.success is True
    assert applied.metadata["changed_files"] == ["file.txt"]
    assert applied.metadata["preview"] == diff
    assert applied.metadata["line_stats"] == {"added": 1, "removed": 1}
    assert registry.summarize_result(applied) == "changed file.txt"
    assert target.read_text(encoding="utf-8") == "after\n"
    assert rejected.error_code == "patch_context_mismatch"


def test_patch_requires_read_for_existing_file_and_rejects_stale_read(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("before\n", encoding="utf-8")
    registry = make_registry()
    diff = """--- a/file.txt
+++ b/file.txt
@@ -1 +1 @@
-before
+after
"""

    unread = registry.execute(ToolAction(name="patch", arguments={"diff": diff}), cwd=str(tmp_path))
    registry.execute(ToolAction(name="read_file", arguments={"path": "file.txt"}), cwd=str(tmp_path))
    target.write_text("external\n", encoding="utf-8")
    stale = registry.execute(ToolAction(name="patch", arguments={"diff": diff}), cwd=str(tmp_path))

    assert unread.error_code == "file_not_read"
    assert stale.error_code == "file_changed_since_read"
    assert target.read_text(encoding="utf-8") == "external\n"


def test_patch_rejects_file_count_and_line_count_limits(tmp_path):
    registry = make_registry()
    many_files = "\n".join(
        f"--- /dev/null\n+++ b/file_{index}.txt\n@@ -0,0 +1 @@\n+{index}"
        for index in range(21)
    )
    many_lines = "--- /dev/null\n+++ b/large.txt\n@@ -0,0 +1,2001 @@\n" + "\n".join(
        f"+{index}" for index in range(2001)
    )

    too_many_files = registry.execute(ToolAction(name="patch", arguments={"diff": many_files}), cwd=str(tmp_path))
    too_many_lines = registry.execute(ToolAction(name="patch", arguments={"diff": many_lines}), cwd=str(tmp_path))

    assert too_many_files.error_code == "patch_too_large"
    assert too_many_files.metadata["max_files"] == 20
    assert too_many_lines.error_code == "patch_too_large"
    assert too_many_lines.metadata["max_lines"] == 2000


def test_patch_applies_multiple_files_and_rejects_outside_workspace(tmp_path):
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    registry = make_registry()
    multi = """--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-a
+aa
--- a/b.txt
+++ b/b.txt
@@ -1 +1 @@
-b
+bb
"""
    outside = """--- a/../outside.txt
+++ b/../outside.txt
@@ -1 +1 @@
-old
+new
"""

    registry.execute(ToolAction(name="read_file", arguments={"path": "a.txt"}), cwd=str(tmp_path))
    registry.execute(ToolAction(name="read_file", arguments={"path": "b.txt"}), cwd=str(tmp_path))
    applied = registry.execute(ToolAction(name="patch", arguments={"diff": multi}), cwd=str(tmp_path))
    rejected = registry.execute(ToolAction(name="patch", arguments={"diff": outside}), cwd=str(tmp_path))

    assert applied.success is True
    assert applied.metadata["changed_files"] == ["a.txt", "b.txt"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "aa\n"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "bb\n"
    assert rejected.error_code == "path_outside_workspace"


def test_patch_rejects_empty_diff_and_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    registry = make_registry()
    diff = """--- a/link.txt
+++ b/link.txt
@@ -1 +1 @@
-old
+new
"""

    empty = registry.execute(ToolAction(name="patch", arguments={"diff": "not a diff"}), cwd=str(tmp_path))
    escaped = registry.execute(ToolAction(name="patch", arguments={"diff": diff}), cwd=str(tmp_path))

    assert empty.error_code == "invalid_patch"
    assert escaped.error_code == "path_outside_workspace"
    assert outside.read_text(encoding="utf-8") == "old\n"


def test_git_read_tools_report_non_git_and_dirty_repo(tmp_path):
    registry = make_registry()

    non_git = registry.execute(ToolAction(name="git_status"), cwd=str(tmp_path))
    non_git_diff = registry.execute(ToolAction(name="git_diff"), cwd=str(tmp_path))
    non_git_show = registry.execute(ToolAction(name="git_show", arguments={"revision": "HEAD"}), cwd=str(tmp_path))
    assert non_git.error_code == "not_git_repository"
    assert non_git.output == "not a git repository"
    assert non_git_diff.error_code == "not_git_repository"
    assert non_git_show.error_code == "not_git_repository"

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    clean = registry.execute(ToolAction(name="git_status"), cwd=str(tmp_path))
    clean_diff = registry.execute(ToolAction(name="git_diff"), cwd=str(tmp_path))

    assert clean.success is True
    assert clean.metadata["clean"] is True
    assert clean.metadata["changed_files"] == []
    assert registry.summarize_result(clean).endswith("; clean")
    assert "status: clean" in clean.output
    assert "exit_code:" not in clean.output
    assert clean_diff.success is True
    assert clean_diff.output == "no diff"
    assert clean_diff.metadata["has_changes"] is False
    assert registry.summarize_result(clean_diff) == "no diff"

    (tmp_path / "tracked.txt").write_text("two\n", encoding="utf-8")

    status = registry.execute(ToolAction(name="git_status"), cwd=str(tmp_path))
    diff = registry.execute(ToolAction(name="git_diff"), cwd=str(tmp_path))
    show = registry.execute(ToolAction(name="git_show", arguments={"revision": "HEAD"}), cwd=str(tmp_path))
    missing = registry.execute(ToolAction(name="git_show", arguments={"revision": "missing"}), cwd=str(tmp_path))

    assert status.success is True
    assert status.metadata["clean"] is False
    assert status.metadata["changed_files"] == [{"status": "M", "path": "tracked.txt"}]
    assert registry.summarize_result(status).endswith("1 changed: M tracked.txt")
    assert "M tracked.txt" in status.output
    assert "exit_code:" not in status.output
    assert "-one" in diff.output
    assert "+two" in diff.output
    assert diff.metadata["has_changes"] is True
    assert registry.summarize_result(diff) == "diff: +1/-1"
    assert show.success is True
    assert show.metadata["revision"] == "HEAD"
    assert registry.summarize_result(show).startswith("HEAD: ")
    assert missing.error_code == "revision_not_found"
