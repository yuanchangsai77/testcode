from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import select
import shutil
import signal
import sys
import termios
import threading
import time
import tty
import unicodedata
from uuid import uuid4

from .presenter import ConsolePresenter
from .terminal import Ansi
from .tui_events import TUIEvent, TUIEventKind, TUIEventQueue
from .tui_state import RunStatus, TUIState, ToolStatus, reduce_tui_state


def _display_width(value: str) -> int:
    width = 0
    for character in value:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
    return width


def _truncate_display(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if _display_width(value) <= width:
        return value
    if width == 1:
        return "…"
    result = ""
    used = 0
    for character in value:
        character_width = 0 if unicodedata.combining(character) else (
            2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
        )
        if used + character_width > width - 1:
            break
        result += character
        used += character_width
    return f"{result}…"


def _terminal_size(output=None) -> os.terminal_size:
    output = output or sys.stdout
    try:
        return os.get_terminal_size(output.fileno())
    except (AttributeError, NotImplementedError, OSError, ValueError):
        return shutil.get_terminal_size(fallback=(80, 24))


class TUIController:
    """Own the bounded event queue and the immutable runtime view state."""

    def __init__(self, max_events: int = 256) -> None:
        self.events = TUIEventQueue(max_events=max_events)
        self._state = TUIState()
        self._lock = threading.RLock()

    def publish(self, event: TUIEvent) -> None:
        self.events.publish(event)

    def drain(self) -> TUIState:
        with self._lock:
            for event in self.events.drain():
                self._state = reduce_tui_state(self._state, event)
            return self._state

    def snapshot(self) -> TUIState:
        with self._lock:
            return self._state


class TUIRenderer:
    spinner_frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    styles = {
        "class:working": f"{Ansi.BOLD}",
        "class:runtime": f"{Ansi.YELLOW}",
        "class:tool.running": f"{Ansi.YELLOW}",
        "class:tool.succeeded": f"{Ansi.GREEN}",
        "class:tool.failed": f"{Ansi.RED}",
        "class:tool.skipped": f"{Ansi.YELLOW}",
        "class:approval.title": f"{Ansi.YELLOW}{Ansi.BOLD}",
        "class:approval": "",
        "class:approval.choice": f"{Ansi.BOLD}",
        "class:approval.hint": f"{Ansi.GRAY}",
    }

    def render(self, state: TUIState, *, now: float | None = None) -> str:
        return "\n".join(text for _style, text in self.render_rows(state, now=now))

    def render_rows(
        self,
        state: TUIState,
        *,
        now: float | None = None,
        include_runtime: bool = True,
    ) -> list[tuple[str, str]]:
        now = time.monotonic() if now is None else now
        width = max(1, state.terminal_width)
        rows: list[tuple[str, str]] = []

        for tool in state.tools[-6:]:
            style = {
                ToolStatus.RUNNING: "class:tool.running",
                ToolStatus.SUCCEEDED: "class:tool.succeeded",
                ToolStatus.FAILED: "class:tool.failed",
                ToolStatus.ABORTED: "class:tool.failed",
                ToolStatus.SKIPPED: "class:tool.skipped",
            }[tool.status]
            rows.append((style, self._fit(f" • {tool.name} → {tool.summary}", width)))

        if state.approval is not None:
            approval = state.approval
            rows.extend(
                [
                    (
                        "class:approval.title",
                        self._fit(f" Permission required: {approval.action_name}", width),
                    ),
                    ("class:approval", self._fit(f" {approval.reason}", width)),
                ]
            )
            if approval.arguments:
                rows.append(("class:approval", self._fit(f" {approval.arguments}", width)))
            yes = "› Yes" if approval.selected == 0 else "  Yes"
            no = "› No" if approval.selected == 1 else "  No"
            rows.extend(
                [
                    ("class:approval.choice", self._fit(f" {yes}", width)),
                    ("class:approval.choice", self._fit(f" {no}", width)),
                    (
                        "class:approval.hint",
                        self._fit(" ↑/↓ to select · enter to confirm · esc to deny", width),
                    ),
                ]
            )
            return rows

        elapsed = state.elapsed(now)
        if state.run_status == RunStatus.CANCELLING:
            activity = "Cancelling"
        elif state.model_status:
            activity = state.model_status.rstrip("…")
        else:
            activity = "Working"
        spinner = self.spinner_frames[int(now * 10) % len(self.spinner_frames)]
        rows.append(
            (
                "class:working",
                self._fit(
                    f" {spinner} {activity} ({self._duration(elapsed)} • esc to interrupt)",
                    width,
                ),
            )
        )
        if include_runtime:
            runtime = self.runtime_row(state)
            if runtime is not None:
                rows.append(runtime)
        return rows

    def runtime_row(self, state: TUIState) -> tuple[str, str] | None:
        runtime = " · ".join(part for part in (state.model_name, state.cwd) if part)
        if not runtime:
            return None
        return "class:runtime", self._fit(f"  {runtime}", max(1, state.terminal_width))

    def ansi_row(self, style: str, text: str) -> str:
        prefix = self.styles.get(style, "")
        return f"{prefix}{text}{Ansi.RESET}" if prefix else text

    @staticmethod
    def _duration(seconds: float) -> str:
        total = max(0, int(seconds))
        minutes, seconds = divmod(total, 60)
        return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"

    @staticmethod
    def _fit(value: str, width: int) -> str:
        safe_width = max(width - 1, 1)
        if _display_width(value) <= safe_width:
            return value
        if safe_width <= 1:
            return "…"
        kept: list[str] = []
        used = 0
        for character in value:
            character_width = max(_display_width(character), 0)
            if used + character_width > safe_width - 1:
                break
            kept.append(character)
            used += character_width
        return "".join(kept) + "…"


from .commands import SlashCommandRegistry, default_slash_command_registry


@dataclass
class ComposerState:
    value: str = ""
    cursor: int = 0
    history_index: int | None = None
    saved_value: str = ""
    completion_index: int = 0
    completion_dismissed: bool = False
    command_registry: SlashCommandRegistry | None = None
    command_context: object | None = None

    def set_value(self, value: str) -> None:
        self.value = value
        self.cursor = len(value)
        self.history_index = None
        self.saved_value = ""
        self.completion_index = 0
        self.completion_dismissed = False

    def insert(self, value: str) -> None:
        self.value = self.value[: self.cursor] + value + self.value[self.cursor :]
        self.cursor += len(value)
        self.history_index = None
        self.completion_index = 0
        self.completion_dismissed = False

    def get_completion_matches(self) -> list[tuple[str, str]]:
        if self.completion_dismissed or not self.value.startswith("/"):
            return []
        registry = self.command_registry or default_slash_command_registry()
        return registry.get_completions(self.value, self.command_context)


    def edit(self, key: str, history: list[str]) -> str | None:
        matches = self.get_completion_matches()
        if matches:
            if self.completion_index >= len(matches):
                self.completion_index = 0
            if key in {"\x1b[A", "\x1bOA"}:
                self.completion_index = (self.completion_index - 1) % len(matches)
                return "changed"
            if key in {"\x1b[B", "\x1bOB"}:
                self.completion_index = (self.completion_index + 1) % len(matches)
                return "changed"
            if key == "\t":
                selected_cmd = matches[self.completion_index][0]
                self.value = selected_cmd + " "
                self.cursor = len(self.value)
                self.completion_index = 0
                return "changed"
            if key in {"\r", "\n"}:
                selected_cmd = matches[self.completion_index][0]
                if self.value != selected_cmd:
                    self.value = selected_cmd
                    self.cursor = len(self.value)
                    self.completion_index = 0
                    if self._advance_to_argument_options(selected_cmd):
                        return "changed"
                    return "changed"
                if self._advance_to_argument_options(selected_cmd):
                    return "changed"
                return "submit"
            if key == "\x1b":
                self.completion_dismissed = True
                return "changed"

        if key in {"\r", "\n"}:
            return "submit"
        if key in {"\x1b\r", "\x1b\n"}:
            self.insert("\n")
            return "changed"
        if key in {"\x7f", "\b"}:
            if self.cursor:
                self.value = self.value[: self.cursor - 1] + self.value[self.cursor :]
                self.cursor -= 1
                self.history_index = None
                self.completion_index = 0
                self.completion_dismissed = False
                return "changed"
            return None
        if key == "\x04":
            if not self.value:
                return "eof"
            if self.cursor < len(self.value):
                self.value = self.value[: self.cursor] + self.value[self.cursor + 1 :]
                self.completion_index = 0
                self.completion_dismissed = False
                return "changed"
            return None
        if key in {"\x1b[D", "\x1bOD"}:
            if self.cursor:
                self.cursor -= 1
                return "changed"
            return None
        if key in {"\x1b[C", "\x1bOC"}:
            if self.cursor < len(self.value):
                self.cursor += 1
                return "changed"
            return None
        if key in {"\x1b[H", "\x1bOH", "\x01"}:
            self.cursor = 0
            return "changed"
        if key in {"\x1b[F", "\x1bOF", "\x05"}:
            self.cursor = len(self.value)
            return "changed"
        if key in {"\x1b[A", "\x1bOA"}:
            return self._move_history(history, -1)
        if key in {"\x1b[B", "\x1bOB"}:
            return self._move_history(history, 1)
        if key and not key.startswith("\x1b") and all(
            character.isprintable() or character in {"\n", "\t"}
            for character in key
        ):
            self.insert(key)
            return "changed"
        return None

    def _advance_to_argument_options(self, selected: str) -> bool:
        parts = selected.split()
        if not parts:
            return False
        should_advance = len(parts) == 1 or (
            len(parts) == 2
            and parts[0] == "/capabilities"
            and parts[1] in {"open", "activate"}
        ) or (
            len(parts) == 3
            and parts[:2] == ["/capabilities", "activate"]
            and parts[2].startswith("--scope=")
        )
        if not should_advance:
            return False
        registry = self.command_registry or default_slash_command_registry()
        next_value = f"{selected} "
        if not registry.get_completions(next_value, self.command_context):
            return False
        self.value = next_value
        self.cursor = len(self.value)
        self.completion_index = 0
        self.completion_dismissed = False
        return True

    def _move_history(self, history: list[str], direction: int) -> str | None:
        if not history:
            return None
        if self.history_index is None:
            if direction > 0:
                return None
            self.saved_value = self.value
            self.history_index = len(history) - 1
        else:
            next_index = self.history_index + direction
            if next_index >= len(history):
                self.set_value(self.saved_value)
                return "changed"
            self.history_index = max(next_index, 0)
        self.value = history[self.history_index]
        self.cursor = len(self.value)
        return "changed"



class InlineTerminalSurface:
    """Redraw the transient tail directly after committed scrollback output."""

    def __init__(self, output=None) -> None:
        self.output = output or sys.stdout
        self._lock = threading.RLock()
        self._active = False
        self._rows: list[str] = []
        self._cursor_row = 0
        self._cursor_column = 0

    def render(self, rows: list[str], *, cursor_row: int, cursor_column: int) -> None:
        with self._lock:
            self._clear_locked()
            if not rows:
                return
            self.output.write("\033[?25l")
            self.output.write("\n".join(rows))
            rows_up = len(rows) - 1 - cursor_row
            self.output.write("\r")
            if rows_up:
                self.output.write(f"\033[{rows_up}A")
            if cursor_column:
                self.output.write(f"\033[{cursor_column}C")
            self.output.write("\033[?25h")
            self.output.flush()
            self._active = True
            self._rows = list(rows)
            self._cursor_row = cursor_row
            self._cursor_column = cursor_column

    def clear(self) -> None:
        with self._lock:
            self._clear_locked()
            self.output.flush()

    def _clear_locked(self) -> None:
        if not self._active:
            return
        size = _terminal_size(self.output)
        visual_cursor_row = self._cursor_visual_row(size.columns)
        self.output.write("\r")
        if visual_cursor_row:
            self.output.write(f"\033[{visual_cursor_row}A")
        self.output.write("\r\033[J")
        self._active = False
        self._rows = []
        self._cursor_row = 0
        self._cursor_column = 0

    def _cursor_visual_row(self, columns: int) -> int:
        columns = max(columns, 1)
        rows_before = self._visual_height(self._rows[: self._cursor_row], columns)
        return rows_before + self._cursor_column // columns

    @staticmethod
    def _visual_height(rows: list[str], columns: int) -> int:
        columns = max(columns, 1)
        height = 0
        for row in rows:
            plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", row)
            width = max(_display_width(plain), 1)
            height += max((width + columns - 1) // columns, 1)
        return height


class TUIConsolePresenter(ConsolePresenter):
    """Normal-screen TUI with native scrollback and a transient inline tail."""

    manages_runtime_display = True
    refresh_interval = 0.1

    def __init__(self, tool_result_summarizer=None, *, input=None, output=None) -> None:
        super().__init__(tool_result_summarizer=tool_result_summarizer)
        self.controller = TUIController()
        self.renderer = TUIRenderer()
        self._input = input
        self._output = output or sys.stdout
        self._surface = InlineTerminalSurface(self._output)
        self._history: list[str] = []
        self._composer = ComposerState()
        self._composer_draft = ""
        self._queued_prompt: str | None = None
        self._pending_prompt = ""
        self._pending_worked_seconds: float | None = None
        self._cwd = os.getcwd()
        self._runtime_active = False
        self._runtime_stop = threading.Event()
        self._runtime_render_thread: threading.Thread | None = None
        self._runtime_input_thread: threading.Thread | None = None
        self._terminal_settings = None
        self._approval_waiters: dict[str, tuple[threading.Event, list[bool]]] = {}
        self._approval_lock = threading.Lock()
        self._flushed_tool_ids: set[str] = set()

    def clear_screen(self) -> None:
        """Clear the terminal and redraw the TUI idle frame."""
        self._surface.clear()
        self._output.write("\033[H\033[2J\033[3J")
        self._output.flush()
        if getattr(self, "_session", None) is not None:
            super().show_session_state(self._session, resumed=getattr(self, "_resumed", False), engine=getattr(self, "_engine", None))
        engine = getattr(self, "_engine", None)
        if self._can_use_native_input():
            self._render_idle(engine=engine)

    def _print(self, value: str = "") -> None:
        self._surface.clear()
        print(value, file=self._output)

    def _print_many(self, values: list[str]) -> None:
        self._surface.clear()
        for value in values:
            print(value, file=self._output)

    def show_session_state(self, session, resumed: bool, engine=None) -> None:
        self._cwd = session.cwd
        super().show_session_state(session, resumed=resumed, engine=engine)

    def show_user_prompt(self, prompt: str) -> None:
        lines = self.prompt_box.wrap_prompt_value(prompt)
        prompt_lbl = f" {Ansi.CYAN}testcode>{Ansi.RESET}"
        background = "\033[48;5;236m"
        blank = f"{background}\033[K{Ansi.RESET}"
        self._print(blank)
        self._print(f"{background}{prompt_lbl}{background} {lines[0]}\033[K{Ansi.RESET}")
        for line in lines[1:]:
            self._print(f"{background}  {line}\033[K{Ansi.RESET}")
        self._print(blank)
        self._print()

    def show_start(self, request) -> None:
        self._pending_prompt = request.prompt
        self._cwd = request.cwd
        lines = self.prompt_box.wrap_prompt_value(request.prompt)
        prompt = f" {Ansi.CYAN}testcode>{Ansi.RESET}"
        background = "\033[48;5;236m"
        blank = f"{background}\033[K{Ansi.RESET}"
        self._print(blank)
        self._print(f"{background}{prompt}{background} {lines[0]}\033[K{Ansi.RESET}")
        for line in lines[1:]:
            self._print(f"{background}  {line}\033[K{Ansi.RESET}")
        self._print(blank)
        self._print()

    def prompt_input(self, engine=None) -> str:
        self._show_worked_separator()
        if self._queued_prompt is not None:
            prompt = self._queued_prompt
            self._queued_prompt = None
            return prompt
        if not self._can_use_native_input():
            return super().prompt_input(engine=engine)

        self._composer.set_value(self._composer_draft)
        self._composer_draft = ""
        try:
            with self._raw_input():
                last_size: os.terminal_size | None = None
                while True:
                    size = _terminal_size(self._output)
                    if size != last_size:
                        self._render_idle(engine)
                        last_size = size
                    if not self._input_ready(timeout=0.1):
                        continue
                    key = self._read_key()
                    result = self._composer.edit(key, self._history)
                    if key == "\x03":
                        raise KeyboardInterrupt
                    if result == "eof":
                        raise EOFError
                    if result == "submit":
                        value = self._composer.value.strip()
                        self._surface.clear()
                        if value:
                            self._history.append(value)
                        return value
                    if result == "changed":
                        self._render_idle(engine)
                        last_size = _terminal_size(self._output)
        finally:
            self._surface.clear()

    def show_status_bar(self, engine=None, active_tasks_count=0, is_running=False, left_override=None) -> None:
        if not is_running:
            return super().show_status_bar(
                engine=engine,
                active_tasks_count=active_tasks_count,
                is_running=is_running,
                left_override=left_override,
            )
        model_name = getattr(getattr(engine, "model", None), "model", "StubModel")
        self.controller.publish(
            TUIEvent(
                TUIEventKind.RUN_STARTED,
                payload={
                    "prompt": self._pending_prompt,
                    "model_name": model_name,
                    "cwd": self._cwd,
                },
            )
        )
        self._start_runtime()

    def clear_running_status_bar(self, tools_count: int) -> None:
        self._stop_runtime(TUIEventKind.RUN_FINISHED)
        self._flush_tool_transcript()

    def show_interrupted(self) -> None:
        self._stop_runtime(TUIEventKind.RUN_FAILED)
        super().show_interrupted()

    def model_started(self, message: str = "Model is thinking…") -> str:
        handle = f"model-{uuid4().hex}"
        self._publish(TUIEvent(TUIEventKind.MODEL_STARTED, entity_id=handle, payload={"message": message}))
        return handle


    def model_finished(self, handle: str) -> None:
        self._publish(TUIEvent(TUIEventKind.MODEL_FINISHED, entity_id=handle))

    def model_retrying(self, handle, retry, max_retries, status, delay_seconds) -> None:
        msg = f"{status} — retrying {retry}/{max_retries} in {delay_seconds:g}s…" if delay_seconds > 0 else f"{status} — retrying {retry}/{max_retries}…"
        self._publish(
            TUIEvent(
                TUIEventKind.MODEL_RETRYING,
                entity_id=handle,
                payload={
                    "message": msg
                },
            )
        )

    def tool_started(self, action_name: str) -> str:
        handle = f"tool-{uuid4().hex}"
        self._publish(
            TUIEvent(TUIEventKind.TOOL_STARTED, entity_id=handle, payload={"name": action_name})
        )
        return handle

    def tool_finished(self, handle: str, action, result) -> None:
        self._publish(
            TUIEvent(
                TUIEventKind.TOOL_FINISHED,
                entity_id=handle,
                payload={
                    "name": action.name,
                    "success": result.success,
                    "summary": self._summarize_tool_result(result),
                },
            )
        )

    def tool_aborted(self, handle: str) -> None:
        self._publish(
            TUIEvent(
                TUIEventKind.TOOL_ABORTED,
                entity_id=handle,
                payload={"summary": "aborted"},
            )
        )

    def tool_skipped(self, action, reason: str) -> None:
        self._publish(
            TUIEvent(
                TUIEventKind.TOOL_SKIPPED,
                entity_id=f"tool-{uuid4().hex}",
                payload={"name": action.name, "summary": reason},
            )
        )

    def confirm_tool_action(self, action, reason: str) -> bool:
        if not self._runtime_active:
            return super().confirm_tool_action(action, reason)
        approval_id = f"approval-{uuid4().hex}"
        completed = threading.Event()
        result: list[bool] = []
        with self._approval_lock:
            self._approval_waiters[approval_id] = completed, result
        arguments = json.dumps(action.arguments, ensure_ascii=False) if action.arguments else ""
        self._publish(
            TUIEvent(
                TUIEventKind.APPROVAL_REQUESTED,
                entity_id=approval_id,
                payload={
                    "action_name": action.name,
                    "reason": reason,
                    "arguments": arguments,
                },
            )
        )
        while not completed.wait(0.1):
            if not self._runtime_active:
                with self._approval_lock:
                    self._approval_waiters.pop(approval_id, None)
                return False
        return bool(result and result[0])

    def _start_runtime(self) -> None:
        if self._runtime_active:
            return
        self._runtime_active = True
        self._runtime_stop.clear()
        self._composer.set_value(self._composer_draft)
        self._runtime_render_thread = threading.Thread(
            target=self._runtime_render_loop,
            name="testcode-tui-render",
            daemon=True,
        )
        self._runtime_render_thread.start()
        if self._can_use_native_input():
            self._runtime_input_thread = threading.Thread(
                target=self._runtime_input_loop,
                name="testcode-tui-input",
                daemon=True,
            )
            self._runtime_input_thread.start()

    def _stop_runtime(self, outcome: TUIEventKind) -> None:
        if not self._runtime_active:
            return
        state = self.controller.drain()
        if state.run_started_at is not None:
            self._pending_worked_seconds = state.elapsed()
        if self._queued_prompt is None:
            self._composer_draft = self._composer.value
        self.controller.publish(TUIEvent(outcome))
        self._runtime_active = False
        self._runtime_stop.set()
        for thread in (self._runtime_input_thread, self._runtime_render_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)
        self._restore_terminal()
        self._surface.clear()
        self.controller.drain()
        self._runtime_input_thread = None
        self._runtime_render_thread = None
        self._deny_all_approvals()

    def _runtime_render_loop(self) -> None:
        while not self._runtime_stop.is_set():
            self._render_runtime()
            self._runtime_stop.wait(self.refresh_interval)

    def _runtime_input_loop(self) -> None:
        try:
            with self._raw_input():
                while not self._runtime_stop.is_set():
                    if not self._input_ready(timeout=0.05):
                        continue
                    key = self._read_key()
                    state = self.controller.drain()
                    if state.approval is not None:
                        self._handle_approval_key(key, state)
                        continue
                    if key in {"\x03", "\x1b"}:
                        self._publish(TUIEvent(TUIEventKind.RUN_CANCELLING))
                        os.kill(os.getpid(), signal.SIGINT)
                        return
                    result = self._composer.edit(key, self._history)
                    if result == "submit" and self._submit_runtime_prompt(state):
                        return
        finally:
            self._restore_terminal()

    def _handle_approval_key(self, key: str, state: TUIState) -> None:
        approval = state.approval
        if approval is None:
            return
        if key in {"\x03", "\x1b", "n", "N"}:
            self._resolve_approval(approval.approval_id, False)
        elif key in {"y", "Y"}:
            self._resolve_approval(approval.approval_id, True)
        elif key in {"\r", "\n"}:
            self._resolve_approval(approval.approval_id, approval.selected == 0)
        elif key in {"\x1b[A", "\x1bOA", "\x1b[D", "\x1bOD"}:
            self._select_approval(approval.approval_id, approval.selected - 1)
        elif key in {"\x1b[B", "\x1bOB", "\x1b[C", "\x1bOC"}:
            self._select_approval(approval.approval_id, approval.selected + 1)

    def _submit_runtime_prompt(self, state: TUIState | None = None) -> bool:
        state = state or self.controller.drain()
        prompt = self._composer.value.strip()
        if state.run_status != RunStatus.WORKING or not prompt:
            return False
        self._queued_prompt = prompt
        self._composer_draft = ""
        self._publish(TUIEvent(TUIEventKind.RUN_CANCELLING))
        os.kill(os.getpid(), signal.SIGINT)
        return True

    def _render_idle(self, engine=None) -> None:
        size = _terminal_size(self._output)
        composer_rows, cursor_row, cursor_column = self._composer_rows(size.columns)
        blank = f"\033[48;5;236m\033[K{Ansi.RESET}"
        status = self.status_bar.text(engine=engine, is_running=False)
        matches = self._composer.get_completion_matches()
        completion_rows = self._completion_rows(matches)
        rows = [blank, *composer_rows, blank, *completion_rows, f"{Ansi.GRAY}{status}{Ansi.RESET}"]
        self._surface.render(rows, cursor_row=1 + cursor_row, cursor_column=cursor_column)

    def _render_runtime(self) -> None:
        size = _terminal_size(self._output)
        self.controller.publish(
            TUIEvent(
                TUIEventKind.RESIZED,
                payload={"width": size.columns, "height": size.lines},
            )
        )
        state = self.controller.drain()
        rows, cursor_row, cursor_column = self._runtime_frame(state, size.columns)
        self._surface.render(
            rows,
            cursor_row=cursor_row,
            cursor_column=cursor_column,
        )

    def _runtime_frame(
        self,
        state: TUIState,
        columns: int,
    ) -> tuple[list[str], int, int]:
        activity = [
            self.renderer.ansi_row(style, text)
            for style, text in self.renderer.render_rows(state, include_runtime=False)
        ]
        composer_rows, cursor_row, cursor_column = self._composer_rows(columns)
        blank = f"\033[48;5;236m\033[K{Ansi.RESET}"
        runtime = self.renderer.runtime_row(state)
        metadata = "" if runtime is None else self.renderer.ansi_row(*runtime)
        matches = self._composer.get_completion_matches()
        completion_rows = self._completion_rows(matches)
        rows = [*activity, "", blank, *composer_rows, blank, *completion_rows, metadata]
        return (
            rows,
            len(activity) + 2 + cursor_row,
            cursor_column,
        )

    def _composer_rows(self, columns: int) -> tuple[list[str], int, int]:
        prompt = " testcode> "
        plain_lines, cursor_row, cursor_column, start = self._wrap_composer(columns)
        rows: list[str] = []
        background = "\033[48;5;236m"
        for index, line in enumerate(plain_lines):
            real_index = start + index
            prefix = (
                f"{Ansi.CYAN}{prompt}{Ansi.RESET}{background}"
                if real_index == 0
                else "  "
            )
            rows.append(f"{background}{prefix}{line}\033[K{Ansi.RESET}")
        real_cursor_row = start + cursor_row
        if real_cursor_row == 0:
            cursor_column += _display_width(prompt)
        else:
            cursor_column += 2
        return rows, cursor_row, cursor_column

    def _completion_rows(self, matches: list[tuple[str, str]], max_visible: int = 5) -> list[str]:
        if not matches:
            return []

        total = len(matches)
        selected_idx = min(self._composer.completion_index, total - 1)

        window_start = 0
        if total > max_visible:
            if selected_idx >= max_visible:
                window_start = min(selected_idx - max_visible + 1, total - max_visible)
            window_start = max(0, window_start)

        visible_matches = matches[window_start : window_start + max_visible]

        menu_name = "Options" if " " in self._composer.value else "Commands"
        header = f"  {Ansi.GRAY}{menu_name} ({total} total):{Ansi.RESET}"
        if total > max_visible:
            header += f" {Ansi.GRAY}({window_start + 1}-{window_start + len(visible_matches)} of {total}){Ansi.RESET}"
        rows: list[str] = [header]

        if window_start > 0:
            rows.append(f"   {Ansi.GRAY}▲ ({window_start} more above){Ansi.RESET}")

        display_matches = [
            (cmd.rsplit(" ", 1)[-1] if menu_name == "Options" else cmd, desc)
            for cmd, desc in matches
        ]
        visible_display_matches = display_matches[
            window_start : window_start + max_visible
        ]
        columns = max(_terminal_size(self._output).columns, 1)
        natural_cmd_width = max(_display_width(cmd) for cmd, _ in display_matches)
        max_cmd_width = max(columns - 5 - 2 - 16 - 1, 8)
        cmd_width = min(natural_cmd_width, max_cmd_width)
        description_width = max(columns - 5 - cmd_width - 2 - 1, 0)
        for rel_idx, (cmd, desc) in enumerate(visible_display_matches):
            actual_idx = window_start + rel_idx
            is_selected = actual_idx == selected_idx
            pointer = "›" if is_selected else " "
            cmd = _truncate_display(cmd, cmd_width)
            cmd_padding = " " * max(cmd_width - _display_width(cmd), 0)
            desc = _truncate_display(desc, description_width)
            if is_selected:
                cmd_str = f"{Ansi.CYAN}{Ansi.BOLD}{cmd}{cmd_padding}{Ansi.RESET}"
                desc_str = f"{Ansi.YELLOW}{desc}{Ansi.RESET}"
                pointer_str = f"{Ansi.CYAN}{Ansi.BOLD}{pointer}{Ansi.RESET}"
            else:
                cmd_str = f"{Ansi.GRAY}{cmd}{cmd_padding}{Ansi.RESET}"
                desc_str = f"{Ansi.GRAY}{desc}{Ansi.RESET}"
                pointer_str = f"{Ansi.GRAY}{pointer}{Ansi.RESET}"
            separator = "  " if desc else ""
            rows.append(f"   {pointer_str} {cmd_str}{separator}{desc_str}")

        remaining_below = total - (window_start + len(visible_matches))
        if remaining_below > 0:
            rows.append(f"   {Ansi.GRAY}▼ ({remaining_below} more below){Ansi.RESET}")

        return rows




    def _wrap_composer(self, columns: int) -> tuple[list[str], int, int, int]:
        columns = max(columns, 2)
        prompt_width = _display_width(" testcode> ")
        lines = [""]
        positions: list[tuple[int, int]] = [(0, 0)]
        row = 0
        used = 0
        limit = max(columns - prompt_width - 1, 1)
        for character in self._composer.value:
            if character == "\n":
                row += 1
                lines.append("")
                used = 0
                limit = max(columns - 3, 1)
                positions.append((row, used))
                continue
            width = max(_display_width(character), 1)
            if used + width > limit and lines[-1]:
                row += 1
                lines.append("")
                used = 0
                limit = max(columns - 3, 1)
            lines[-1] += character
            used += width
            positions.append((row, used))
        cursor_index = min(self._composer.cursor, len(positions) - 1)
        cursor_row, cursor_column = positions[cursor_index]
        start = 0
        max_rows = 6
        if len(lines) > max_rows:
            start = max(min(cursor_row - (max_rows - 1), len(lines) - max_rows), 0)
            lines = lines[start : start + max_rows]
            cursor_row -= start
        return lines, cursor_row, cursor_column, start

    def _show_worked_separator(self) -> None:
        if self._pending_worked_seconds is None:
            return
        columns = max(_terminal_size(self._output).columns, 1)
        label = f"─ Worked for {self.renderer._duration(self._pending_worked_seconds)} "
        line = label + "─" * max(columns - _display_width(label), 0)
        self._print(f"{Ansi.GRAY}{line}{Ansi.RESET}")
        self._print()
        self._pending_worked_seconds = None

    def _flush_tool_transcript(self) -> None:
        state = self.controller.snapshot()
        flushed_any = False
        for tool in state.tools:
            if tool.tool_id in self._flushed_tool_ids:
                continue
            style = {
                ToolStatus.RUNNING: "class:tool.running",
                ToolStatus.SUCCEEDED: "class:tool.succeeded",
                ToolStatus.FAILED: "class:tool.failed",
                ToolStatus.ABORTED: "class:tool.failed",
                ToolStatus.SKIPPED: "class:tool.skipped",
            }[tool.status]
            self._print(self.renderer.ansi_row(style, f" • {tool.name} → {tool.summary}"))
            self._flushed_tool_ids.add(tool.tool_id)
            flushed_any = True
        if flushed_any:
            self._print()

    def _publish(self, event: TUIEvent) -> None:
        self.controller.publish(event)

    def _select_approval(self, approval_id: str, selected: int) -> None:
        self._publish(
            TUIEvent(
                TUIEventKind.APPROVAL_SELECTED,
                entity_id=approval_id,
                payload={"selected": selected},
            )
        )

    def _resolve_approval(self, approval_id: str, approved: bool) -> None:
        with self._approval_lock:
            waiter = self._approval_waiters.pop(approval_id, None)
        self._publish(TUIEvent(TUIEventKind.APPROVAL_RESOLVED, entity_id=approval_id))
        if waiter is not None:
            completed, result = waiter
            result.append(approved)
            completed.set()

    def _deny_all_approvals(self) -> None:
        with self._approval_lock:
            waiters = list(self._approval_waiters.values())
            self._approval_waiters.clear()
        for completed, result in waiters:
            result.append(False)
            completed.set()

    def _can_use_native_input(self) -> bool:
        if self._input is not None:
            return True
        return sys.stdin.isatty() and self._output.isatty()

    def _input_fd(self) -> int:
        source = self._input or sys.stdin
        return source if isinstance(source, int) else source.fileno()

    def _input_ready(self, timeout: float | None = None) -> bool:
        readable, _, _ = select.select([self._input_fd()], [], [], timeout)
        return bool(readable)

    class _RawInputContext:
        def __init__(self, presenter: "TUIConsolePresenter") -> None:
            self.presenter = presenter
            self.settings = None

        def __enter__(self):
            fd = self.presenter._input_fd()
            try:
                self.settings = termios.tcgetattr(fd)
                tty.setcbreak(fd)
                self.presenter._terminal_settings = (fd, self.settings)
            except (OSError, termios.error):
                self.settings = None
            self.presenter._output.write("\033[?2004h")
            self.presenter._output.flush()
            return self

        def __exit__(self, _type, _value, _traceback):
            self.presenter._output.write("\033[?2004l")
            self.presenter._output.flush()
            self.presenter._restore_terminal()

    def _raw_input(self):
        return self._RawInputContext(self)

    def _restore_terminal(self) -> None:
        settings = self._terminal_settings
        if settings is None:
            return
        fd, previous = settings
        self._terminal_settings = None
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        except (OSError, termios.error):
            pass

    def _read_key(self) -> str:
        fd = self._input_fd()
        raw = os.read(fd, 1)
        if raw != b"\x1b":
            while True:
                try:
                    return raw.decode()
                except UnicodeDecodeError as error:
                    if error.reason != "unexpected end of data":
                        return raw.decode(errors="replace")
                    raw += os.read(fd, 1)

        sequence = "\x1b"
        for _ in range(32):
            readable, _, _ = select.select([fd], [], [], 0.02)
            if not readable:
                break
            character = os.read(fd, 1).decode(errors="ignore")
            sequence += character
            if len(sequence) == 2 and character not in {"[", "O"}:
                break
            if len(sequence) >= 3 and "@" <= character <= "~":
                break
        if sequence == "\x1b[200~":
            return self._read_paste(fd)
        return sequence

    @staticmethod
    def _read_paste(fd: int) -> str:
        terminator = b"\x1b[201~"
        payload = bytearray()
        while not payload.endswith(terminator):
            chunk = os.read(fd, 1)
            if not chunk:
                break
            payload.extend(chunk)
        if payload.endswith(terminator):
            del payload[-len(terminator) :]
        return payload.decode(errors="replace")


def should_use_tui() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
