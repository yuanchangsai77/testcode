from __future__ import annotations

import pytest

from testcode.observability.logger import InMemoryLogger
from testcode.orchestration.engine import ExecutionEngine
from testcode.safety.guardrails import Guardrails
from testcode.safety.policy import DefaultPolicy
from testcode.tools.builtin_provider import build_builtin_registry
from testcode.types import ModelReply, ToolAction, UserRequest


@pytest.mark.parametrize(
    "line",
    [
        'API_KEY = "1234567890abcdef1234567890abcdef"',
        'AMAP_WEB_SERVICE_KEY = "1234567890abcdef1234567890abcdef"',
        'serviceKey = "1234567890abcdef1234567890abcdef"',
        'token = "ghp_1234567890abcdef"',
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "-----BEGIN PRIVATE KEY-----",
        "database_url = https://user:password@example.com/database",
    ],
)
def test_patch_blocks_high_confidence_credentials_before_write(tmp_path, line):
    logger = InMemoryLogger()
    registry = build_builtin_registry(logger)
    diff = (
        "--- /dev/null\n"
        "+++ b/config.py\n"
        "@@ -0,0 +1 @@\n"
        f"+{line}\n"
    )

    result = registry.execute(
        ToolAction(name="patch", arguments={"diff": diff}),
        cwd=str(tmp_path),
    )

    assert result.error_code == "blocked_by_security_policy"
    assert result.metadata["policy_id"] == "SEC-CREDENTIAL-001"
    assert result.metadata["locations"][0]["path"] == "config.py"
    assert not (tmp_path / "config.py").exists()
    combined_events = repr([(event.name, event.payload) for event in logger.events])
    assert line not in combined_events


@pytest.mark.parametrize(
    "line",
    [
        'API_KEY = os.getenv("API_KEY")',
        'api_key = "${API_KEY}"',
        'api_key = "YOUR_API_KEY"',
        'digest = "1234567890abcdef1234567890abcdef"',
    ],
)
def test_patch_allows_runtime_references_placeholders_and_hashes(tmp_path, line):
    registry = build_builtin_registry(InMemoryLogger())
    diff = (
        "--- /dev/null\n"
        "+++ b/config.py\n"
        "@@ -0,0 +1 @@\n"
        f"+{line}\n"
    )

    result = registry.execute(
        ToolAction(name="patch", arguments={"diff": diff}),
        cwd=str(tmp_path),
    )

    assert result.success is True
    assert (tmp_path / "config.py").read_text(encoding="utf-8") == line + "\n"


def test_patch_allows_removing_an_existing_credential(tmp_path):
    secret = 'API_KEY = "1234567890abcdef1234567890abcdef"'
    target = tmp_path / "config.py"
    target.write_text(secret + "\n", encoding="utf-8")
    registry = build_builtin_registry(InMemoryLogger())
    registry.execute(
        ToolAction(name="read_file", arguments={"path": "config.py"}),
        cwd=str(tmp_path),
    )
    diff = (
        "--- a/config.py\n"
        "+++ b/config.py\n"
        "@@ -1 +1 @@\n"
        f"-{secret}\n"
        '+API_KEY = os.getenv("API_KEY")\n'
    )

    result = registry.execute(
        ToolAction(name="patch", arguments={"diff": diff}),
        cwd=str(tmp_path),
    )

    assert result.success is True
    assert "1234567890abcdef" not in target.read_text(encoding="utf-8")


def test_security_preflight_blocks_before_requesting_write_approval(tmp_path):
    secret = "1234567890abcdef1234567890abcdef"

    class Model:
        def respond(self, _session):
            return ModelReply(
                message="write config",
                done=True,
                actions=[
                    ToolAction(
                        name="patch",
                        arguments={
                            "diff": (
                                "--- /dev/null\n"
                                "+++ b/config.py\n"
                                "@@ -0,0 +1 @@\n"
                                f'+API_KEY = "{secret}"\n'
                            )
                        },
                    )
                ],
            )

    approvals = []
    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=Model(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(DefaultPolicy(mode="confirm"), logger),
        logger=logger,
        approval_callback=lambda action, _reason: approvals.append(action) or True,
    )

    summary = engine.execute(
        UserRequest(prompt="create config.py", cwd=str(tmp_path))
    )

    assert approvals == []
    assert summary.tool_results[0].error_code == "blocked_by_security_policy"
    assert secret not in repr(summary.tool_results[0].metadata)
    assert not (tmp_path / "config.py").exists()
    assert any(
        event.name == "safety.content_scan.blocked"
        for event in logger.events
    )


def test_security_block_does_not_copy_secret_into_recovery_session(tmp_path):
    secret = "1234567890abcdef1234567890abcdef"

    class InspectingModel:
        calls = 0
        recovery_history = ""

        def respond(self, session):
            self.calls += 1
            if self.calls == 2:
                self.recovery_history = repr(session.history)
                return ModelReply(message="stopped safely", done=True, actions=[])
            return ModelReply(
                message="write config",
                done=True,
                actions=[
                    ToolAction(
                        name="patch",
                        arguments={
                            "diff": (
                                "--- /dev/null\n"
                                "+++ b/config.py\n"
                                "@@ -0,0 +1 @@\n"
                                f'+API_KEY = "{secret}"\n'
                            )
                        },
                    )
                ],
            )

    model = InspectingModel()
    logger = InMemoryLogger()
    summary = ExecutionEngine(
        model=model,
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(DefaultPolicy(mode="auto"), logger),
        logger=logger,
    ).execute(
        UserRequest(prompt="create config.py", cwd=str(tmp_path))
    )

    assert model.calls == 2
    assert secret not in model.recovery_history
    assert secret not in repr(summary.tool_results[0].metadata)


def test_shell_literal_credentials_are_blocked_as_a_supplemental_guard(tmp_path):
    result = build_builtin_registry(InMemoryLogger()).execute(
        ToolAction(
            name="shell_exec",
            arguments={
                "command": (
                    "printf 'API_KEY = "
                    '"1234567890abcdef1234567890abcdef"'
                    "' > config.py"
                ),
            },
        ),
        cwd=str(tmp_path),
    )

    assert result.error_code == "blocked_by_security_policy"
    assert not (tmp_path / "config.py").exists()


@pytest.mark.parametrize(
    "added_lines",
    [
        ['API_KEY = ("12345678" "90abcdef1234567890abcdef")'],
        [
            "API_KEY = (",
            '    "12345678"',
            '    "90abcdef1234567890abcdef"',
            ")",
        ],
    ],
)
def test_patch_blocks_concatenated_credential_literals(tmp_path, added_lines):
    diff_lines = [
        "--- /dev/null",
        "+++ b/config.py",
        f"@@ -0,0 +1,{len(added_lines)} @@",
        *(f"+{line}" for line in added_lines),
    ]

    result = build_builtin_registry(InMemoryLogger()).execute(
        ToolAction(
            name="patch",
            arguments={"diff": "\n".join(diff_lines) + "\n"},
        ),
        cwd=str(tmp_path),
    )

    assert result.error_code == "blocked_by_security_policy"
    assert not (tmp_path / "config.py").exists()


def test_approval_callback_cannot_replace_preflighted_action(tmp_path):
    safe_diff = (
        "--- /dev/null\n"
        "+++ b/config.py\n"
        "@@ -0,0 +1 @@\n"
        "+VALUE = 1\n"
    )
    unsafe_diff = (
        "--- /dev/null\n"
        "+++ b/config.py\n"
        "@@ -0,0 +1 @@\n"
        '+AMAP_WEB_SERVICE_KEY = "1234567890abcdef1234567890abcdef"\n'
    )

    class Model:
        def respond(self, _session):
            return ModelReply(
                message="write config",
                done=True,
                actions=[
                    ToolAction(
                        name="patch",
                        arguments={"diff": safe_diff},
                    )
                ],
            )

    def mutate_and_approve(action, _reason):
        action.arguments["diff"] = unsafe_diff
        return True

    logger = InMemoryLogger()
    summary = ExecutionEngine(
        model=Model(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(DefaultPolicy(mode="confirm"), logger),
        logger=logger,
        approval_callback=mutate_and_approve,
        max_turns=2,
    ).execute(
        UserRequest(prompt="create config.py", cwd=str(tmp_path))
    )

    assert any(
        result.error_code == "blocked_by_security_policy"
        for result in summary.tool_results
    )
    assert not (tmp_path / "config.py").exists()


def test_security_block_gets_a_recovery_turn_even_when_model_says_done(tmp_path):
    class RepairingModel:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            value = (
                '"1234567890abcdef1234567890abcdef"'
                if self.calls == 1
                else 'os.getenv("AMAP_WEB_SERVICE_KEY")'
            )
            return ModelReply(
                message="write config",
                done=True,
                actions=[
                    ToolAction(
                        name="patch",
                        arguments={
                            "diff": (
                                "--- /dev/null\n"
                                "+++ b/config.py\n"
                                "@@ -0,0 +1 @@\n"
                                f"+AMAP_WEB_SERVICE_KEY = {value}\n"
                            )
                        },
                    )
                ],
            )

    model = RepairingModel()
    logger = InMemoryLogger()
    summary = ExecutionEngine(
        model=model,
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(DefaultPolicy(mode="auto"), logger),
        logger=logger,
    ).execute(
        UserRequest(prompt="create config.py", cwd=str(tmp_path))
    )

    assert model.calls == 2
    assert summary.tool_results[0].error_code == "blocked_by_security_policy"
    assert summary.tool_results[1].success is True
    assert "os.getenv" in (tmp_path / "config.py").read_text(encoding="utf-8")
