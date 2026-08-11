from __future__ import annotations

import os
import select
import signal
import sys
import termios
import time
import tty
import unicodedata
from collections.abc import Callable
from math import ceil

from .terminal import colored_border, stable_terminal_line, terminal_columns


class StatusBar:
    def __init__(self) -> None:
        self.running_visible = False

    def show(self, engine=None, active_tasks_count=0, is_running=False, left_override=None) -> None:
        print(self.render_line(engine, active_tasks_count, is_running, left_override))

    def render_line(self, engine=None, active_tasks_count=0, is_running=False, left_override=None) -> str:
        gray = "\033[90m"
        reset = "\033[0m"

        line = f"{gray}{self.text(engine, active_tasks_count, is_running, left_override)}{reset}"
        return stable_terminal_line(line)

    def text(self, engine=None, active_tasks_count=0, is_running=False, left_override=None) -> str:

        if is_running or left_override == "esc to cancel":
            self.running_visible = True

        if left_override:
            left_text = left_override
        elif is_running:
            left_text = "esc to interrupted"
        else:
            left_text = "? for shortcuts"

        model_name = "StubModel"
        if engine and hasattr(engine, "model"):
            model_name = getattr(engine.model, "model", "StubModel")

        if not is_running and active_tasks_count > 0:
            right_text = f"{model_name} · {active_tasks_count} task(s) · /tasks"
        else:
            right_text = f"{model_name}"

        columns = max(terminal_columns(), 1)
        # Keep the status bar on one terminal row in a narrow terminal.
        left_text = self._fit_text(left_text, max(columns - 1, 0))
        right_text = self._fit_text(right_text, max(columns - len(left_text) - 1, 0))
        padding = max(columns - len(left_text) - len(right_text) - 1, 0)

        return f" {left_text}{' ' * padding}{right_text}"

    @staticmethod
    def _fit_text(value: str, width: int) -> str:
        if width <= 0:
            return ""
        if len(value) <= width:
            return value
        if width == 1:
            return "…"
        return f"{value[:width - 1]}…"

    def clear_running(self, tools_count: int) -> None:
        if self.running_visible and sys.stdout.isatty():
            lines_up = tools_count + 1
            if lines_up > 1:
                lines_down = lines_up - 1
                sys.stdout.write(f"\r\033[{lines_up}A\r\033[2K\033[{lines_down}B\r")
            else:
                sys.stdout.write("\r\033[1A\r\033[2K")
            sys.stdout.flush()
            self.running_visible = False


class PromptBox:
    resize_settle_delay = 0.15

    def __init__(self, status_bar: StatusBar) -> None:
        self.status_bar = status_bar
        self._input_rows = 1
        self._cursor_row = 0
        self._frame_columns: int | None = None
        self._frame_active = False
        self._frame_renderer: Callable[[str, int, object], None] | None = None
        self._selection_renderer: Callable[[tuple[str, ...], int, object], None] | None = None

    def set_frame_renderer(self, renderer: Callable[[str, int, object], None] | None) -> None:
        self._frame_renderer = renderer

    def set_selection_renderer(
        self,
        renderer: Callable[[tuple[str, ...], int, object], None] | None,
    ) -> None:
        self._selection_renderer = renderer

    def show_border(self) -> None:
        print(colored_border())

    def prompt_input(self, engine=None) -> str:
        if self._frame_renderer is not None and sys.stdin.isatty() and sys.stdout.isatty():
            return self._read_screen_input(engine).strip()

        if sys.stdin.isatty() and sys.stdout.isatty():
            try:
                user_input = self._read_expanding_input(engine)
                self._clear_input_frame()
                self._show_submitted_input(user_input)
                return user_input.strip()
            except KeyboardInterrupt:
                self._clear_input_frame()
                print()
                self.show_border()
                raise
            except EOFError:
                self._clear_input_frame()
                print()
                self.show_border()
                raise

        self.show_border()
        sys.stdout.write("\n")
        sys.stdout.flush()
        self.show_border()
        self.status_bar.show(engine=engine, is_running=False)

        try:
            readline_start = "\x01"
            readline_end = "\x02"
            prompt = f" {readline_start}\033[1;36m{readline_end}testcode>{readline_start}\033[0m{readline_end} "
            user_input = input(prompt).strip()
            self._clear_input_frame()
            self.show_border()
            return user_input
        except KeyboardInterrupt:
            self._clear_input_frame()
            print()
            self.show_border()
            raise KeyboardInterrupt
        except EOFError:
            self._clear_input_frame()
            print()
            self.show_border()
            raise

    def _read_screen_input(self, engine=None) -> str:
        """Read input by redrawing the complete screen after each change."""
        fd = sys.stdin.fileno()
        previous_settings = termios.tcgetattr(fd)
        value = ""
        cursor = 0
        resized = False
        last_resize_at = 0.0

        def mark_resized(_signum, _frame) -> None:
            nonlocal last_resize_at, resized
            resized = True
            last_resize_at = time.monotonic()

        previous_resize_handler = signal.getsignal(signal.SIGWINCH)
        try:
            tty.setcbreak(fd)
            signal.signal(signal.SIGWINCH, mark_resized)
            self._render_screen_input(value, cursor, engine)
            while True:
                readable, _, _ = select.select([fd], [], [], 0.1)
                if resized and time.monotonic() - last_resize_at >= self.resize_settle_delay:
                    resized = False
                    self._render_screen_input(value, cursor, engine)
                if not readable:
                    continue
                key = self._read_key(fd)
                if key in {"\r", "\n"}:
                    return value
                if key == "\x03":
                    raise KeyboardInterrupt
                if key == "\x04":
                    if not value:
                        raise EOFError
                    if cursor < len(value):
                        value = value[:cursor] + value[cursor + 1 :]
                    else:
                        continue
                elif key in {"\x7f", "\b"}:
                    if not cursor:
                        continue
                    value = value[: cursor - 1] + value[cursor:]
                    cursor -= 1
                elif key in {"\x1b[D", "\x1bOD"}:
                    if not cursor:
                        continue
                    cursor -= 1
                elif key in {"\x1b[C", "\x1bOC"}:
                    if cursor >= len(value):
                        continue
                    cursor += 1
                elif key in {"\x1b[H", "\x1bOH"}:
                    cursor = 0
                elif key in {"\x1b[F", "\x1bOF"}:
                    cursor = len(value)
                elif key.startswith("\x1b"):
                    continue
                elif key.isprintable():
                    value = value[:cursor] + key + value[cursor:]
                    cursor += 1
                else:
                    continue
                self._render_screen_input(value, cursor, engine)
        finally:
            signal.signal(signal.SIGWINCH, previous_resize_handler)
            termios.tcsetattr(fd, termios.TCSADRAIN, previous_settings)

    def _render_screen_input(self, value: str, cursor: int, engine=None) -> None:
        if self._frame_renderer is None:
            return
        self._frame_renderer(value, cursor, engine)

    def _read_expanding_input(self, engine=None) -> str:
        """Read a prompt while keeping the border and status bar below wrapped text."""
        fd = sys.stdin.fileno()
        previous_settings = termios.tcgetattr(fd)
        value = ""
        cursor = 0
        self._input_rows = 1
        resized = False
        last_resize_at = 0.0

        def mark_resized(_signum, _frame) -> None:
            nonlocal last_resize_at, resized
            resized = True
            last_resize_at = time.monotonic()

        previous_resize_handler = signal.getsignal(signal.SIGWINCH)
        try:
            tty.setcbreak(fd)
            signal.signal(signal.SIGWINCH, mark_resized)
            self._render_expanding_input(value, engine, cursor)
            while True:
                readable, _, _ = select.select([fd], [], [], 0.1)
                if resized and time.monotonic() - last_resize_at >= self.resize_settle_delay:
                    resized = False
                    # A window drag emits a burst of intermediate sizes. Wait
                    # until it settles so terminals that preserve reflowed
                    # rows do not accumulate one top border per event.
                    self._reset_after_resize(value, engine, cursor)
                if not readable:
                    continue
                key = self._read_key(fd)
                if key in {"\r", "\n"}:
                    return value
                if key == "\x03":
                    raise KeyboardInterrupt
                if key == "\x04":
                    if not value:
                        raise EOFError
                    if cursor < len(value):
                        value = value[:cursor] + value[cursor + 1 :]
                        self._render_expanding_input(value, engine, cursor)
                    else:
                        continue
                    continue
                if key in {"\x7f", "\b"}:
                    if cursor:
                        value = value[: cursor - 1] + value[cursor:]
                        cursor -= 1
                    else:
                        continue
                elif key in {"\x1b[D", "\x1bOD"}:
                    if cursor:
                        cursor -= 1
                    else:
                        continue
                elif key in {"\x1b[C", "\x1bOC"}:
                    if cursor < len(value):
                        cursor += 1
                    else:
                        continue
                elif key in {"\x1b[H", "\x1bOH"}:
                    cursor = 0
                elif key in {"\x1b[F", "\x1bOF"}:
                    cursor = len(value)
                elif key.startswith("\x1b"):
                    continue
                elif key.isprintable():
                    value = value[:cursor] + key + value[cursor:]
                    cursor += 1
                else:
                    continue
                self._render_expanding_input(value, engine, cursor)
        finally:
            signal.signal(signal.SIGWINCH, previous_resize_handler)
            termios.tcsetattr(fd, termios.TCSADRAIN, previous_settings)

    def _reset_after_resize(self, value: str, engine=None, cursor: int | None = None) -> None:
        """Replace the complete input frame after terminal reflow."""
        self._render_relative_frame(value, engine, cursor, resized=True)

    def _redraw_empty_input(self, engine=None) -> None:
        """Refresh only the lower input chrome after a window resize."""
        # Keep the existing prompt row intact. Only replace the rows below it;
        # this avoids duplicating ``testcode>`` when a narrow terminal reflows.
        sys.stdout.write("\r\033[1B\033[J")
        self.show_border()
        self.status_bar.show(engine=engine, is_running=False)
        sys.stdout.write(f"\r\033[3A\033[{self._display_width(' testcode> ')}C")
        sys.stdout.flush()

    def _render_expanding_input(self, value: str, engine=None, cursor: int | None = None) -> None:
        """Redraw an input region that grows directly below the transcript."""
        self._render_relative_frame(value, engine, cursor, resized=False)

    def _render_relative_frame(
        self,
        value: str,
        engine=None,
        cursor: int | None = None,
        *,
        resized: bool,
    ) -> None:
        cursor = len(value) if cursor is None else cursor
        columns = max(terminal_columns(), 1)

        if self._frame_active:
            rows_to_top = self._cursor_row + 1
            if resized:
                previous_columns = self._frame_columns or columns
                previous_lines = self._wrap_prompt_value(value, columns=previous_columns)
                previous_cursor_lines = self._wrap_prompt_value(value[:cursor], columns=previous_columns)
                previous_cursor_row = len(previous_cursor_lines) - 1
                previous_cursor_column = self._display_width(previous_cursor_lines[-1])
                if previous_cursor_row == 0:
                    previous_cursor_column += self._display_width(" testcode> ")

                rows_to_top = ceil(previous_columns / columns)
                for row, line in enumerate(previous_lines[:previous_cursor_row]):
                    width = self._display_width(line)
                    if row == 0:
                        width += self._display_width(" testcode> ")
                    rows_to_top += max(ceil(width / columns), 1)
                rows_to_top += previous_cursor_column // columns

            sys.stdout.write("\r")
            if rows_to_top:
                sys.stdout.write(f"\033[{rows_to_top}A")
            sys.stdout.write("\r\033[J")

        lines = self._wrap_prompt_value(value, columns=columns)
        cursor_lines = self._wrap_prompt_value(value[:cursor], columns=columns)
        cursor_row = len(cursor_lines) - 1
        self.show_border()

        prompt = " \033[1;36mtestcode>\033[0m "
        sys.stdout.write(f"{prompt}{lines[0]}")
        for line in lines[1:]:
            sys.stdout.write(f"\n  {line}")
        sys.stdout.write("\n")
        self.show_border()
        sys.stdout.write(self.status_bar.render_line(engine=engine, is_running=False))

        cursor_column = self._display_width(cursor_lines[-1])
        if cursor_row == 0:
            cursor_column += self._display_width(" testcode> ")
        else:
            cursor_column += 2
        rows_up = len(lines) + 1 - cursor_row
        sys.stdout.write("\r")
        if rows_up:
            sys.stdout.write(f"\033[{rows_up}A")
        if cursor_column:
            sys.stdout.write(f"\033[{cursor_column}C")
        self._input_rows = len(lines)
        self._cursor_row = cursor_row
        self._frame_columns = columns
        self._frame_active = True
        sys.stdout.flush()

    def _show_submitted_input(self, value: str) -> None:
        """Leave the completed prompt in the transcript after it is submitted."""
        lines = self._wrap_prompt_value(value)
        prompt = " \033[1;36mtestcode>\033[0m "
        self.show_border()
        sys.stdout.write(f"{prompt}{lines[0]}")
        for line in lines[1:]:
            sys.stdout.write(f"\n  {line}")
        sys.stdout.write("\n")
        self.show_border()
        sys.stdout.flush()

    def _wrap_prompt_value(self, value: str, columns: int | None = None) -> list[str]:
        columns = max(columns if columns is not None else terminal_columns(), 1)
        first_width = max(columns - self._display_width(" testcode> ") - 1, 1)
        subsequent_width = max(columns - 3, 1)
        lines: list[str] = [""]
        remaining = first_width

        for char in value:
            if char == "\n":
                lines.append("")
                remaining = subsequent_width
                continue
            width = max(self._display_width(char), 1)
            if width > remaining and lines[-1]:
                lines.append("")
                remaining = subsequent_width
            lines[-1] += char
            remaining -= width
            if remaining <= 0:
                lines.append("")
                remaining = subsequent_width

        return lines[:-1] if len(lines) > 1 and not lines[-1] else lines

    def wrap_prompt_value(self, value: str) -> list[str]:
        """Public wrapper used by the full-screen renderer."""
        return self._wrap_prompt_value(value)

    def display_width(self, value: str) -> int:
        return self._display_width(value)

    @staticmethod
    def _display_width(value: str) -> int:
        width = 0
        for char in value:
            if unicodedata.combining(char):
                continue
            width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        return width

    def read_selection(
        self,
        engine=None,
        prompt="    Selection [1-2]: ",
        options: tuple[str, ...] = ("Yes", "No"),
    ) -> str | None:
        if self._selection_renderer is not None and sys.stdin.isatty() and sys.stdout.isatty():
            return self._read_screen_selection(engine, options)

        if sys.stdin.isatty() and sys.stdout.isatty():
            return self._read_interactive_selection(options)

        self._render_selection_options(options, selected=0)
        sys.stdout.write("\n")
        sys.stdout.flush()
        self.show_border()
        self.status_bar.show(engine=engine, left_override="esc to cancel")

        if sys.stdout.isatty():
            sys.stdout.write("\033[3A\r")
            sys.stdout.flush()

        try:
            choice = input(prompt).strip().lower()
            self._clear_input_frame()
            self.show_border()
            return choice
        except (KeyboardInterrupt, EOFError):
            self._clear_input_frame()
            print()
            self.show_border()
            return None

    def _read_screen_selection(self, engine, options: tuple[str, ...]) -> str | None:
        selected = 0
        fd = sys.stdin.fileno()
        previous_settings = termios.tcgetattr(fd)
        resized = False

        def mark_resized(_signum, _frame) -> None:
            nonlocal resized
            resized = True

        previous_resize_handler = signal.getsignal(signal.SIGWINCH)
        try:
            tty.setcbreak(fd)
            signal.signal(signal.SIGWINCH, mark_resized)
            self._selection_renderer(options, selected, engine)
            while True:
                readable, _, _ = select.select([fd], [], [], 0.1)
                if resized:
                    resized = False
                    self._selection_renderer(options, selected, engine)
                if not readable:
                    continue
                key = self._read_key(fd)
                if key in {"\r", "\n"}:
                    return str(selected + 1)
                if key in {"\x03", "\x1b"}:
                    return None
                if key in {"\x1b[A", "\x1bOA", "k"}:
                    selected = (selected - 1) % len(options)
                elif key in {"\x1b[B", "\x1bOB", "j"}:
                    selected = (selected + 1) % len(options)
                elif key.isdigit() and 1 <= int(key) <= len(options):
                    return key
                elif key.lower() == "y":
                    return "1"
                elif key.lower() == "n" and len(options) >= 2:
                    return "2"
                else:
                    continue
                self._selection_renderer(options, selected, engine)
        finally:
            signal.signal(signal.SIGWINCH, previous_resize_handler)
            termios.tcsetattr(fd, termios.TCSADRAIN, previous_settings)

    def _read_interactive_selection(self, options: tuple[str, ...]) -> str | None:
        selected = 0
        self._render_selection_options(options, selected)
        fd = sys.stdin.fileno()
        previous_settings = termios.tcgetattr(fd)

        try:
            tty.setcbreak(fd)
            while True:
                key = self._read_key(fd)
                if key in {"\r", "\n"}:
                    return str(selected + 1)
                if key in {"\x03", "\x1b"}:
                    return None
                if key in {"\x1b[A", "\x1bOA", "k"}:
                    selected = (selected - 1) % len(options)
                elif key in {"\x1b[B", "\x1bOB", "j"}:
                    selected = (selected + 1) % len(options)
                elif key.isdigit() and 1 <= int(key) <= len(options):
                    return key
                elif key.lower() == "y":
                    return "1"
                elif key.lower() == "n" and len(options) >= 2:
                    return "2"
                else:
                    continue
                self._render_selection_options(options, selected, redraw=True)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous_settings)

    def _read_key(self, fd: int) -> str:
        raw_key = os.read(fd, 1)
        if raw_key != b"\x1b":
            # A terminal provides UTF-8 one byte at a time in cbreak mode.
            # Continue until a complete character is available so non-ASCII
            # prompts are not silently discarded.
            while True:
                try:
                    return raw_key.decode()
                except UnicodeDecodeError as error:
                    if error.reason != "unexpected end of data":
                        return raw_key.decode(errors="replace")
                    raw_key += os.read(fd, 1)

        sequence = "\x1b"
        # Arrow keys are short CSI sequences, while mouse wheels commonly
        # send longer SGR sequences (for example ``ESC [ < ... M``).  Consume
        # the complete control sequence so its remaining bytes cannot become
        # literal prompt text.
        for _ in range(32):
            readable, _, _ = select.select([fd], [], [], 0.5)
            if not readable:
                break
            char = os.read(fd, 1).decode(errors="ignore")
            sequence += char
            if len(sequence) == 2 and char not in {"[", "O"}:
                break
            if len(sequence) >= 3 and "@" <= char <= "~":
                break
        return sequence

    def _render_selection_options(
        self,
        options: tuple[str, ...],
        selected: int,
        redraw: bool = False,
    ) -> None:
        if redraw:
            sys.stdout.write(f"\033[{len(options)}A")
        for index, label in enumerate(options):
            pointer = "\033[1;36m>\033[0m" if index == selected else " "
            sys.stdout.write(f"\r\033[2K    {pointer} {index + 1}. {label}\n")
        sys.stdout.flush()

    def _clear_input_frame(self) -> None:
        if sys.stdout.isatty():
            if self._frame_active:
                sys.stdout.write("\r")
                rows_to_top = self._cursor_row + 1
                if rows_to_top:
                    sys.stdout.write(f"\033[{rows_to_top}A")
                sys.stdout.write("\r\033[J")
            else:
                sys.stdout.write("\r")
                if self._input_rows > 1:
                    sys.stdout.write(f"\033[{self._input_rows - 1}A")
                for _ in range(self._input_rows + 2):
                    sys.stdout.write("\033[2K\n")
                sys.stdout.write(f"\033[{self._input_rows + 2}A\r")
            sys.stdout.flush()
        self._input_rows = 1
        self._cursor_row = 0
        self._frame_columns = None
        self._frame_active = False
