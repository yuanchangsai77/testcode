from __future__ import annotations

import os
import sys
import threading
import time


class Ansi:
    CYAN = "\033[1;36m"
    GREEN = "\033[1;32m"
    RED = "\033[1;31m"
    YELLOW = "\033[1;33m"
    MAGENTA = "\033[1;35m"
    GRAY = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def terminal_columns(default: int = 80) -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return default


def colored_border(columns: int | None = None) -> str:
    width = max(columns if columns is not None else terminal_columns(), 1)
    return f"{Ansi.GRAY}{'─' * width}{Ansi.RESET}"


class Spinner:
    def __init__(self, message="Thinking...", delay=0.1, prefix=None):
        self.message = message
        self.delay = delay
        self.spinner_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self.stop_running = threading.Event()
        self.thread = None
        self.is_tty = sys.stdout.isatty()
        self.prefix = prefix

    def _spin(self):
        idx = 0
        while not self.stop_running.is_set():
            char = self.spinner_chars[idx % len(self.spinner_chars)]
            if self.prefix:
                sys.stdout.write(f"\r{self.prefix} {self.message} {Ansi.YELLOW}{char}{Ansi.RESET}")
            else:
                sys.stdout.write(f"\r{Ansi.CYAN}{char}{Ansi.RESET} {self.message}")
            sys.stdout.flush()
            idx += 1
            time.sleep(self.delay)
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    def start(self):
        if self.is_tty:
            self.stop_running.clear()
            self.thread = threading.Thread(target=self._spin, daemon=True)
            self.thread.start()
        else:
            if self.prefix:
                sys.stdout.write(f"{self.prefix} {self.message}... ")
            else:
                sys.stdout.write(f"{self.message}... ")
            sys.stdout.flush()

    def stop(self):
        if self.is_tty:
            if self.thread and self.thread.is_alive():
                self.stop_running.set()
                self.thread.join()
        else:
            sys.stdout.write("done.\n")
            sys.stdout.flush()
