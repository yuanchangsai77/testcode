from __future__ import annotations

import sys

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

    def read_selection(self, engine=None, prompt="    Selection [1-2]: ") -> str | None:
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

    def _clear_input_frame(self) -> None:
        if sys.stdout.isatty():
            sys.stdout.write("\r\033[2K\033[1B\r\033[2K\033[1A\r")
            sys.stdout.flush()
