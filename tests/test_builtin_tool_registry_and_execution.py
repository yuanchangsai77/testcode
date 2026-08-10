from __future__ import annotations

import os
import time
import subprocess

from testcode.observability.logger import InMemoryLogger
from testcode.tools.builtin import build_builtin_registry
from testcode.types import ToolAction


def make_registry(*, max_output_bytes: int = 32_000):
    return build_builtin_registry(logger=InMemoryLogger(), max_output_bytes=max_output_bytes)


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


def test_registry_validates_argument_types_and_number_bounds(tmp_path):
    registry = make_registry()

    wrong_type = registry.execute(
        ToolAction(
            name="run_tests",
            arguments={"command": "printf ok", "timeout": "invalid"},
        ),
        cwd=str(tmp_path),
    )
    not_finite = registry.execute(
        ToolAction(
            name="run_tests",
            arguments={"command": "printf ok", "timeout": float("nan")},
        ),
        cwd=str(tmp_path),
    )
    too_large = registry.execute(
        ToolAction(
            name="run_tests",
            arguments={"command": "printf ok", "timeout": 3601},
        ),
        cwd=str(tmp_path),
    )

    assert wrong_type.error_code == "invalid_argument_type"
    assert not_finite.error_code == "invalid_argument_value"
    assert too_large.error_code == "invalid_argument_value"
    assert not registry.state_for("shell_session")


def test_default_definitions_have_structured_schema_and_risk():
    definitions = make_registry().definitions()

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


def test_read_file_refuses_sensitive_files(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret-value\n", encoding="utf-8")
    (tmp_path / "id_rsa.pem").write_text("private key\n", encoding="utf-8")
    registry = make_registry()

    env_result = registry.execute(ToolAction(name="read_file", arguments={"path": ".env"}), cwd=str(tmp_path))
    key_result = registry.execute(ToolAction(name="read_file", arguments={"path": "id_rsa.pem"}), cwd=str(tmp_path))

    assert env_result.error_code == "sensitive_file"
    assert key_result.error_code == "sensitive_file"
    assert "secret-value" not in env_result.output


def test_search_tools_return_matches_no_matches_and_truncation(tmp_path):
    (tmp_path / "a.py").write_text("needle\nneedle\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("hay", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "c.py").write_text("needle\n", encoding="utf-8")
    registry = make_registry()

    files = registry.execute(ToolAction(name="find_files", arguments={"pattern": "*.py"}), cwd=str(tmp_path))
    nested_files = registry.execute(
        ToolAction(name="find_files", arguments={"pattern": "*.py", "path": "nested"}),
        cwd=str(tmp_path),
    )
    matches = registry.execute(
        ToolAction(name="search_text", arguments={"query": "needle", "max_results": 1}),
        cwd=str(tmp_path),
    )
    none = registry.execute(ToolAction(name="search_text", arguments={"query": "absent"}), cwd=str(tmp_path))

    assert files.output == "a.py\nnested/c.py"
    assert nested_files.output == "c.py"
    assert "needle" in matches.output
    assert matches.metadata["count"] == 1
    assert none.output == "no matches"


def test_find_files_supports_authorized_external_path(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "target.py").write_text("print('target')\n", encoding="utf-8")
    registry = make_registry()

    denied = registry.execute(
        ToolAction(name="find_files", arguments={"pattern": "*.py", "path": str(outside)}),
        cwd=str(workspace),
    )
    allowed = registry.execute(
        ToolAction(name="find_files", arguments={"pattern": "*.py", "path": str(outside)}),
        cwd=str(workspace),
        allowed_roots=[str(outside)],
    )

    assert denied.error_code == "path_outside_workspace"
    assert allowed.success is True
    assert allowed.output == "target.py"


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


def test_shell_exec_rejects_cd_outside_workspace(tmp_path):
    (tmp_path / "child").mkdir()
    registry = make_registry()

    inside = registry.execute(ToolAction(name="shell_exec", arguments={"command": "cd child && pwd"}), cwd=str(tmp_path))
    outside = make_registry().execute(ToolAction(name="shell_exec", arguments={"command": "cd .. && pwd"}), cwd=str(tmp_path))
    compact_outside = make_registry().execute(
        ToolAction(name="shell_exec", arguments={"command": "cd ..&& pwd"}),
        cwd=str(tmp_path),
    )
    dynamic_outside = make_registry().execute(
        ToolAction(name="shell_exec", arguments={"command": "cd $HOME && pwd"}),
        cwd=str(tmp_path),
    )

    assert inside.success is True
    assert inside.metadata["stdout"].strip() == str(tmp_path / "child")
    assert outside.error_code == "path_outside_workspace"
    assert "outside the current workspace" in outside.output
    assert compact_outside.error_code == "path_outside_workspace"
    assert dynamic_outside.error_code == "path_outside_workspace"


def test_shell_exec_allows_cd_in_command_arguments(tmp_path):
    result = make_registry().execute(
        ToolAction(name="shell_exec", arguments={"command": "printf '%s\\n' cd /tmp"}),
        cwd=str(tmp_path),
    )

    assert result.success is True
    assert result.metadata["stdout"].strip() == "cd\n/tmp"


def test_shell_exec_bounds_captured_output_without_losing_shell_state(tmp_path):
    registry = make_registry(max_output_bytes=32)

    result = registry.execute(
        ToolAction(name="shell_exec", arguments={"command": "head -c 4096 /dev/zero | tr '\\0' x"}),
        cwd=str(tmp_path),
    )
    follow_up = registry.execute(ToolAction(name="shell_exec", arguments={"command": "printf ready"}), cwd=str(tmp_path))

    assert result.success is True
    assert result.metadata["truncated"] is True
    assert "...truncated..." in result.metadata["stdout"]
    assert follow_up.success is True
    assert follow_up.metadata["stdout"].strip() == "ready"


def test_shell_exec_reset_terminates_the_entire_process_group(tmp_path):
    registry = make_registry()
    result = registry.execute(
        ToolAction(name="shell_exec", arguments={"command": "sleep 30 & printf %s \"$!\""}),
        cwd=str(tmp_path),
    )
    child_pid = int(result.metadata["stdout"].strip())
    shell = registry.state_for("shell_session")

    assert result.success is True
    assert shell is not None
    if os.name == "posix":
        assert os.getpgid(shell.process.pid) == shell.process.pid

    registry.reset_state()

    deadline = time.monotonic() + 1
    while _process_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _process_exists(child_pid) is False


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_shell_exec_persists_cd_and_environment_within_registry_state(tmp_path):
    (tmp_path / "child").mkdir()
    registry = make_registry()

    cd_result = registry.execute(ToolAction(name="shell_exec", arguments={"command": "cd child"}), cwd=str(tmp_path))
    pwd_result = registry.execute(ToolAction(name="shell_exec", arguments={"command": "pwd"}), cwd=str(tmp_path))
    env_set = registry.execute(ToolAction(name="shell_exec", arguments={"command": "export TESTCODE_MARKER=kept"}), cwd=str(tmp_path))
    env_read = registry.execute(ToolAction(name="shell_exec", arguments={"command": "printf $TESTCODE_MARKER"}), cwd=str(tmp_path))
    parent = registry.execute(ToolAction(name="shell_exec", arguments={"command": "cd .. && pwd"}), cwd=str(tmp_path))

    assert cd_result.success is True
    assert cd_result.metadata["cwd"] == str(tmp_path / "child")
    assert pwd_result.metadata["stdout"].strip() == str(tmp_path / "child")
    assert env_set.success is True
    assert env_read.metadata["stdout"].strip() == "kept"
    assert parent.success is True
    assert parent.metadata["stdout"].strip() == str(tmp_path)


def test_run_tests_reports_test_status(tmp_path):
    registry = make_registry()

    passed = registry.execute(ToolAction(name="run_tests", arguments={"command": "printf ok"}), cwd=str(tmp_path))
    failed = registry.execute(ToolAction(name="run_tests", arguments={"command": "exit 2"}), cwd=str(tmp_path))

    assert passed.success is True
    assert passed.metadata["passed"] is True
    assert passed.output.startswith("tests passed")
    assert registry.summarize_result(passed).startswith("tests passed; exit 0")
    assert failed.success is False
    assert failed.metadata["passed"] is False
    assert failed.output.startswith("tests failed")
    assert registry.summarize_result(failed).startswith("tests failed; exit 2")


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
    assert rejected.error_code == "file_not_read"


def test_patch_reports_syntax_error_for_corrupt_diff(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("before\n", encoding="utf-8")
    registry = make_registry()
    corrupt = """--- a/file.txt
+++ b/file.txt
@@ -1,2 +1,3 @@
 before
garbage line here
"""

    registry.execute(ToolAction(name="read_file", arguments={"path": "file.txt"}), cwd=str(tmp_path))
    result = registry.execute(ToolAction(name="patch", arguments={"diff": corrupt}), cwd=str(tmp_path))

    assert result.error_code == "patch_syntax_error"
    assert "patch syntax error" in result.output
    assert "corrupt patch" in result.output
    assert target.read_text(encoding="utf-8") == "before\n"


def test_patch_accepts_model_diff_without_trailing_newline(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("before\n", encoding="utf-8")
    registry = make_registry()
    diff = "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-before\n+after"

    registry.execute(
        ToolAction(name="read_file", arguments={"path": "file.txt"}),
        cwd=str(tmp_path),
    )
    applied = registry.execute(
        ToolAction(name="patch", arguments={"diff": diff}),
        cwd=str(tmp_path),
    )

    assert applied.success is True
    assert target.read_text(encoding="utf-8") == "after\n"


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


def test_patch_requires_only_the_affected_lines_to_have_been_read(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    registry = make_registry()
    diff = """--- a/file.txt
+++ b/file.txt
@@ -2,3 +2,3 @@
 two
-three
+THREE
 four
"""

    prefix = registry.execute(
        ToolAction(
            name="read_file",
            arguments={"path": "file.txt", "max_bytes": 4},
        ),
        cwd=str(tmp_path),
    )
    blocked = registry.execute(
        ToolAction(name="patch", arguments={"diff": diff}),
        cwd=str(tmp_path),
    )
    targeted = registry.execute(
        ToolAction(
            name="read_file",
            arguments={"path": "file.txt", "start_line": 2, "end_line": 4},
        ),
        cwd=str(tmp_path),
    )
    applied = registry.execute(
        ToolAction(name="patch", arguments={"diff": diff}),
        cwd=str(tmp_path),
    )

    assert prefix.metadata["end_line"] == 1
    assert blocked.error_code == "file_not_read"
    assert blocked.metadata["read_hint"] == {
        "path": "file.txt",
        "start_line": 2,
        "end_line": 4,
    }
    assert targeted.output == "two\nthree\nfour\n"
    assert applied.success is True
    assert target.read_text(encoding="utf-8") == "one\ntwo\nTHREE\nfour\n"


def test_search_context_counts_as_read_but_unreturned_lines_do_not(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text(
        "one\ntwo\nthree\nneedle\nfive\nsix\nseven\neight\n",
        encoding="utf-8",
    )
    registry = make_registry()
    unseen_diff = """--- a/file.txt
+++ b/file.txt
@@ -8 +8 @@
-eight
+EIGHT
"""
    seen_diff = """--- a/file.txt
+++ b/file.txt
@@ -1,7 +1,7 @@
 one
 two
 three
-needle
+NEEDLE
 five
 six
 seven
"""

    searched = registry.execute(
        ToolAction(
            name="search_text",
            arguments={"query": "needle", "path": "file.txt"},
        ),
        cwd=str(tmp_path),
    )
    blocked = registry.execute(
        ToolAction(name="patch", arguments={"diff": unseen_diff}),
        cwd=str(tmp_path),
    )
    applied = registry.execute(
        ToolAction(name="patch", arguments={"diff": seen_diff}),
        cwd=str(tmp_path),
    )

    assert ":1:one" in searched.output
    assert ":7:seven" in searched.output
    assert ":8:eight" not in searched.output
    assert blocked.error_code == "file_not_read"
    assert applied.success is True
    assert "\nNEEDLE\n" in target.read_text(encoding="utf-8")


def test_patch_allows_unrelated_external_changes_outside_observed_hunk(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    registry = make_registry()
    diff = """--- a/file.txt
+++ b/file.txt
@@ -1,2 +1,2 @@
-one
+ONE
 two
"""

    registry.execute(
        ToolAction(
            name="read_file",
            arguments={"path": "file.txt", "start_line": 1, "end_line": 2},
        ),
        cwd=str(tmp_path),
    )
    target.write_text("one\ntwo\nTHREE\n", encoding="utf-8")
    applied = registry.execute(
        ToolAction(name="patch", arguments={"diff": diff}),
        cwd=str(tmp_path),
    )

    assert applied.success is True
    assert target.read_text(encoding="utf-8") == "ONE\ntwo\nTHREE\n"


def test_patch_automatically_relocates_unique_unchanged_observed_lines(tmp_path):
    target = tmp_path / "file.txt"
    original_lines = [f"line-{line_no:02d}" for line_no in range(1, 61)]
    target.write_text("\n".join(original_lines) + "\n", encoding="utf-8")
    registry = make_registry()
    diff = """--- a/file.txt
+++ b/file.txt
@@ -31,3 +31,3 @@
 line-31
-line-32
+changed-32
 line-33
"""

    registry.execute(
        ToolAction(
            name="read_file",
            arguments={"path": "file.txt", "start_line": 30, "end_line": 35},
        ),
        cwd=str(tmp_path),
    )
    inserted = [f"inserted-{line_no:02d}" for line_no in range(1, 21)]
    target.write_text(
        "\n".join(inserted + original_lines) + "\n",
        encoding="utf-8",
    )
    applied = registry.execute(
        ToolAction(name="patch", arguments={"diff": diff}),
        cwd=str(tmp_path),
    )

    assert applied.success is True
    assert applied.metadata["relocations"] == [
        {
            "path": "file.txt",
            "old_start": 31,
            "old_end": 33,
            "new_start": 51,
            "new_end": 53,
            "offset": 20,
        }
    ]
    assert "offset +20" in applied.output
    assert registry.summarize_result(applied) == (
        "changed file.txt; relocated 1 hunk (offset +20)"
    )
    assert target.read_text(encoding="utf-8").splitlines()[51] == "changed-32"


def test_patch_relocates_uniquely_observed_context_from_incorrect_hunk_line(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("alpha\nbeta\ntarget\nomega\n", encoding="utf-8")
    registry = make_registry()
    registry.execute(
        ToolAction(
            name="read_file",
            arguments={"path": "file.txt", "start_line": 3, "end_line": 3},
        ),
        cwd=str(tmp_path),
    )

    applied = registry.execute(
        ToolAction(
            name="patch",
            arguments={
                "diff": "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1,2 @@\n target\n+added\n"
            },
        ),
        cwd=str(tmp_path),
    )

    assert applied.success is True
    assert applied.metadata["relocations"][0]["offset"] == 2
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\ntarget\nadded\nomega\n"


def test_patch_rejects_ambiguous_relocation_matches(tmp_path):
    target = tmp_path / "file.txt"
    original = ["header", "before", "target", "after", "middle", "before", "target", "after"]
    target.write_text("\n".join(original) + "\n", encoding="utf-8")
    registry = make_registry()
    diff = """--- a/file.txt
+++ b/file.txt
@@ -2,3 +2,3 @@
 before
-target
+changed
 after
"""

    registry.execute(
        ToolAction(
            name="read_file",
            arguments={"path": "file.txt", "start_line": 2, "end_line": 4},
        ),
        cwd=str(tmp_path),
    )
    target.write_text("inserted\n" + "\n".join(original) + "\n", encoding="utf-8")
    blocked = registry.execute(
        ToolAction(name="patch", arguments={"diff": diff}),
        cwd=str(tmp_path),
    )

    assert blocked.error_code == "file_changed_since_read"
    assert "changed" not in target.read_text(encoding="utf-8")


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


def test_patch_applies_to_authorized_external_root(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    target = outside / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    registry = make_registry()
    diff = f"""--- {target}
+++ {target}
@@ -1 +1 @@
-old
+new
"""

    registry.execute(
        ToolAction(name="read_file", arguments={"path": str(target)}),
        cwd=str(workspace),
        allowed_roots=[str(outside)],
    )
    result = registry.execute(
        ToolAction(name="patch", arguments={"diff": diff}),
        cwd=str(workspace),
        allowed_roots=[str(outside)],
    )

    assert result.success is True
    assert target.read_text(encoding="utf-8") == "new\n"


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
