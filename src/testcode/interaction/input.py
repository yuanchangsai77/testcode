from __future__ import annotations

import os
import select
import sys
import termios
import tty

from .terminal import colored_border, terminal_columns


class StatusBar:
    def __init__(self) -> None:
        self.running_visible = False

    def show(self, engine=None, active_tasks_count=0, is_running=False, left_override=None) -> None:
        gray = "\033[90m"
        reset = "\033[0m"

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

        if "gemini" in model_name.lower():
            model_name = "Gemini 3.5 Flash"
        elif "gpt-4" in model_name.lower() or "gpt-5" in model_name.lower():
            model_name = "GPT-4o"

        if not is_running and active_tasks_count > 0:
            right_text = f"{model_name} · {active_tasks_count} task(s) · /tasks"
        else:
            right_text = f"{model_name}"

        padding = terminal_columns() - len(left_text) - len(right_text) - 4
        if padding < 2:
            padding = 2

        print(f" {gray}{left_text}{' ' * padding}{right_text}{reset}")

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
    def __init__(self, status_bar: StatusBar) -> None:
        self.status_bar = status_bar

    def show_border(self) -> None:
        print(colored_border())

    def prompt_input(self, engine=None) -> str:
        self.show_border()
        sys.stdout.write("\n")
        sys.stdout.flush()
        self.show_border()
        self.status_bar.show(engine=engine, is_running=False)

        if sys.stdout.isatty():
            sys.stdout.write("\033[3A\r")
            sys.stdout.flush()

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

    def read_selection(
        self,
        engine=None,
        prompt="    Selection [1-2]: ",
        options: tuple[str, ...] = ("Yes", "No"),
    ) -> str | None:
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
        key = os.read(fd, 1).decode(errors="ignore")
        if key != "\x1b":
            return key

        sequence = key
        for _ in range(2):
            # Direction keys arrive as a three-byte escape sequence. Allow
            # enough time for the remaining bytes on slower terminals/SSH.
            readable, _, _ = select.select([fd], [], [], 0.5)
            if not readable:
                break
            sequence += os.read(fd, 1).decode(errors="ignore")
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
            sys.stdout.write("\r\033[2K\033[1B\r\033[2K\033[1A\r")
            sys.stdout.flush()
