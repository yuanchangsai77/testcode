from __future__ import annotations

import os
import select
import signal
import sys
import termios
import threading
import time
import tty


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
        columns = os.get_terminal_size().columns
        return columns if columns > 0 else default
    except OSError:
        return default


def stable_terminal_line(value: str) -> str:
    """Render a full-width TTY line without setting pending autowrap."""
    if not sys.stdout.isatty():
        return value
    return f"\033[?7l{value}\r\033[?7h"


def colored_border(columns: int | None = None) -> str:
    width = max(columns if columns is not None else terminal_columns(), 1)
    return stable_terminal_line(f"{Ansi.GRAY}{'─' * width}{Ansi.RESET}")


class Spinner:
    def __init__(self, message="Thinking...", delay=0.1, prefix=None, interruptible=False):
        self.message = message
        self.delay = delay
        self.spinner_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self.stop_running = threading.Event()
        self.thread = None
        self.is_tty = sys.stdout.isatty()
        self.prefix = prefix
        self.interruptible = interruptible
        self.escape_stop = threading.Event()
        self.escape_thread = None
        self.stdin_fd = None
        self.stdin_settings = None

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
            if self.interruptible:
                self._start_escape_listener()
        else:
            if self.prefix:
                sys.stdout.write(f"{self.prefix} {self.message}... ")
            else:
                sys.stdout.write(f"{self.message}... ")
            sys.stdout.flush()

    def stop(self):
        self._stop_escape_listener()
        if self.is_tty:
            if self.thread and self.thread.is_alive():
                self.stop_running.set()
                self.thread.join()
        else:
            sys.stdout.write("done.\n")
            sys.stdout.flush()

    def update_message(self, message: str) -> None:
        self.message = message
        if not self.is_tty:
            sys.stdout.write(f"\n{message} ")
            sys.stdout.flush()

    def _start_escape_listener(self) -> None:
        if not sys.stdin.isatty():
            return
        try:
            self.stdin_fd = sys.stdin.fileno()
            self.stdin_settings = termios.tcgetattr(self.stdin_fd)
            tty.setcbreak(self.stdin_fd)
        except (AttributeError, OSError, termios.error):
            self.stdin_fd = None
            self.stdin_settings = None
            return

        self.escape_stop.clear()
        self.escape_thread = threading.Thread(target=self._watch_for_escape, daemon=True)
        self.escape_thread.start()

    def _stop_escape_listener(self) -> None:
        self.escape_stop.set()
        if self.escape_thread and self.escape_thread.is_alive():
            self.escape_thread.join(timeout=0.6)
        if self.stdin_fd is not None and self.stdin_settings is not None:
            try:
                termios.tcsetattr(self.stdin_fd, termios.TCSADRAIN, self.stdin_settings)
            except (OSError, termios.error):
                pass
        self.escape_thread = None
        self.stdin_fd = None
        self.stdin_settings = None

    def _watch_for_escape(self) -> None:
        if self.stdin_fd is None:
            return
        while not self.escape_stop.is_set():
            readable, _, _ = select.select([self.stdin_fd], [], [], 0.05)
            if not readable:
                continue
            if self._read_key(self.stdin_fd) == "\x1b":
                self._signal_interrupt()
                return

    def _read_key(self, fd: int) -> str:
        key = os.read(fd, 1).decode(errors="ignore")
        if key != "\x1b":
            return key

        sequence = key
        for _ in range(2):
            readable, _, _ = select.select([fd], [], [], 0.5)
            if not readable:
                break
            sequence += os.read(fd, 1).decode(errors="ignore")
        return sequence

    def _signal_interrupt(self) -> None:
        os.kill(os.getpid(), signal.SIGINT)
