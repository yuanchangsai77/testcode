from __future__ import annotations

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import path_error, positive_int, resolve_workspace_path, retarget, run_command, schema
from ..summary import match_count_summary

MAX_SEARCH_RESULTS = 2_000
DEFAULT_SEARCH_RESULTS = 200


def tool(default_max_results: int = DEFAULT_SEARCH_RESULTS) -> SimpleTool:
    default_max_results = min(max(1, int(default_max_results)), MAX_SEARCH_RESULTS)
    return SimpleTool(
        name="search_text",
        description="Search workspace text files using ripgrep.",
        arguments={
            "query": "Text or regex pattern to search for.",
            "path": "Optional workspace-relative path to search within.",
            "max_results": f"Maximum matches to return. Defaults to {default_max_results}; hard limit {MAX_SEARCH_RESULTS}.",
        },
        input_schema=schema(
            {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "max_results": {"type": "integer"},
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
    command = [
        "rg",
        "--line-number",
        "--no-heading",
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
    lines = result_lines[:max_results]
    parsed = []
    for line in lines:
        path, line_no, snippet = split_rg_line(line)
        parsed.append({"path": path, "line": line_no, "text": snippet})
    return ToolResult(
        name=action.name,
        success=True,
        output="\n".join(lines) if lines else "no matches",
        metadata={
            "count": len(lines),
            "truncated": len(result_lines) > len(lines),
            "matches": parsed,
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
