from __future__ import annotations

from ..types import ToolResult


def file_list_summary(files: object, limit: int = 3) -> str:
    if not isinstance(files, list) or not files:
        return "0 files"
    shown = [str(path) for path in files[:limit]]
    remaining = len(files) - len(shown)
    suffix = f", +{remaining} more" if remaining else ""
    return ", ".join(shown) + suffix


def changed_files_summary(changed_files: object) -> str:
    if not isinstance(changed_files, list) or not changed_files:
        return "0 changed files"
    paths = []
    for item in changed_files:
        if isinstance(item, dict):
            path = item.get("path", "")
            status = item.get("status", "").strip()
            paths.append(f"{status} {path}".strip())
        else:
            paths.append(str(item))
    return f"{len(paths)} changed: {file_list_summary(paths)}"


def diff_change_summary(diff: object) -> str:
    added = 0
    removed = 0
    for line in str(diff).splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return f"+{added}/-{removed}"


def process_summary(metadata: dict, *, duration: object | None = None) -> str:
    exit_code = metadata.get("exit_code")
    stdout = _line_count(metadata.get("stdout", ""))
    stderr = _line_count(metadata.get("stderr", ""))
    details = [f"exit {exit_code}" if exit_code is not None else "completed"]
    if stdout:
        details.append(f"stdout {stdout}")
    if stderr:
        details.append(f"stderr {stderr}")
    if duration is not None:
        details.append(f"{duration}s")
    return "; ".join(details)


def process_result_summary(result: ToolResult) -> str:
    if result.error_code == "timeout":
        return f"timeout after {result.metadata.get('timeout')}s"
    return process_summary(result.metadata)


def run_tests_summary(result: ToolResult) -> str:
    return process_summary(result.metadata, duration=result.metadata.get("duration_seconds"))


def read_file_summary(result: ToolResult) -> str:
    path = result.metadata.get("path", "file")
    size = result.metadata.get("bytes")
    suffix = " truncated" if result.metadata.get("truncated") else ""
    return f"read {path} ({size} bytes{suffix})" if size is not None else result.output


def list_dir_summary(result: ToolResult) -> str:
    path = result.metadata.get("path", "directory")
    count = result.metadata.get("count")
    suffix = " truncated" if result.metadata.get("truncated") else ""
    return f"listed {path} ({count} entries{suffix})" if count is not None else result.output


def file_info_summary(result: ToolResult) -> str:
    kind = result.metadata.get("type")
    path = result.metadata.get("path")
    size = result.metadata.get("size")
    if kind and path and size is not None:
        return f"{kind} {path} size={size}"
    return result.output


def match_count_summary(result: ToolResult) -> str:
    count = result.metadata.get("count")
    if count is None:
        return result.output
    suffix = " truncated" if result.metadata.get("truncated") else ""
    return f"{count} matches{suffix}"


def git_status_summary(result: ToolResult) -> str:
    branch = result.metadata.get("branch", "unknown")
    if result.metadata.get("clean"):
        return f"branch {branch}; clean"
    return f"branch {branch}; {changed_files_summary(result.metadata.get('changed_files', []))}"


def git_diff_summary(result: ToolResult) -> str:
    path = result.metadata.get("path")
    scope = f" for {path}" if path else ""
    if not result.metadata.get("has_changes"):
        return f"no diff{scope}"
    return f"diff{scope}: {diff_change_summary(result.metadata.get('stdout', result.output))}"


def git_show_summary(result: ToolResult) -> str:
    revision = result.metadata.get("revision", "revision")
    return f"{revision}: {diff_change_summary(result.metadata.get('stdout', result.output))}"


def patch_summary(result: ToolResult) -> str:
    return f"changed {file_list_summary(result.metadata.get('changed_files', []))}"


def _line_count(value: object) -> int:
    text = str(value or "")
    return len(text.splitlines()) if text else 0
