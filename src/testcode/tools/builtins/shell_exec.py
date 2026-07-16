from __future__ import annotations

import selectors
import os
import shlex
import subprocess
import time
from pathlib import Path

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import clip, format_process_output, path_error, resolve_workspace_path, retarget, schema
from ..summary import process_result_summary


def tool() -> SimpleTool:
    return SimpleTool(
        name="shell_exec",
        description="Execute a shell command in the workspace and return stdout, stderr, and exit code.",
        arguments={
            "command": "Command to execute.",
            "cwd": "Optional workspace-relative working directory.",
            "timeout": "Timeout in seconds. Defaults to 30.",
        },
        input_schema=schema(
            {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout": {"type": "number"},
            },
            required=["command"],
        ),
        risk_level="execute",
        handler=run,
        summarizer=process_result_summary,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    cwd = resolve_workspace_path(context, action.arguments.get("cwd", "."))
    if isinstance(cwd, ToolResult):
        return retarget(cwd, action.name)
    if error := path_error(action, cwd, "directory"):
        return error
    session = _shell_session(context, cwd.path)
    cd_base = cwd.path if "cwd" in action.arguments else session.cwd
    if error := _shell_cd_error(action, context, cd_base):
        return error
    timeout = float(action.arguments.get("timeout", 30))
    result = session.run(
        str(action.arguments["command"]),
        cwd.path,
        timeout=timeout,
        reset_cwd="cwd" in action.arguments,
    )
    if result.error_code == "path_outside_workspace":
        result.name = action.name
        return result
    return retarget(result, action.name)


class ShellSession:
    def __init__(self, context: ToolContext, cwd: Path) -> None:
        self.context = context
        self.cwd = cwd
        self.process = self._start(cwd)

    def run(self, command: str, cwd: Path, *, timeout: float, reset_cwd: bool = False) -> ToolResult:
        if self.process.poll() is not None:
            self.process = self._start(cwd)
            self.cwd = cwd

        if (exit_code := _standalone_exit_code(command)) is not None:
            return ToolResult(
                name="command",
                success=exit_code == 0,
                output=format_process_output("", "", exit_code),
                error_code=None if exit_code == 0 else "nonzero_exit",
                metadata={"exit_code": exit_code, "stdout": "", "stderr": "", "cwd": str(self.cwd)},
            )

        setup = ""
        if reset_cwd and cwd != self.cwd:
            quoted_cwd = shlex.quote(str(cwd))
            setup = f"cd {quoted_cwd} || exit $?\n"

        marker = f"__TESTCODE_DONE_{time.monotonic_ns()}__"
        wrapped = (
            f"{setup}{command}\n"
            "__testcode_status=$?\n"
            f"printf '\\n{marker}:%s:%s\\n' \"$__testcode_status\" \"$PWD\"\n"
        )
        assert self.process.stdin is not None
        self.process.stdin.write(wrapped.encode("utf-8"))
        self.process.stdin.flush()

        output, timed_out = self._read_until_marker(marker, timeout)
        if timed_out:
            self.close()
            self.process = self._start(cwd)
            self.cwd = cwd
            return ToolResult(
                name="command",
                success=False,
                output=format_process_output(clip(output, self.context.max_output_bytes), "", None),
                error_code="timeout",
                metadata={"timeout": timeout, "stdout": clip(output, self.context.max_output_bytes), "stderr": ""},
            )

        stdout, exit_code, reported_cwd = self._split_marker(output, marker)
        if exit_code is None or reported_cwd is None:
            self.close()
            self.process = self._start(cwd)
            self.cwd = cwd
            return ToolResult(
                name="command",
                success=False,
                output="persistent shell returned an invalid command marker",
                error_code="shell_protocol_error",
                metadata={"stdout": clip(output, self.context.max_output_bytes), "stderr": ""},
            )

        cwd_check = resolve_workspace_path(self.context, reported_cwd)
        if isinstance(cwd_check, ToolResult):
            self.close()
            self.process = self._start(cwd)
            self.cwd = cwd
            cwd_check.name = "command"
            cwd_check.output = f"persistent shell cwd escaped the authorized workspace: {reported_cwd}"
            return cwd_check

        self.cwd = cwd_check.path
        stdout = clip(stdout, self.context.max_output_bytes)
        return ToolResult(
            name="command",
            success=exit_code == 0,
            output=format_process_output(stdout, "", exit_code),
            error_code=None if exit_code == 0 else "nonzero_exit",
            metadata={"exit_code": exit_code, "stdout": stdout, "stderr": "", "cwd": str(self.cwd)},
        )

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.kill()
        self.process.wait(timeout=1)

    def _start(self, cwd: Path):
        return subprocess.Popen(
            ["bash", "--noprofile", "--norc"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )

    def _read_until_marker(self, marker: str, timeout: float) -> tuple[str, bool]:
        assert self.process.stdout is not None
        marker_bytes = marker.encode("utf-8")
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        output = b""
        try:
            while marker_bytes not in output:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return output.decode("utf-8", errors="replace"), True
                events = selector.select(remaining)
                if not events:
                    return output.decode("utf-8", errors="replace"), True
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk:
                    break
                output += chunk
        finally:
            selector.close()
        return output.decode("utf-8", errors="replace"), False

    def _split_marker(self, output: str, marker: str) -> tuple[str, int | None, str | None]:
        lines = output.splitlines()
        for index, line in enumerate(lines):
            if not line.startswith(marker + ":"):
                continue
            _, raw_code, reported_cwd = line.split(":", 2)
            try:
                exit_code = int(raw_code)
            except ValueError:
                return "\n".join(lines[:index]), None, None
            stdout = "\n".join(lines[:index])
            if stdout:
                stdout += "\n"
            return stdout, exit_code, reported_cwd
        return output, None, None


def _shell_session(context: ToolContext, cwd: Path) -> ShellSession:
    existing = context.state.get("shell_session")
    if isinstance(existing, ShellSession):
        existing.context = context
        return existing
    session = ShellSession(context, cwd)
    context.state["shell_session"] = session
    return session


def _shell_cd_error(action: ToolAction, context: ToolContext, cwd_path) -> ToolResult | None:
    command = str(action.arguments["command"])
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None

    for index, token in enumerate(tokens):
        if token != "cd":
            continue

        target = tokens[index + 1] if index + 1 < len(tokens) else "~"
        if target in {"&&", "||", ";"}:
            target = "~"
        if target == "-":
            return ToolResult(
                name=action.name,
                success=False,
                output="shell cd target is not deterministic within the workspace: cd -",
                error_code="path_outside_workspace",
                metadata={"command": command, "workspace": str(context.cwd)},
            )
        if _has_shell_expansion(target):
            return ToolResult(
                name=action.name,
                success=False,
                output=f"shell cd target is not deterministic within the workspace: cd {target}",
                error_code="path_outside_workspace",
                metadata={"command": command, "workspace": str(context.cwd)},
            )

        target_path = Path(target).expanduser()
        raw_target = str(target_path if target_path.is_absolute() else cwd_path / target)
        resolved = resolve_workspace_path(context, raw_target)
        if isinstance(resolved, ToolResult):
            resolved.name = action.name
            resolved.output = (
                f"shell command attempts to change directory outside the current workspace: cd {target}. "
                "Run the session from that project directory or pass an in-workspace cwd."
            )
            resolved.metadata["command"] = command
            return resolved

    return None


def _has_shell_expansion(value: str) -> bool:
    return any(marker in value for marker in ("$", "`"))


def _standalone_exit_code(command: str) -> int | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens or tokens[0] != "exit" or len(tokens) > 2:
        return None
    if len(tokens) == 1:
        return 0
    try:
        return int(tokens[1])
    except ValueError:
        return 2
