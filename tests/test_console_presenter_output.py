from testcode.interaction.presenter import ConsolePresenter
import os

from testcode.types import ExecutionSummary, ToolAction, ToolResult


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
        active_skills=[],
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
    assert "test-session-123" in output
    assert "/tmp/fake-cwd" in output
    assert "confirm" in output
    assert "Context Loaders" in output
    assert "Tools" in output
    assert "Skills" in output


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
    assert "   Line 1.\n   \n   Line 2.\n   Line 3." in output


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
    assert "GPT-4o" in output
    assert "3 task(s)" in output
    
    presenter.show_status_bar(engine=engine, active_tasks_count=0)
    output_zero = capsys.readouterr().out
    assert "shortcuts" in output_zero
    assert "GPT-4o" in output_zero
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
