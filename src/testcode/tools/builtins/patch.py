from __future__ import annotations

from pathlib import Path
import re

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import ResolvedPath, resolve_workspace_path, retarget, run_command, schema
from ..summary import patch_summary
from .read_state import (
    observed_empty_file,
    observed_line,
    observed_line_matches,
    snapshot,
    snapshot_changed,
    text_lines,
)

MAX_PATCH_FILES = 20
MAX_PATCH_LINES = 2_000


def tool() -> SimpleTool:
    return SimpleTool(
        name="patch",
        description=(
            "Apply a unified diff inside the workspace after validating paths and "
            "observed context. Unchanged hunks that moved to one unique location "
            "may be safely relocated and reported."
        ),
        arguments={"diff": "Unified diff text to apply."},
        input_schema=schema({"diff": {"type": "string"}}, required=["diff"]),
        risk_level="write",
        handler=run,
        summarizer=patch_summary,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    diff = str(action.arguments["diff"])
    lines = diff.splitlines()
    if len(lines) > MAX_PATCH_LINES:
        return ToolResult(
            name=action.name,
            success=False,
            output=f"patch exceeds maximum line count: {len(lines)} > {MAX_PATCH_LINES}",
            error_code="patch_too_large",
            metadata={"line_count": len(lines), "max_lines": MAX_PATCH_LINES},
        )
    if diff and not diff.endswith("\n"):
        diff += "\n"

    changed_files = changed_files_from_diff(diff)
    if not changed_files:
        return ToolResult(name=action.name, success=False, output="diff contains no changed files", error_code="invalid_patch")
    if len(changed_files) > MAX_PATCH_FILES:
        return ToolResult(
            name=action.name,
            success=False,
            output=f"patch changes too many files: {len(changed_files)} > {MAX_PATCH_FILES}",
            error_code="patch_too_large",
            metadata={"changed_files": changed_files, "max_files": MAX_PATCH_FILES},
        )

    hunks_by_file = parse_hunks(diff)
    relocations: list[dict[str, object]] = []
    resolved_files = {}
    for path in changed_files:
        resolved = resolve_workspace_path(context, path)
        if isinstance(resolved, ToolResult):
            return retarget(resolved, action.name)
        resolved_files[path] = resolved
        if resolved.path.exists():
            read_error, file_relocations = validate_hunks_were_read(
                action.name,
                path,
                resolved.path,
                hunks_by_file.get(path, []),
                context.state,
            )
            if read_error is not None:
                read_error.metadata.setdefault("changed_files", changed_files)
                return read_error
            relocations.extend(file_relocations)

    roots = {resolved.root for resolved in resolved_files.values()}
    if len(roots) != 1:
        return ToolResult(
            name=action.name,
            success=False,
            output="a patch may only modify files within one authorized workspace root",
            error_code="patch_multiple_roots",
            metadata={"changed_files": changed_files, "roots": sorted(str(root) for root in roots)},
        )

    root = roots.pop()
    diff = relocate_diff_hunks(diff, hunks_by_file)
    diff = rebase_diff_paths(diff, resolved_files, root)

    check = run_command(["git", "apply", "--check", "-"], root, input_text=diff, shell=False)
    use_recount = False
    if not check.success:
        error_code = classify_git_apply_failure(check.output)
        if error_code == "patch_syntax_error":
            recount_check = run_command(["git", "apply", "--check", "--recount", "-"], root, input_text=diff, shell=False)
            if recount_check.success:
                check = recount_check
                use_recount = True
        
        if not use_recount:
            if error_code == "patch_context_mismatch":
                stale = stale_read_error(
                    action.name,
                    resolved_files,
                    hunks_by_file,
                    context.state,
                )
                if stale is not None:
                    stale.metadata.update(
                        {
                            "changed_files": changed_files,
                            "preview": diff,
                            "line_stats": line_stats(diff),
                        }
                    )
                    return stale
            return ToolResult(
                name=action.name,
                success=False,
                output=format_patch_failure(check.output, error_code),
                error_code=error_code,
                metadata={"changed_files": changed_files, "preview": diff, "line_stats": line_stats(diff)},
            )

    cmd = ["git", "apply", "--recount", "-"] if use_recount else ["git", "apply", "-"]
    applied = run_command(cmd, root, input_text=diff, shell=False)
    if not applied.success:
        return retarget(applied, action.name)

    output = "applied patch:\n" + "\n".join(changed_files)
    if relocations:
        notices = "\n".join(
            (
                f"{item['path']}: lines {item['old_start']}-{item['old_end']} "
                f"moved to {item['new_start']}-{item['new_end']} "
                f"(offset {int(item['offset']):+d})"
            )
            for item in relocations
        )
        output += "\nautomatically relocated unchanged patch context:\n" + notices
    return ToolResult(
        name=action.name,
        success=True,
        output=output,
        metadata={
            "changed_files": changed_files,
            "preview": diff,
            "line_stats": line_stats(diff),
            "relocations": relocations,
        },
    )


def changed_files_from_diff(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            raw_path = line[4:].strip()
            if raw_path == "/dev/null":
                continue
            if raw_path.startswith("b/"):
                raw_path = raw_path[2:]
            files.append(raw_path)
        elif line.startswith("--- "):
            raw_path = line[4:].strip()
            if raw_path == "/dev/null":
                continue
            if raw_path.startswith("a/"):
                raw_path = raw_path[2:]
            if raw_path not in files:
                files.append(raw_path)
    return sorted(set(files))


def rebase_diff_paths(diff: str, resolved_files: dict[str, ResolvedPath], root: Path) -> str:
    """Make accepted absolute paths relative to the root used by git apply."""
    relative_paths = {
        path: resolved.path.relative_to(root).as_posix()
        for path, resolved in resolved_files.items()
    }
    rewritten = []
    for line in diff.splitlines():
        if line.startswith(("--- ", "+++ ")):
            raw_path = line[4:].strip()
            path = _diff_path(raw_path)
            relative_path = relative_paths.get(path)
            if relative_path is not None:
                prefix = "a/" if line.startswith("--- ") else "b/"
                line = line[:4] + prefix + relative_path
        rewritten.append(line)
    return "\n".join(rewritten) + ("\n" if diff.endswith("\n") else "")


def _diff_path(raw_path: str) -> str:
    if raw_path.startswith(("a/", "b/")):
        return raw_path[2:]
    return raw_path


def parse_hunks(diff: str) -> dict[str, list[dict[str, object]]]:
    hunks: dict[str, list[dict[str, object]]] = {}
    lines = diff.splitlines()
    current_path: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- "):
            old_path = _diff_path(line[4:].strip())
            new_path = ""
            if index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
                new_path = _diff_path(lines[index + 1][4:].strip())
            current_path = new_path if old_path == "/dev/null" else old_path
            index += 1
        elif line.startswith("@@ ") and current_path and current_path != "/dev/null":
            match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", line)
            if match is None:
                index += 1
                continue
            old_start = int(match.group(1))
            old_line = old_start
            required: list[tuple[int, str]] = []
            index += 1
            while index < len(lines) and not lines[index].startswith(("@@ ", "--- ")):
                body_line = lines[index]
                if body_line.startswith((" ", "-")):
                    required.append((old_line, body_line[1:]))
                    old_line += 1
                elif not body_line.startswith(("+", "\\")):
                    break
                index += 1
            hunks.setdefault(current_path, []).append(
                {"old_start": old_start, "required": required}
            )
            continue
        index += 1
    return hunks


def validate_hunks_were_read(
    tool_name: str,
    diff_path: str,
    path: Path,
    hunks: list[dict[str, object]],
    state: dict,
) -> tuple[ToolResult | None, list[dict[str, object]]]:
    read_files = state.get("read_files", {})
    entry = read_files.get(str(path))
    if not entry:
        affected_lines = hunk_affected_lines(hunks)
        start_line = min(affected_lines) if affected_lines else 1
        end_line = max(affected_lines) if affected_lines else start_line
        return ToolResult(
            name=tool_name,
            success=False,
            output=(
                f"patch target has not been inspected: {diff_path}. "
                f"Read lines {start_line}-{end_line} with read_file and retry."
            ),
            error_code="file_not_read",
            metadata={
                "path": str(path),
                "read_hint": {
                    "path": diff_path,
                    "start_line": start_line,
                    "end_line": end_line,
                },
            },
        ), []

    missing: list[int] = []
    for hunk in hunks:
        required = hunk.get("required", [])
        if required:
            missing.extend(
                line_no
                for line_no, content in required
                if not observed_line_matches(entry, line_no, content)
            )
            continue

        old_start = int(hunk.get("old_start", 0))
        anchor = max(1, old_start)
        if not observed_line(entry, anchor) and not observed_empty_file(entry):
            missing.append(anchor)

    if missing:
        start_line = min(missing)
        end_line = max(missing)
        return ToolResult(
            name=tool_name,
            success=False,
            output=(
                f"patch includes lines not yet inspected in {diff_path}: "
                f"{format_line_ranges(missing)}. "
                f"Read that range with read_file and retry."
            ),
            error_code="file_not_read",
            metadata={
                "path": str(path),
                "missing_lines": sorted(set(missing)),
                "read_hint": {
                    "path": diff_path,
                    "start_line": start_line,
                    "end_line": end_line,
                },
            },
        ), []

    current_data, current_sha256, current_mtime_ns = snapshot(path)
    current_lines = text_lines(current_data)
    stale_lines: list[int] = []
    relocations: list[dict[str, object]] = []
    for hunk in hunks:
        required = hunk.get("required", [])
        if required:
            old_start = required[0][0]
            contents = [content for _line_no, content in required]
            old_index = old_start - 1
            if current_lines[old_index : old_index + len(contents)] == contents:
                continue
            matches = find_line_sequence(current_lines, contents)
            if len(matches) != 1:
                stale_lines.extend(line_no for line_no, _content in required)
                continue
            new_start = matches[0]
            if new_start != old_start:
                hunk["relocation_delta"] = new_start - old_start
                relocations.append(
                    {
                        "path": diff_path,
                        "old_start": old_start,
                        "old_end": old_start + len(contents) - 1,
                        "new_start": new_start,
                        "new_end": new_start + len(contents) - 1,
                        "offset": new_start - old_start,
                    }
                )
            continue

        anchor = max(1, int(hunk.get("old_start", 0)))
        if observed_empty_file(entry):
            if current_lines:
                stale_lines.append(anchor)
        elif (
            anchor > len(current_lines)
            or not observed_line_matches(entry, anchor, current_lines[anchor - 1])
        ):
            stale_lines.append(anchor)

    if stale_lines:
        return changed_region_error(
            tool_name,
            diff_path,
            path,
            stale_lines,
            current_sha256,
            current_mtime_ns,
        ), []

    return None, relocations


def stale_read_error(
    tool_name: str,
    resolved_files: dict[str, ResolvedPath],
    hunks_by_file: dict[str, list[dict[str, object]]],
    state: dict,
) -> ToolResult | None:
    read_files = state.get("read_files", {})
    for diff_path, resolved in resolved_files.items():
        if not resolved.path.exists() or not hunks_by_file.get(diff_path):
            continue
        entry = read_files.get(str(resolved.path))
        if not entry:
            continue
        _data, current_sha256, current_mtime_ns = snapshot(resolved.path)
        if not snapshot_changed(entry, current_sha256):
            continue
        affected_lines = hunk_affected_lines(hunks_by_file[diff_path])
        start_line = min(affected_lines) if affected_lines else 1
        end_line = max(affected_lines) if affected_lines else start_line
        return changed_region_error(
            tool_name,
            diff_path,
            resolved.path,
            affected_lines,
            current_sha256,
            current_mtime_ns,
        )
    return None


def changed_region_error(
    tool_name: str,
    diff_path: str,
    path: Path,
    affected_lines: list[int],
    current_sha256: str,
    current_mtime_ns: int,
) -> ToolResult:
    start_line = min(affected_lines) if affected_lines else 1
    end_line = max(affected_lines) if affected_lines else start_line
    return ToolResult(
        name=tool_name,
        success=False,
        output=(
            f"the inspected patch region moved or changed in {diff_path}. "
            "Re-locate the intended content with search_text, read its current "
            "surrounding lines, and retry."
        ),
        error_code="file_changed_since_read",
        metadata={
            "path": str(path),
            "current_sha256": current_sha256,
            "current_mtime_ns": current_mtime_ns,
            "relocate_required": True,
            "read_hint": {
                "path": diff_path,
                "start_line": start_line,
                "end_line": end_line,
            },
        },
    )


def hunk_affected_lines(hunks: list[dict[str, object]]) -> list[int]:
    affected: list[int] = []
    for hunk in hunks:
        required = hunk.get("required", [])
        if required:
            affected.extend(line_no for line_no, _content in required)
        else:
            affected.append(max(1, int(hunk.get("old_start", 0))))
    return affected


def find_line_sequence(lines: list[str], sequence: list[str]) -> list[int]:
    if not sequence or len(sequence) > len(lines):
        return []
    width = len(sequence)
    return [
        index + 1
        for index in range(len(lines) - width + 1)
        if lines[index : index + width] == sequence
    ]


def relocate_diff_hunks(
    diff: str,
    hunks_by_file: dict[str, list[dict[str, object]]],
) -> str:
    rewritten: list[str] = []
    current_path: str | None = None
    hunk_indexes: dict[str, int] = {}
    header_pattern = re.compile(
        r"^(@@ -)(\d+)((?:,\d+)? \+)(\d+)((?:,\d+)? @@.*)$"
    )
    for line in diff.splitlines():
        if line.startswith("--- "):
            old_path = _diff_path(line[4:].strip())
            current_path = old_path
        elif line.startswith("+++ ") and current_path == "/dev/null":
            current_path = _diff_path(line[4:].strip())
        elif line.startswith("@@ ") and current_path:
            hunk_index = hunk_indexes.get(current_path, 0)
            file_hunks = hunks_by_file.get(current_path, [])
            if hunk_index < len(file_hunks):
                delta = int(file_hunks[hunk_index].get("relocation_delta", 0))
                match = header_pattern.match(line)
                if delta and match is not None:
                    old_start = int(match.group(2)) + delta
                    new_start = int(match.group(4)) + delta
                    line = (
                        f"{match.group(1)}{old_start}{match.group(3)}"
                        f"{new_start}{match.group(5)}"
                    )
            hunk_indexes[current_path] = hunk_index + 1
        rewritten.append(line)
    return "\n".join(rewritten) + ("\n" if diff.endswith("\n") else "")


def format_line_ranges(lines: list[int]) -> str:
    unique = sorted(set(lines))
    if not unique:
        return ""
    ranges: list[str] = []
    start = previous = unique[0]
    for line in unique[1:]:
        if line == previous + 1:
            previous = line
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = line
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(ranges)


def classify_git_apply_failure(output: str) -> str:
    lowered = output.lower()
    if "corrupt patch" in lowered or "unrecognized input" in lowered:
        return "patch_syntax_error"
    return "patch_context_mismatch"


def format_patch_failure(output: str, error_code: str) -> str:
    if error_code == "patch_syntax_error":
        return "patch syntax error; generate a valid unified diff and retry.\n" + output
    return output


def line_stats(diff: str) -> dict[str, int]:
    added = 0
    removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return {"added": added, "removed": removed}
