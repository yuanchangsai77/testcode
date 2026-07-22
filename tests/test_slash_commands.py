from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
import pytest

from testcode.interaction.commands import SlashCommand, SlashCommandRegistry, default_slash_command_registry
from testcode.interaction.cli import CLI
from testcode.interaction.presenter import ConsolePresenter


def test_slash_command_registry_completions():
    registry = default_slash_command_registry()
    completions = registry.get_completions("/")
    cmd_names = [name for name, _ in completions]

    assert "/help" in cmd_names
    assert "/clear" in cmd_names
    assert "/status" in cmd_names
    assert "/mode" in cmd_names
    assert "/skills" in cmd_names
    assert "/tasks" in cmd_names
    assert "/resume" in cmd_names
    assert "/exit" in cmd_names
    assert "/quit" in cmd_names

    completions_res = registry.get_completions("/resu")
    assert [name for name, _ in completions_res] == ["/resume"]



def test_slash_command_execution():
    output = StringIO()
    presenter = ConsolePresenter()
    presenter._output = output
    cli = CLI(engine=None, presenter=presenter)

    # Execute /help
    should_exit = cli.command_registry.execute(cli, "/help")
    assert should_exit is False
    assert "Shortcuts & Commands" in output.getvalue()

    # Execute /status
    output.seek(0)
    output.truncate(0)
    should_exit_status = cli.command_registry.execute(cli, "/status")
    assert should_exit_status is False
    assert "Session Status & Environment" in output.getvalue()

    # Execute /exit
    should_exit = cli.command_registry.execute(cli, "/exit")
    assert should_exit is True


def test_custom_command_registration():
    registry = SlashCommandRegistry()
    called = []
    registry.register(
        SlashCommand(
            name="/custom",
            description="Custom command",
            handler=lambda cli, args, **kwargs: called.append(args) or False,
        )
    )

    completions = registry.get_completions("/cus")
    assert completions == [("/custom", "Custom command")]

    cli = CLI(engine=None, presenter=ConsolePresenter(), command_registry=registry)
    cli.command_registry.execute(cli, "/custom arg1 arg2")
    assert called == [["arg1", "arg2"]]


def test_reset_and_compact_commands():
    output = StringIO()
    presenter = ConsolePresenter()
    presenter._output = output
    cli = CLI(engine=None, presenter=presenter)

    conversation = [
        {"role": "user", "content": "msg 1"},
        {"role": "assistant", "content": "ans 1"},
        {"role": "user", "content": "msg 2"},
        {"role": "assistant", "content": "ans 2"},
    ]

    # Compact command
    cli.command_registry.execute(cli, "/compact", conversation=conversation)
    assert len(conversation) == 3
    assert conversation[0]["role"] == "system"
    assert "Executive Summary" in conversation[0]["content"]

    assert "Conversation context compacted" in output.getvalue()

    # Reset command
    output.seek(0)
    output.truncate(0)
    cli.command_registry.execute(cli, "/reset", conversation=conversation)
    assert len(conversation) == 0
    assert "Conversation context reset" in output.getvalue()


from unittest.mock import MagicMock, patch

def test_choose_session_interactive_tty():
    presenter = ConsolePresenter()
    cli = CLI(engine=None, presenter=presenter)

    # Mock list_sessions to return a few dummy sessions
    session_mock1 = SimpleNamespace(session_id="sess-1", cwd="/path/1", status="active", message_count=5, preview="hello", updated_at="2026-07-22")
    session_mock2 = SimpleNamespace(session_id="sess-2", cwd="/path/2", status="closed", message_count=2, preview="world", updated_at="2026-07-22")
    cli.list_sessions = MagicMock(return_value=[session_mock1, session_mock2])
    cli.load_session = MagicMock(side_effect=lambda sid: sid)

    # Mock TTY check and input reading
    with patch("sys.stdin.isatty", return_value=True), \
         patch("sys.stdout.isatty", return_value=True), \
         patch("sys.stdin.fileno", return_value=0), \
         patch("termios.tcgetattr"), \
         patch("termios.tcsetattr"), \
         patch("tty.setcbreak"), \
 \
         patch("select.select", return_value=([1], [], [])), \
         patch("os.read") as mock_read:

        # Simulate user pressing "Down Arrow" (\x1b[B) then "Enter" (\r)
        mock_read.side_effect = [
            b"\x1b", b"[", b"B",  # Down Arrow key
            b"\r"                 # Enter key
        ]

        selected = cli.choose_session()
        assert selected == "sess-2"


