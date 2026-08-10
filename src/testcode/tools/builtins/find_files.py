from __future__ import annotations

import fnmatch
import subprocess
import threading

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import path_error, positive_int, resolve_workspace_path, retarget, schema
from ..summary import match_count_summary

MAX_SEARCH_RESULTS = 2_000
DEFAULT_SEARCH_RESULTS = 200


def tool(default_max_results: int = DEFAULT_SEARCH_RESULTS) -> SimpleTool:
    default_max_results = min(max(1, int(default_max_results)), MAX_SEARCH_RESULTS)
    return SimpleTool(
        name="find_files",
        description="Find files in the workspace by glob pattern.",
        arguments={
            "pattern": "Glob pattern, for example '*.py' or 'src/**/*.py'.",
            "path": "Optional workspace-relative path to search within.",
            "max_results": f"Maximum files to return. Defaults to {default_max_results}; hard limit {MAX_SEARCH_RESULTS}.",
            "include_hidden": "Include hidden files and directories. Defaults to false.",
            "include_ignored": "Include files excluded by ignore files. Defaults to false.",
        },
        input_schema=schema(
            {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "max_results": {"type": "integer"},
                "include_hidden": {"type": "boolean"},
                "include_ignored": {"type": "boolean"},
            },
            required=["pattern"],
        ),
        handler=lambda action, context: run(action, context, default_max_results),
        summarizer=match_count_summary,
    )


def run(action: ToolAction, context: ToolContext, default_max_results: int = DEFAULT_SEARCH_RESULTS) -> ToolResult:
    pattern = str(action.arguments["pattern"])
    max_results = min(positive_int(action.arguments.get("max_results"), default_max_results), MAX_SEARCH_RESULTS)
    resolved = resolve_workspace_path(context, action.arguments.get("path", "."))
    if isinstance(resolved, ToolResult):
        return retarget(resolved, action.name)
    if error := path_error(action, resolved, "directory"):
        return error

    command = ["rg", "--files"]
    include_ignored = bool(action.arguments.get("include_ignored", False))
    if action.arguments.get("include_hidden", False):
        command.append("--hidden")
    if include_ignored:
        command.append("--no-ignore")
    else:
        for ignore_name in (".gitignore", ".ignore", ".rgignore"):
            ignore_path = resolved.path / ignore_name
            if ignore_path.is_file():
                command.extend(("--ignore-file", str(ignore_path)))
    discovered = _bounded_matches(
        command,
        resolved.path,
        pattern=pattern,
        max_results=max_results,
        timeout=30,
    )
    if isinstance(discovered, ToolResult):
        discovered.name = action.name
        return discovered
    matches, truncated = discovered
    matches.sort()

    return ToolResult(
        name=action.name,
        success=True,
        output="\n".join(matches) if matches else "no matches",
        metadata={
            "count": len(matches),
            "truncated": truncated,
            "include_hidden": bool(action.arguments.get("include_hidden", False)),
            "include_ignored": include_ignored,
        },
    )


def _bounded_matches(
    command: list[str],
    cwd,
    *,
    pattern: str,
    max_results: int,
    timeout: float,
) -> tuple[list[str], bool] | ToolResult:
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        return ToolResult("find_files", False, str(error), error_code="command_not_found")

    matches: list[str] = []
    stderr_parts: list[str] = []
    limit_reached = threading.Event()

    def read_stdout() -> None:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            if not line or not (
                fnmatch.fnmatch(line, pattern)
                or fnmatch.fnmatch(line.rsplit("/", 1)[-1], pattern)
            ):
                continue
            matches.append(line)
            if len(matches) > max_results:
                limit_reached.set()
                process.terminate()
                return

    def read_stderr() -> None:
        assert process.stderr is not None
        for chunk in iter(lambda: process.stderr.read(4096), ""):
            if sum(len(item) for item in stderr_parts) < 32_000:
                stderr_parts.append(chunk)

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    try:
        exit_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        return ToolResult(
            "find_files",
            False,
            "file search timed out",
            error_code="timeout",
            metadata={"timeout_seconds": timeout},
        )

    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    if exit_code not in {0, 1} and not limit_reached.is_set():
        error_text = "".join(stderr_parts).strip()
        return ToolResult(
            "find_files",
            False,
            error_text or f"file search failed with exit code {exit_code}",
            error_code="command_failed",
            metadata={"exit_code": exit_code},
        )
    return matches[:max_results], limit_reached.is_set()
