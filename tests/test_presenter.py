from testcode.interaction.presenter import ConsolePresenter
from testcode.types import ExecutionSummary, ToolResult


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
    assert "[testcode] thinking:\nNeed to inspect files first." in output
    assert "[testcode] result:\n项目可以先保留 JSON。" in output
    assert "<think>" not in output
    assert "<parameter" not in output
    assert 'tool="shell_exec"' not in output


def test_presenter_uses_tool_result_summarizer(capsys):
    presenter = ConsolePresenter(tool_result_summarizer=lambda result: f"summary for {result.name}")
    summary = ExecutionSummary(
        final_message="done",
        tool_results=[
            ToolResult(
                name="external_tool",
                success=True,
                output="large output that should not be shown",
                metadata={},
            ),
            ToolResult(
                name="git_diff",
                success=True,
                output="diff --git a/src/app.py b/src/app.py\n-old\n+new\n",
                metadata={},
            ),
            ToolResult(
                name="patch",
                success=True,
                output="applied patch:\nsrc/app.py\ntests/test_app.py",
                metadata={},
            ),
            ToolResult(
                name="run_tests",
                success=True,
                output="exit_code: 0\nstdout:\nok\n",
                metadata={},
            ),
        ],
    )

    presenter.show_summary(summary)

    output = capsys.readouterr().out
    assert "- external_tool: ok -> summary for external_tool" in output
    assert "- git_diff: ok -> summary for git_diff" in output
    assert "- patch: ok -> summary for patch" in output
    assert "- run_tests: ok -> summary for run_tests" in output
    assert "large output that should not be shown" not in output
    assert "diff --git" not in output
    assert "exit_code: 0" not in output


def test_presenter_summarizes_failed_tool_with_error_code(capsys):
    presenter = ConsolePresenter(tool_result_summarizer=lambda _result: "exit 2; stderr 1")
    summary = ExecutionSummary(
        final_message="blocked",
        tool_results=[
            ToolResult(
                name="shell_exec",
                success=False,
                output="exit_code: 2\nstderr:\nfailed loudly",
                error_code="nonzero_exit",
                metadata={"exit_code": 2, "stdout": "", "stderr": "failed loudly"},
            )
        ],
    )

    presenter.show_summary(summary)

    output = capsys.readouterr().out
    assert "- shell_exec: blocked -> nonzero_exit: exit 2; stderr 1" in output
