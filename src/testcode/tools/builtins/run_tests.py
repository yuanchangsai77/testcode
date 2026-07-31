from __future__ import annotations

import time
from pathlib import Path

from ...project import ProjectCommandResolver, ProjectDetector
from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import path_error, resolve_workspace_path, retarget, schema
from ..summary import run_tests_summary
from .shell_exec import run as run_shell


def tool(
    project_detector: ProjectDetector | None = None,
    command_resolver: ProjectCommandResolver | None = None,
) -> SimpleTool:
    detector = project_detector or ProjectDetector()
    resolver = command_resolver or ProjectCommandResolver()
    return SimpleTool(
        name="run_tests",
        description=(
            "Run a test command in the workspace. If command is omitted, "
            "detect one from the nearest supported project."
        ),
        arguments={
            "command": "Test command to execute, for example 'python -m pytest'.",
            "cwd": "Optional workspace-relative working directory.",
            "timeout": "Timeout in seconds. Defaults to 120.",
        },
        input_schema=schema(
            {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout": {"type": "number", "minimum": 0.001, "maximum": 3600},
            },
            required=[],
        ),
        risk_level="test",
        handler=lambda action, context: run(
            action,
            context,
            project_detector=detector,
            command_resolver=resolver,
        ),
        summarizer=run_tests_summary,
    )


def run(
    action: ToolAction,
    context: ToolContext,
    *,
    project_detector: ProjectDetector | None = None,
    command_resolver: ProjectCommandResolver | None = None,
) -> ToolResult:
    project_detector = project_detector or ProjectDetector()
    command_resolver = command_resolver or ProjectCommandResolver()
    initial_arguments = dict(action.arguments)
    initial_arguments.setdefault("timeout", 120)
    effective_action = ToolAction(name=action.name, arguments=initial_arguments)
    command_source = "explicit"
    environment_source = "explicit"
    project_root = str(
        Path(context.cwd, str(action.arguments.get("cwd", "."))).resolve()
    )

    command = action.arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        resolved_cwd = resolve_workspace_path(
            context,
            action.arguments.get("cwd", "."),
        )
        if isinstance(resolved_cwd, ToolResult):
            return retarget(resolved_cwd, action.name)
        if error := path_error(action, resolved_cwd, "directory"):
            return error

        profiles = project_detector.detect(
            resolved_cwd.path,
            boundary=Path(context.cwd).resolve(),
        )
        runnable_profiles = [
            profile for profile in profiles if profile.test_commands
        ]
        if not runnable_profiles:
            return ToolResult(
                name=action.name,
                success=False,
                output=(
                    "Could not detect a supported project test command. "
                    "Provide the command argument explicitly."
                ),
                error_code="test_command_not_detected",
                metadata={
                    "detected_markers": [profile.marker for profile in profiles],
                },
            )
        if len(runnable_profiles) > 1:
            candidates = [
                {
                    "language": profile.language,
                    "marker": profile.marker,
                    "root": profile.root,
                    "commands": profile.test_commands,
                }
                for profile in runnable_profiles
            ]
            return ToolResult(
                name=action.name,
                success=False,
                output=(
                    "Multiple project test commands are available. "
                    "Provide the command argument explicitly."
                ),
                error_code="test_command_ambiguous",
                metadata={"candidates": candidates},
            )

        resolved = command_resolver.resolve(runnable_profiles[0])
        arguments = dict(effective_action.arguments)
        arguments["command"] = resolved.command
        arguments["cwd"] = resolved.project_root
        effective_action = ToolAction(name=action.name, arguments=arguments)
        command = resolved.command
        command_source = resolved.command_source
        environment_source = resolved.environment_source
        project_root = resolved.project_root

    started = time.monotonic()
    result = run_shell(effective_action, context)
    result.name = action.name
    result.metadata["duration_seconds"] = round(time.monotonic() - started, 3)
    result.metadata["command"] = command
    result.metadata["command_source"] = command_source
    result.metadata["environment_source"] = environment_source
    result.metadata["project_root"] = project_root
    result.metadata["timeout_seconds"] = float(
        effective_action.arguments.get("timeout", 120)
    )
    result.metadata["passed"] = result.success
    if result.error_code == "timeout":
        result.output = "test command timed out\n" + result.output
    elif result.success:
        result.output = "tests passed\n" + result.output
    else:
        result.output = "tests failed\n" + result.output
    return result
