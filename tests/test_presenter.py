from testcode.interaction.presenter import ConsolePresenter
from testcode.types import ExecutionSummary


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
