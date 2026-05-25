from __future__ import annotations

import time

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import schema
from ..summary import run_tests_summary
from .shell_exec import run as run_shell


def tool() -> SimpleTool:
    return SimpleTool(
        name="run_tests",
        description="Run a test command in the workspace.",
        arguments={
            "command": "Test command to execute, for example 'python -m pytest'.",
            "cwd": "Optional workspace-relative working directory.",
            "timeout": "Timeout in seconds. Defaults to 120.",
        },
        input_schema=schema(
            {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout": {"type": "number"},
            },
            required=["command"],
        ),
        risk_level="test",
        handler=run,
        summarizer=run_tests_summary,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    started = time.monotonic()
    result = run_shell(action, context)
    result.name = action.name
    result.metadata["duration_seconds"] = round(time.monotonic() - started, 3)
    result.metadata["command"] = action.arguments["command"]
    result.metadata["passed"] = result.success
    if result.error_code == "timeout":
        result.output = "test command timed out\n" + result.output
    elif result.success:
        result.output = "tests passed\n" + result.output
    else:
        result.output = "tests failed\n" + result.output
    return result
