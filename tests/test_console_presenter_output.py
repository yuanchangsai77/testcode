import os
import re
from types import SimpleNamespace

from testcode.interaction.presenter import ConsolePresenter
from testcode.interaction.terminal import Spinner, colored_border
from testcode.types import ExecutionSummary, SessionResumeState, ToolAction, ToolResult


def test_presenter_cleans_protocol_tags_only_for_display(capsys):
    presenter = ConsolePresenter()
    summary = ExecutionSummary(
        final_message=(
            "<think>Need to inspect files first.</think>\n"
            "项目可以先保留 JSON。\n"
            '<minimax:tool_call tool="shell_exec">'
            '<parameter name="command">grep sqlite</parameter>'
            "</minimax:tool_call>"
        ),
        tool_results=[],
    )

    presenter.show_summary(summary)

    output = capsys.readouterr().out
    assert "thinking:" in output
    assert "Need to inspect files first." in output
    assert "项目可以先保留 JSON。" in output
    assert "<think>" not in output
    assert "<parameter" not in output
    assert 'tool="shell_exec"' not in output


def test_presenter_uses_tool_result_summarizer():
    presenter = ConsolePresenter(tool_result_summarizer=lambda result: f"summary for {result.name}")
    
    res1 = ToolResult(
        name="external_tool",
        success=True,
        output="large output that should not be shown",
        metadata={},
    )
    res2 = ToolResult(
        name="git_diff",
        success=True,
        output="diff --git a/src/app.py b/src/app.py\n-old\n+new\n",
        metadata={},
    )
    res3 = ToolResult(
        name="patch",
        success=True,
        output="applied patch:\nsrc/app.py\ntests/test_app.py",
        metadata={},
    )
    res4 = ToolResult(
        name="run_tests",
        success=True,
        output="exit_code: 0\nstdout:\nok\n",
        metadata={},
    )
    
    assert presenter._summarize_tool_result(res1) == "summary for external_tool"
    assert presenter._summarize_tool_result(res2) == "summary for git_diff"
    assert presenter._summarize_tool_result(res3) == "summary for patch"
    assert presenter._summarize_tool_result(res4) == "summary for run_tests"


def test_presenter_summarizes_failed_tool_with_error_code():
    presenter = ConsolePresenter(tool_result_summarizer=lambda _result: "exit 2; stderr 1")
    result = ToolResult(
        name="shell_exec",
        success=False,
        output="exit_code: 2\nstderr:\nfailed loudly",
        error_code="nonzero_exit",
        metadata={"exit_code": 2, "stdout": "", "stderr": "failed loudly"},
    )
    assert presenter._summarize_tool_result(result) == "nonzero_exit: exit 2; stderr 1"


def test_show_tool_end_does_not_duplicate_error_code(capsys):
    presenter = ConsolePresenter()
    action = ToolAction(name="shell_exec", arguments={"command": "false"})
    result = ToolResult(
        name="shell_exec",
        success=False,
        output="command failed",
        error_code="nonzero_exit",
    )

    spinner = presenter.show_tool_start(action.name)
    presenter.show_tool_end(spinner, action, result)

    output = capsys.readouterr().out
    assert "nonzero_exit: command failed" in output
    assert "nonzero_exit: nonzero_exit:" not in output


def test_input_border_preserves_ansi_reset_in_narrow_terminal(monkeypatch, capsys):
    presenter = ConsolePresenter()
    monkeypatch.setattr(os, "get_terminal_size", lambda: os.terminal_size((10, 24)))

    presenter.show_input_border()

    output = capsys.readouterr().out
    assert output == "\033[90m──────────\033[0m\n"


def test_full_width_tty_border_suppresses_pending_autowrap(monkeypatch):
    monkeypatch.setattr("testcode.interaction.terminal.sys.stdout.isatty", lambda: True)

    border = colored_border(4)

    assert border == "\033[?7l\033[90m────\033[0m\r\033[?7h"


def test_resize_preserves_prompt_and_refreshes_only_the_chrome(monkeypatch, capsys):
    presenter = ConsolePresenter()
    columns = 30
    monkeypatch.setattr(os, "get_terminal_size", lambda: os.terminal_size((columns, 24)))
    presenter.prompt_box._render_expanding_input("abcdefghij", cursor=10)
    capsys.readouterr()

    columns = 20
    presenter.prompt_box._reset_after_resize("abcdefghij", cursor=10)

    output = capsys.readouterr().out
    assert output.startswith("\r\033[3A\r\033[J")
    assert "testcode>" in output
    assert output.count("────────────────────") == 2
    assert output.endswith("\r\033[2A\033[4C")


def test_resize_erases_all_reflowed_top_border_rows(monkeypatch, capsys):
    presenter = ConsolePresenter()
    columns = 100
    monkeypatch.setattr(os, "get_terminal_size", lambda: os.terminal_size((columns, 24)))
    presenter.prompt_box._render_expanding_input("", cursor=0)
    capsys.readouterr()

    columns = 20
    presenter.prompt_box._reset_after_resize("", cursor=0)

    output = capsys.readouterr().out
    assert output.startswith("\r\033[5A\r\033[J")
    assert output.count("────────────────────") == 2
    assert "testcode>" in output
    assert output.endswith("\r\033[2A\033[11C")


def test_growing_frame_stays_attached_to_transcript(monkeypatch, capsys):
    presenter = ConsolePresenter()
    monkeypatch.setattr(os, "get_terminal_size", lambda: os.terminal_size((20, 24)))
    presenter.prompt_box._reset_after_resize("", cursor=0)
    capsys.readouterr()

    presenter.prompt_box._render_expanding_input("a" * 30, cursor=30)

    output = capsys.readouterr().out
    assert output.startswith("\r\033[1A\r\033[J")
    assert "\033[24;1H" not in output


def test_submitted_input_keeps_both_history_borders(monkeypatch, capsys):
    presenter = ConsolePresenter()
    monkeypatch.setattr(os, "get_terminal_size", lambda: os.terminal_size((20, 24)))

    presenter.prompt_box._show_submitted_input("hello")

    output = capsys.readouterr().out
    assert output.count("────────────────────") == 2
    assert "testcode>" in output


def test_show_session_state(capsys):
    from testcode.types import StoredSession
    presenter = ConsolePresenter()
    session = StoredSession(
        session_id="test-session-123",
        cwd="/tmp/fake-cwd",
        created_at="2026-07-06T10:00:00",
        updated_at="2026-07-06T10:00:00",
        status="active",
        messages=[],
        run_ids=[],
        trace=[],
        resume_state=SessionResumeState(),
    )

    class DummyEngine:
        def __init__(self):
            self.context_loaders = [object(), object()]
            class DummyTools:
                def __init__(self):
                    self._tools = {"t1": object(), "t2": object(), "t3": object()}
            self.tools = DummyTools()
            class DummyRegistry:
                def __init__(self):
                    self._skills = {"s1": object()}
            self.skills_registry = DummyRegistry()
            self.mcp_server_count = 1
            self.capability_warehouse = SimpleNamespace(
                catalog_entries=lambda: [
                    SimpleNamespace(kind="toolbox", source="skill"),
                    SimpleNamespace(kind="toolbox", source="local"),
                    SimpleNamespace(kind="toolbox", source="mcp"),
                    SimpleNamespace(kind="tool", source="local"),
                ]
            )
            class DummyGuardrails:
                def __init__(self):
                    class DummyPolicy:
                        def __init__(self):
                            self.mode = "confirm"
                    self.policy = DummyPolicy()
            self.guardrails = DummyGuardrails()

    engine = DummyEngine()
    presenter.show_session_state(session, resumed=True, engine=engine)

    output = capsys.readouterr().out
    plain_output = re.sub(r"\x1b\[[0-9;]*m", "", output)
    assert "test-session-123" in output
    assert "/tmp/fake-cwd" in output
    assert "confirm" in output
    assert "Runtime:" in plain_output
    assert "2 Context Loaders" in plain_output
    assert "3 Tools" in plain_output
    assert "Capability Catalog:" in plain_output
    assert "3 Toolboxes (1 Skill · 1 Local · 1 MCP)" in plain_output


def test_show_capabilities_formats_catalog_without_raw_json(capsys):
    presenter = ConsolePresenter()
    presenter.show_capabilities(
        "Capability Warehouse",
        {
            "entries": [
                {
                    "id": "skill:git-helper",
                    "name": "git-helper",
                    "source": "skill",
                    "description": "Safe Git workflows.",
                    "enabled": True,
                    "lifecycle_state": "stored",
                },
                {
                    "id": "mcp:maps",
                    "name": "maps",
                    "source": "mcp",
                    "description": "Map services.",
                    "enabled": False,
                    "lifecycle_state": "stored",
                },
            ]
        },
    )

    output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert "2 toolboxes" in output
    assert "git-helper  Skill · stored" in output
    assert "maps  MCP · disabled" in output
    assert "/capabilities open <toolbox-id>" in output
    assert '"entries"' not in output


def test_show_capabilities_formats_manifest_status_and_result(capsys):
    presenter = ConsolePresenter()
    presenter.show_capabilities(
        "Capability Warehouse",
        {
            "toolbox_id": "local:subagents",
            "state": "ready",
            "items": [
                {
                    "id": "local:subagents:subagent_status",
                    "name": "subagent_status",
                    "risk": "read",
                    "description": "Inspect child sessions.",
                }
            ],
        },
    )
    presenter.show_capabilities(
        "Capability Warehouse",
        {
            "catalog_count": 4,
            "entries": [],
            "opened": [{"toolbox_id": "local:subagents", "name": "subagents", "state": "ready", "item_count": 4}],
            "active": [{"capability_id": "local:subagents:subagent_status", "scope": "session", "state": "activated"}],
            "released": [],
            "budgets": {
                "max_active_capabilities": 8,
                "active_toolboxes": 1,
                "active_capabilities": 1,
                "max_active_schema_chars": 40000,
                "active_schema_chars": 500,
            },
        },
    )
    presenter.show_capabilities(
        "Capability Warehouse",
        {"activated": ["local:subagents:subagent_status"], "scope": "session"},
    )
    presenter.show_capabilities(
        "Capability Warehouse",
        {
            "activated": [f"mcp:amap:tool-{index}" for index in range(15)],
            "toolboxes": ["mcp:amap"],
            "scope": "session",
        },
    )

    output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert "subagents  ready" in output
    assert "subagent_status  read" in output
    assert "Catalog 4 · Opened 1 · Active leaves 1" in output
    assert "Budget 1/8 toolboxes · 1 leaf capability · 500/40000 schema chars" in output
    assert "Activated 1 capability · scope: session" in output
    assert "Activated 1 toolbox · 15 leaf capabilities · scope: session" in output
    assert "mcp:amap:tool-0" not in output


def test_show_tool_start_and_end(capsys):
    from testcode.types import ToolAction, ToolResult
    presenter = ConsolePresenter()
    action = ToolAction(name="test_tool", arguments={"a": 1, "b": "hello world"})
    result = ToolResult(name="test_tool", success=True, output="tool output")
    
    spinner = presenter.show_tool_start(action.name)
    assert spinner is not None
    presenter.show_tool_end(spinner, action, result)
    
    output = capsys.readouterr().out
    assert "test_tool" in output
    assert "a=1" in output
    assert "b=\"hello world\"" in output
    assert "tool output" in output


def test_presenter_preserves_newlines_for_paragraphs(capsys):
    presenter = ConsolePresenter()
    summary = ExecutionSummary(
        final_message="Line 1.\n\nLine 2.\nLine 3.",
        tool_results=[],
    )
    presenter.show_summary(summary)
    output = capsys.readouterr().out
    assert "Line 1." in output
    assert "Line 2." in output


def test_presenter_show_status_bar_and_help(capsys):
    presenter = ConsolePresenter()
    
    class DummyEngine:
        def __init__(self):
            class DummyModel:
                def __init__(self):
                    self.model = "gpt-4-turbo"
            self.model = DummyModel()
            
    engine = DummyEngine()
    presenter.show_status_bar(engine=engine, active_tasks_count=3)
    output = capsys.readouterr().out
    assert "shortcuts" in output
    assert "gpt-4-turbo" in output
    assert "3 task(s)" in output
    
    presenter.show_status_bar(engine=engine, active_tasks_count=0)
    output_zero = capsys.readouterr().out
    assert "shortcuts" in output_zero
    assert "gpt-4-turbo" in output_zero
    assert "task(s)" not in output_zero
    assert "/tasks" not in output_zero
    
    presenter.show_help()
    output_help = capsys.readouterr().out
    assert "/help" in output_help
    assert "/tasks" in output_help
    assert "/skills" in output_help
    assert "/mode" in output_help
    
    presenter.show_input_border()
    output_border = capsys.readouterr().out
    assert "────" in output_border


def test_presenter_prompt_input(monkeypatch, capsys):
    presenter = ConsolePresenter()
    def mock_input(prompt=""):
        import sys
        sys.stdout.write(prompt)
        return "hello test"
    monkeypatch.setattr("builtins.input", mock_input)
    
    val = presenter.prompt_input(engine=None)
    assert val == "hello test"
    
    output = capsys.readouterr().out
    assert "testcode>" in output
    assert "shortcuts" in output
    assert "StubModel" in output


def test_presenter_prompt_input_preserves_eof(monkeypatch, capsys):
    presenter = ConsolePresenter()

    def mock_input(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", mock_input)

    try:
        presenter.prompt_input(engine=None)
    except EOFError:
        pass
    else:
        raise AssertionError("expected EOFError")

    output = capsys.readouterr().out
    assert "shortcuts" in output


def test_interactive_selection_uses_arrow_keys_and_enter(monkeypatch, capsys):
    presenter = ConsolePresenter()
    keys = iter(("\x1b[B", "\r"))

    monkeypatch.setattr(presenter.prompt_box, "_read_key", lambda _fd: next(keys))
    monkeypatch.setattr("testcode.interaction.input.sys.stdin.fileno", lambda: 0)
    monkeypatch.setattr("testcode.interaction.input.termios.tcgetattr", lambda _fd: [])
    monkeypatch.setattr("testcode.interaction.input.termios.tcsetattr", lambda *_args: None)
    monkeypatch.setattr("testcode.interaction.input.tty.setcbreak", lambda _fd: None)

    choice = presenter.prompt_box._read_interactive_selection(("Yes", "No"))

    assert choice == "2"
    output = capsys.readouterr().out
    assert ">\033[0m 1. Yes" in output
    assert ">\033[0m 2. No" in output


def test_interactive_selection_accepts_number_keys(monkeypatch):
    presenter = ConsolePresenter()

    monkeypatch.setattr(presenter.prompt_box, "_read_key", lambda _fd: "2")
    monkeypatch.setattr("testcode.interaction.input.sys.stdin.fileno", lambda: 0)
    monkeypatch.setattr("testcode.interaction.input.termios.tcgetattr", lambda _fd: [])
    monkeypatch.setattr("testcode.interaction.input.termios.tcsetattr", lambda *_args: None)
    monkeypatch.setattr("testcode.interaction.input.tty.setcbreak", lambda _fd: None)

    assert presenter.prompt_box._read_interactive_selection(("Yes", "No")) == "2"


def test_read_key_waits_for_complete_arrow_sequence(monkeypatch):
    presenter = ConsolePresenter()
    bytes_to_read = iter((b"\x1b", b"[", b"B"))
    timeouts = []

    monkeypatch.setattr("testcode.interaction.input.os.read", lambda _fd, _size: next(bytes_to_read))

    def readable(_read, _write, _errors, timeout):
        timeouts.append(timeout)
        return ([0], [], [])

    monkeypatch.setattr("testcode.interaction.input.select.select", readable)

    assert presenter.prompt_box._read_key(0) == "\x1b[B"
    assert timeouts == [0.5, 0.5]


def test_thinking_spinner_is_interruptible(capsys):
    presenter = ConsolePresenter()

    spinner = presenter.show_thinking_start()
    spinner.stop()

    assert spinner.interruptible is True
    assert spinner.message == "Model is thinking..."


def test_presenter_shows_model_timeout_retry_progress(capsys):
    presenter = ConsolePresenter()
    spinner = Spinner(message="Model is thinking...")

    presenter.model_retrying(spinner, 3, 7, "Model request timed out", 1.5)

    assert spinner.message == "Model request timed out — retrying 3/7 in 1.5s..."
    assert "retrying 3/7" in capsys.readouterr().out


def test_spinner_escape_listener_interrupts_on_escape(monkeypatch):
    spinner = Spinner(interruptible=True)
    spinner.stdin_fd = 0
    interrupted = []

    monkeypatch.setattr(spinner, "_read_key", lambda _fd: "\x1b")
    monkeypatch.setattr(spinner, "_signal_interrupt", lambda: interrupted.append(True))
    monkeypatch.setattr("testcode.interaction.terminal.select.select", lambda *_args: ([0], [], []))

    spinner._watch_for_escape()

    assert interrupted == [True]


def test_spinner_escape_listener_ignores_arrow_sequence(monkeypatch):
    spinner = Spinner(interruptible=True)
    spinner.stdin_fd = 0
    interrupted = []

    def arrow_then_stop(_fd):
        spinner.escape_stop.set()
        return "\x1b[B"

    monkeypatch.setattr(spinner, "_read_key", arrow_then_stop)
    monkeypatch.setattr(spinner, "_signal_interrupt", lambda: interrupted.append(True))
    monkeypatch.setattr("testcode.interaction.terminal.select.select", lambda *_args: ([0], [], []))

    spinner._watch_for_escape()

    assert interrupted == []
