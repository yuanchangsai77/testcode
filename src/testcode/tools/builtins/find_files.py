from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import positive_int, resolve_workspace_path, retarget, schema
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
        },
        input_schema=schema(
            {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "max_results": {"type": "integer"},
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

    matches: list[str] = []
    for root, dirs, files in os.walk(resolved.path):
        dirs[:] = [item for item in dirs if item != ".git"]
        for filename in files:
            path = Path(root) / filename
            rel = path.relative_to(resolved.path).as_posix()
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(filename, pattern):
                matches.append(rel)
                if len(matches) >= max_results:
                    return ToolResult(
                        name=action.name,
                        success=True,
                        output="\n".join(matches),
                        metadata={"count": len(matches), "truncated": True},
                    )

    return ToolResult(
        name=action.name,
        success=True,
        output="\n".join(matches) if matches else "no matches",
        metadata={"count": len(matches), "truncated": False},
    )
