from __future__ import annotations

import shlex
import sys

from testcode.interaction.presenter import ConsolePresenter
from testcode.observability.logger import InMemoryLogger
from testcode.orchestration.engine import ExecutionEngine
from testcode.safety.guardrails import Guardrails
from testcode.safety.policy import DefaultPolicy
from testcode.tools.builtin_provider import build_builtin_registry
from testcode.types import ExecutionSummary, ModelReply, ToolAction, ToolResult, UserRequest


def test_agent_repairs_after_failed_tests_and_verifies_success(tmp_path):
    (tmp_path / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a - b\n",
        encoding="utf-8",
    )
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    test_command = f"{shlex.quote(sys.executable)} -m pytest -q"

    class RepairModel:
        calls = 0

        def respond(self, session):
            self.calls += 1
            history = "\n".join(session.history)
            if self.calls == 1:
                return ModelReply(
                    message="read implementation",
                    actions=[ToolAction(name="read_file", arguments={"path": "calc.py"})],
                    done=False,
                )
            if self.calls == 2:
                return ModelReply(
                    message="try first fix",
                    actions=[
                        ToolAction(
                            name="patch",
                            arguments={
                                "diff": "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return 0\n"
                            },
                        )
                    ],
                    done=False,
                )
            if self.calls == 3:
                return ModelReply(
                    message="verify first fix",
                    actions=[ToolAction(name="run_tests", arguments={"command": test_command})],
                    done=False,
                )
            if self.calls == 4 and "tests failed" in history:
                return ModelReply(
                    message="re-read before second fix",
                    actions=[ToolAction(name="read_file", arguments={"path": "calc.py"})],
                    done=False,
                )
            if self.calls == 5:
                return ModelReply(
                    message="apply correct fix",
                    actions=[
                        ToolAction(
                            name="patch",
                            arguments={
                                "diff": "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return 0\n+    return a + b\n"
                            },
                        )
                    ],
                    done=False,
                )
            if self.calls == 6:
                return ModelReply(
                    message="verify final fix",
                    actions=[ToolAction(name="run_tests", arguments={"command": test_command})],
                    done=False,
                )
            assert "tests passed" in history
            return ModelReply(message="fixed and verified", done=True)

    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=RepairModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(mode="auto"), logger=logger),
        logger=logger,
        approval_callback=lambda _action, _reason: True,
    )

    summary = engine.execute(UserRequest(prompt="repair calc", cwd=str(tmp_path)))

    assert summary.final_message == "fixed and verified"
    assert [result.name for result in summary.tool_results] == [
        "read_file",
        "patch",
        "run_tests",
        "read_file",
        "patch",
        "run_tests",
    ]
    assert summary.tool_results[2].error_code == "nonzero_exit"
    assert summary.tool_results[-1].success is True
    assert "return a + b" in (tmp_path / "calc.py").read_text(encoding="utf-8")


def test_symlink_escape_is_rejected_by_workspace_bounds(tmp_path):
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)
    registry = build_builtin_registry(InMemoryLogger())

    result = registry.execute(ToolAction(name="read_file", arguments={"path": "link.txt"}), cwd=str(tmp_path))

    assert result.error_code == "path_outside_workspace"
    assert "secret" not in result.output


def test_sensitive_tool_output_is_redacted_in_run_logs(tmp_path):
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    request = UserRequest(
        prompt="check token=sk-test123456789abcdef",
        cwd=str(tmp_path),
        metadata={"secret": "plain-secret"},
    )
    summary = ExecutionSummary(
        final_message="done ghp_1234567890abcdef",
        tool_results=[
            ToolResult(
                name="run_tests",
                success=False,
                output="TOKEN=sk-test123456789abcdef\nPASSWORD=hunter2",
                metadata={"api_key": "plain-key"},
            )
        ],
    )

    logger.finalize(request, summary)

    run_dir = tmp_path / "runs" / logger.last_run_id
    payload = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    details = (run_dir / "details.log").read_text(encoding="utf-8")
    combined = payload + details
    assert "plain-secret" not in combined
    assert "plain-key" not in combined
    assert "hunter2" not in combined
    assert "sk-test123456789abcdef" not in combined
    assert "ghp_1234567890abcdef" not in combined
    assert "[REDACTED]" in combined


def test_patch_approval_shows_diff_preview(monkeypatch, capsys):
    diff = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+print('new')\n"
    action = ToolAction(name="patch", arguments={"diff": diff})
    presenter = ConsolePresenter()
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    approved = presenter.confirm_tool_action(action, "tool 'patch' has risk 'write'")

    output = capsys.readouterr().out
    assert approved is True
    assert "Patch Preview:" in output
    assert "--- /dev/null" in output
    assert "+++ b/new.py" in output
    assert "+print('new')" in output
