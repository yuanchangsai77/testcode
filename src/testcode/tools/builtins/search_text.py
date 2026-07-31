from __future__ import annotations

from pathlib import Path

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import path_error, positive_int, resolve_workspace_path, retarget, run_command, schema
from ..summary import match_count_summary
from .read_state import record_observation, snapshot, text_lines

MAX_SEARCH_RESULTS = 2_000
DEFAULT_SEARCH_RESULTS = 200
MAX_CONTEXT_LINES = 20
DEFAULT_CONTEXT_LINES = 3


def tool(default_max_results: int = DEFAULT_SEARCH_RESULTS) -> SimpleTool:
    default_max_results = min(max(1, int(default_max_results)), MAX_SEARCH_RESULTS)
    return SimpleTool(
        name="search_text",
        description="Search workspace text files using ripgrep.",
        arguments={
            "query": "Text or regex pattern to search for.",
            "path": "Optional workspace-relative path to search within.",
            "max_results": f"Maximum matches to return. Defaults to {default_max_results}; hard limit {MAX_SEARCH_RESULTS}.",
            "context_lines": f"Lines of surrounding context to return. Defaults to {DEFAULT_CONTEXT_LINES}; hard limit {MAX_CONTEXT_LINES}.",
        },
        input_schema=schema(
            {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "max_results": {"type": "integer"},
                "context_lines": {"type": "integer", "minimum": 0, "maximum": MAX_CONTEXT_LINES},
            },
            required=["query"],
        ),
        handler=lambda action, context: run(action, context, default_max_results),
        summarizer=match_count_summary,
    )


def run(action: ToolAction, context: ToolContext, default_max_results: int = DEFAULT_SEARCH_RESULTS) -> ToolResult:
    target = resolve_workspace_path(context, action.arguments.get("path", "."))
    if isinstance(target, ToolResult):
        return retarget(target, action.name)
    if error := path_error(action, target):
        return error

    max_results = min(positive_int(action.arguments.get("max_results"), default_max_results), MAX_SEARCH_RESULTS)
    context_lines = min(
        max(0, int(action.arguments.get("context_lines", DEFAULT_CONTEXT_LINES))),
        MAX_CONTEXT_LINES,
    )
    command = [
        "rg",
        "--line-number",
        "--no-heading",
        "--with-filename",
        "--color",
        "never",
        "--max-count",
        str(max_results),
        str(action.arguments["query"]),
        str(target.path),
    ]
    result = run_command(command, target.root, timeout=30, shell=False, max_output_bytes=context.max_output_bytes)
    if result.metadata.get("exit_code") == 1:
        return ToolResult(name=action.name, success=True, output="no matches", metadata={"count": 0})
    if not result.success:
        return retarget(result, action.name)

    result_lines = str(result.metadata.get("stdout", "")).splitlines()
    match_lines = result_lines[:max_results]
    parsed = []
    displayed: list[str] = []
    observations: dict[Path, set[int]] = {}
    for line in match_lines:
        path, line_no, snippet = split_rg_line(line)
        parsed.append({"path": path, "line": line_no, "text": snippet})
        if line_no is None:
            displayed.append(line)
            continue
        match_path = Path(path)
        if not match_path.is_absolute():
            match_path = target.root / match_path
        try:
            match_path = match_path.resolve()
            match_path.relative_to(target.root)
        except (OSError, ValueError):
            displayed.append(line)
            continue
        observations.setdefault(match_path, set()).update(
            range(max(1, line_no - context_lines), line_no + context_lines + 1)
        )

    for match_path, requested_lines in observations.items():
        data, digest, mtime_ns = snapshot(match_path)
        file_lines = text_lines(data)
        observed = {
            line_no: file_lines[line_no - 1]
            for line_no in sorted(requested_lines)
            if line_no <= len(file_lines)
        }
        record_observation(
            context.state,
            match_path,
            sha256=digest,
            mtime_ns=mtime_ns,
            lines=observed,
            empty=not data,
        )
        displayed.extend(
            f"{match_path}:{line_no}:{content}"
            for line_no, content in observed.items()
        )
    return ToolResult(
        name=action.name,
        success=True,
        output="\n".join(displayed) if displayed else "no matches",
        metadata={
            "count": len(match_lines),
            "truncated": len(result_lines) > len(match_lines),
            "matches": parsed,
            "context_lines": context_lines,
        },
    )


def split_rg_line(line: str) -> tuple[str, int | None, str]:
    first, sep, rest = line.partition(":")
    if not sep:
        return line, None, ""
    line_no, sep, snippet = rest.partition(":")
    try:
        parsed_line = int(line_no)
    except ValueError:
        parsed_line = None
    return first, parsed_line, snippet if sep else rest
