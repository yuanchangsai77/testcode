import pytest

from testcode.safety.policy import DefaultPolicy
from testcode.types import ToolAction, ToolDefinition


def decision_for(mode: str, risk_level: str):
    policy = DefaultPolicy(mode=mode)
    action = ToolAction(name=f"{risk_level}_tool")
    definition = ToolDefinition(name=action.name, description="", risk_level=risk_level)
    return policy.evaluate(action, definition)


def test_readonly_mode_only_allows_read_tools():
    read = decision_for("readonly", "read")
    write = decision_for("readonly", "write")

    assert read.allowed is True
    assert read.requires_confirmation is False
    assert write.allowed is False
    assert write.requires_confirmation is False
    assert "blocked in readonly mode" in write.reason


def test_confirm_mode_requires_approval_for_risky_tools():
    read = decision_for("confirm", "read")
    write = decision_for("confirm", "write")
    execute = decision_for("confirm", "execute")

    assert read.allowed is True
    assert read.requires_confirmation is False
    assert write.allowed is False
    assert write.requires_confirmation is True
    assert execute.allowed is False
    assert execute.requires_confirmation is True


def test_auto_mode_allows_write_but_confirms_execute():
    write = decision_for("auto", "write")
    execute = decision_for("auto", "execute")

    assert write.allowed is True
    assert write.requires_confirmation is False
    assert execute.allowed is False
    assert execute.requires_confirmation is True


def test_policy_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unknown safety mode"):
        DefaultPolicy(mode="unsafe")


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "rm -fr build",
        "git reset --hard HEAD",
        "git clean -fd",
        "git clean -xdf",
        "printf x > /etc/testcode.conf",
    ],
)
def test_policy_marks_dangerous_shell_commands_destructive(command):
    policy = DefaultPolicy(mode="confirm")
    action = ToolAction(name="shell_exec", arguments={"command": command})
    definition = ToolDefinition(name="shell_exec", description="", risk_level="execute")

    decision = policy.evaluate(action, definition)

    assert decision.risk_level == "destructive"
    assert decision.requires_confirmation is True
    assert "destructive" in decision.reason


def test_policy_keeps_normal_shell_command_execute_risk():
    policy = DefaultPolicy(mode="confirm")
    action = ToolAction(name="shell_exec", arguments={"command": "python -m pytest"})
    definition = ToolDefinition(name="shell_exec", description="", risk_level="execute")

    decision = policy.evaluate(action, definition)

    assert decision.risk_level == "execute"
