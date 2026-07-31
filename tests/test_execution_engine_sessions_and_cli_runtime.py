import json

import pytest

from testcode.app import create_app
from testcode.interaction.cli import CLI
from testcode.interaction.presenter import ConsolePresenter
from testcode.model.types import ModelConnectionError, ModelTimeoutError
from testcode.observability.logger import InMemoryLogger
from testcode.orchestration.engine import ExecutionEngine
from testcode.safety.guardrails import Guardrails
from testcode.safety.policy import DefaultPolicy
from testcode.sessions import SessionStore
from testcode.tools.builtin import build_builtin_registry
from testcode.types import ModelReply, SessionRunTrace, SessionTurnTrace, ToolAction, ToolDefinition, UserRequest


def test_scaffold_runs_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    app = create_app()
    summary = app.run(UserRequest(prompt="inspect workspace", cwd=str(tmp_path)))

    assert "scaffold is ready" in summary.final_message
    assert summary.tool_results
    assert summary.tool_results[0].success is True


def test_run_returns_message_when_model_request_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app()

    def fail(_request):
        raise RuntimeError("Model request failed: [Errno 111] Connection refused")

    monkeypatch.setattr(app.engine, "execute", fail)

    summary = app.run(UserRequest(prompt="inspect workspace", cwd=str(tmp_path)))

    assert "Model API is unavailable right now." in summary.final_message
    assert "Connection refused" in summary.final_message
    assert "keep this session open and try again" in summary.final_message
    assert summary.tool_results == []


def test_engine_retries_model_timeout_seven_times_before_succeeding(tmp_path):
    class EventuallyRespondingModel:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            if self.calls <= 7:
                raise ModelTimeoutError("timed out")
            return ModelReply(message="recovered", done=True)

    class Progress:
        retries = []

        def model_started(self):
            return object()

        def model_retrying(self, _handle, retry, max_retries, status, delay_seconds):
            self.retries.append((retry, max_retries, status, delay_seconds))

        def model_finished(self, _handle):
            pass

    logger = InMemoryLogger()
    model = EventuallyRespondingModel()
    progress = Progress()
    engine = ExecutionEngine(
        model=model,
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
        progress_reporter=progress,
    )
    engine.model_retry_delays = (0.0,) * 7

    summary = engine.execute(UserRequest(prompt="hello", cwd=str(tmp_path)))

    assert summary.final_message == "recovered"
    assert model.calls == 8
    
    expected_retries = []
    for retry in range(1, 8):
        expected_retries.append((retry, 7, "Model request timed out", 0.0))
        expected_retries.append((retry, 7, "Sending request", 0.0))
        
    assert progress.retries == expected_retries


def test_engine_reports_api_unavailable_after_seven_timeout_retries(tmp_path):
    class AlwaysTimingOutModel:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            raise ModelTimeoutError("timed out")

    logger = InMemoryLogger()
    model = AlwaysTimingOutModel()
    engine = ExecutionEngine(
        model=model,
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
    )
    engine.model_retry_delays = (0.0,) * 7

    with pytest.raises(RuntimeError, match="All 7 retry attempts failed"):
        engine.execute(UserRequest(prompt="hello", cwd=str(tmp_path)))

    assert model.calls == 8
    retry_events = [event for event in logger.events if event.name == "model.retry"]
    assert [event.payload["retry"] for event in retry_events] == list(range(1, 8))


def test_engine_retries_transient_model_connection_failure(tmp_path):
    class ReconnectingModel:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            if self.calls == 1:
                raise ModelConnectionError("Remote end closed connection without response")
            return ModelReply(message="reconnected", done=True)

    logger = InMemoryLogger()
    model = ReconnectingModel()
    engine = ExecutionEngine(
        model=model,
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
    )
    engine.model_retry_delays = (0.0,) * 7

    summary = engine.execute(UserRequest(prompt="hello", cwd=str(tmp_path)))

    assert summary.final_message == "reconnected"
    assert model.calls == 2


def test_create_app_tolerates_configured_mcp_servers_without_transport_implementation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".testcode"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[[mcp.servers]]
name = "github"
transport = "stdio"
command = "missing-mcp-server-command"
        """.strip(),
        encoding="utf-8",
    )

    app = create_app()

    assert app.engine.tools.definition_for("read_file") is not None
    assert app.engine.mcp_server_count == 1


def test_create_app_does_not_connect_mcp_servers_during_startup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".testcode"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[[mcp.servers]]
name = "remote"
transport = "streamable_http"
url = "http://example.test/mcp"
        """.strip(),
        encoding="utf-8",
    )

    def fail_if_connected(_server):
        raise AssertionError("MCP discovery must not run while creating the app")

    monkeypatch.setattr("testcode.app.create_mcp_client", fail_if_connected)

    app = create_app()

    assert app.engine.tools.definition_for("read_file") is not None


def test_explicit_mcp_request_opens_only_selected_toolbox_and_reports_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".testcode"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[[mcp.servers]]
name = "amap"
transport = "stdio"
command = "missing-mcp-server-command"
        """.strip(),
        encoding="utf-8",
    )
    app = create_app()

    calls = 0

    def respond(session):
        nonlocal calls
        calls += 1
        assert session.workspace_summary is None
        if calls == 1:
            return ModelReply(
                message="list warehouse",
                actions=[ToolAction(name="warehouse_list", arguments={})],
            )
        if calls == 2:
            assert "mcp:amap" in session.tool_results[-1].output
            return ModelReply(
                message="opening requested toolbox",
                actions=[ToolAction(name="toolbox_open", arguments={"toolbox_id": "mcp:amap"})],
            )
        return ModelReply(message=session.tool_results[-1].output, done=True)

    monkeypatch.setattr(app.engine.model, "respond", respond)

    summary = app.run(UserRequest(prompt="使用 MCP 帮我查询路线", cwd=str(tmp_path)))

    assert calls == 3
    assert "amap" in summary.final_message
    assert "unavailable" in summary.final_message
    event_names = [event.name for event in app.logger.events]
    assert "capability.toolbox.opened" in event_names
    assert "context.workspace_summary" not in event_names
    assert "context.workspace_summary.skipped" in event_names


def test_explicit_mcp_request_without_configuration_exposes_no_mcp_toolbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app()

    observed = {"calls": 0}

    def respond(session):
        observed["calls"] += 1
        observed["tools"] = [tool.name for tool in session.available_tools]
        observed["workspace"] = session.workspace_summary
        if observed["calls"] == 1:
            return ModelReply(
                message="list warehouse",
                actions=[ToolAction(name="warehouse_list", arguments={})],
            )
        observed["warehouse_output"] = session.tool_results[-1].output
        return ModelReply(message="No configured MCP toolbox is present.", done=True)

    monkeypatch.setattr(app.engine.model, "respond", respond)

    summary = app.engine.execute(UserRequest(prompt="使用 MCP 查询路线", cwd=str(tmp_path)))

    assert "No configured MCP toolbox" in summary.final_message
    entries = json.loads(observed["warehouse_output"])["entries"]
    assert not any(entry["source"] == "mcp" for entry in entries)
    assert "warehouse_list" in observed["tools"]
    assert observed["workspace"] is None


def test_mcp_code_task_is_not_mistaken_for_external_tool_request(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app()
    observed = {"called": False}

    def respond(session):
        observed["called"] = True
        observed["workspace_summary"] = session.workspace_summary
        return ModelReply(message="reviewed MCP code", done=True)

    monkeypatch.setattr(app.engine.model, "respond", respond)

    summary = app.engine.execute(
        UserRequest(prompt="检查并修复 MCP integration code", cwd=str(tmp_path))
    )

    assert summary.final_message == "reviewed MCP code"
    assert observed["called"] is True
    assert observed["workspace_summary"] is not None


def test_healthy_mcp_request_reaches_model_without_workspace_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".testcode"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[[mcp.servers]]
name = "amap"
transport = "stdio"
command = "fake-amap"
        """.strip(),
        encoding="utf-8",
    )

    lifecycle = {"initialized": 0}

    class Client:
        def initialize(self):
            lifecycle["initialized"] += 1

        def list_tools(self):
            from testcode.mcp.types import MCPToolDescriptor

            return (
                MCPToolDescriptor(
                    server_name="amap",
                    tool_name="maps_direction_driving",
                    description="Plan a driving route",
                ),
            )

        def list_resources(self):
            return ()

        def close(self):
            pass

    monkeypatch.setattr("testcode.app.create_mcp_client", lambda _config: Client())
    app = create_app()
    observed = {"turn_tools": []}

    def respond(session):
        tools = [tool.name for tool in session.available_tools]
        observed["turn_tools"].append(tools)
        observed["workspace_summary"] = session.workspace_summary
        turn = len(observed["turn_tools"])
        if turn == 1:
            assert lifecycle["initialized"] == 0
            return ModelReply(
                message="list warehouse",
                actions=[ToolAction(name="warehouse_list", arguments={})],
            )
        if turn == 2:
            assert lifecycle["initialized"] == 0
            assert "mcp:amap" in session.tool_results[-1].output
            return ModelReply(
                message="open maps",
                actions=[ToolAction(name="toolbox_open", arguments={"toolbox_id": "mcp:amap"})],
            )
        if turn == 3:
            assert lifecycle["initialized"] == 1
            return ModelReply(
                message="activate route planner",
                actions=[ToolAction(
                    name="capability_activate",
                    arguments={"capability_ids": ["mcp:amap:maps_direction_driving"]},
                )],
            )
        return ModelReply(message="route ready", done=True)

    monkeypatch.setattr(app.engine.model, "respond", respond)

    summary = app.engine.execute(
        UserRequest(prompt="使用 MCP 帮我查询留仙洞到梅塘路线", cwd=str(tmp_path))
    )

    assert summary.final_message == "route ready"
    assert "amap__maps_direction_driving" not in observed["turn_tools"][0]
    assert "amap__maps_direction_driving" not in observed["turn_tools"][1]
    assert "amap__maps_direction_driving" not in observed["turn_tools"][2]
    assert "amap__maps_direction_driving" in observed["turn_tools"][3]
    assert observed["workspace_summary"] is None
    assert lifecycle["initialized"] == 1


def test_newly_activated_tool_cannot_run_in_same_model_turn(tmp_path):
    logger = InMemoryLogger()
    registry = build_builtin_registry(logger)

    class Source:
        def catalog_entries(self):
            from testcode.capabilities.model import CapabilityEntry

            return (CapabilityEntry("fake:box", "box", "toolbox", "fake", "Fake box"),)

        def owns_toolbox(self, toolbox_id):
            return toolbox_id == "fake:box"

        def open_toolbox(self, toolbox_id):
            from testcode.capabilities.model import CapabilityManifest, ManifestItem

            return CapabilityManifest(
                toolbox_id=toolbox_id,
                name="box",
                source="fake",
                state="ready",
                items=(ManifestItem("fake:box:leaf", toolbox_id, "leaf", "tool", "Leaf"),),
            )

        def activate(self, capability_id):
            from testcode.capabilities.model import ActivatedCapability
            from testcode.tools.base import SimpleTool

            tool = SimpleTool(
                name="leaf",
                description="Leaf",
                arguments={},
                input_schema={"type": "object", "properties": {}},
                handler=lambda action, _context: ToolResult(action.name, True, "used"),
            )
            return ActivatedCapability(capability_id, "fake:box", "tool", tool=tool)

    from testcode.capabilities.tools import build_warehouse_tools
    from testcode.capabilities.warehouse import CapabilityWarehouse

    warehouse = CapabilityWarehouse([Source()], registry, logger=logger)
    for tool in build_warehouse_tools(warehouse):
        registry.register(tool)
    warehouse.open_toolbox("fake:box")

    class Model:
        calls = 0

        def respond(self, session):
            self.calls += 1
            if self.calls == 1:
                return ModelReply(
                    message="activate and use",
                    actions=[
                        ToolAction("capability_activate", {"capability_ids": ["fake:box:leaf"]}),
                        ToolAction("leaf", {}),
                    ],
                )
            assert any(tool.name == "leaf" for tool in session.available_tools)
            return ModelReply(message="done", done=True)

    engine = ExecutionEngine(
        model=Model(),
        tools=registry,
        guardrails=Guardrails(DefaultPolicy(mode="auto"), logger),
        logger=logger,
        capability_warehouse=warehouse,
    )
    summary = engine.execute(UserRequest(prompt="use leaf", cwd=str(tmp_path), metadata={"session_id": "s"}))

    assert summary.tool_results[0].success is True
    assert summary.tool_results[1].error_code == "tool_not_visible_this_turn"


def test_engine_stops_repeated_non_retryable_tool_failures(tmp_path):
    class RepeatingBlockedModel:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            return ModelReply(
                message="retrying",
                actions=[ToolAction(name="shell_exec", arguments={"command": "pwd"})],
                done=False,
            )

    logger = InMemoryLogger()
    model = RepeatingBlockedModel()
    engine = ExecutionEngine(
        model=model,
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
    )

    summary = engine.execute(UserRequest(prompt="run pwd", cwd=str(tmp_path)))

    assert model.calls == 2
    assert "requires explicit approval" in summary.final_message
    assert summary.tool_results[-1].error_code == "approval_required"


def test_engine_stops_spinner_without_masking_tool_execute_exception(tmp_path):
    class OneToolModel:
        def respond(self, _session):
            return ModelReply(
                message="run tool",
                actions=[ToolAction(name="explode", arguments={})],
                done=True,
            )

    class ExplodingTools:
        def definitions(self):
            return [ToolDefinition(name="explode", description="explode", risk_level="read")]

        def definition_for(self, _name):
            return ToolDefinition(name="explode", description="explode", risk_level="read")

        def execute(self, *_args, **_kwargs):
            raise RuntimeError("tool exploded")

    class ProgressHandle:
        stopped = False

        def stop(self):
            self.stopped = True

    class ProgressReporter:
        def __init__(self):
            self.handle = ProgressHandle()
            self.tool_finished_calls = 0

        def model_started(self):
            return None

        def model_finished(self, _handle):
            return None

        def tool_started(self, _action_name):
            return self.handle

        def tool_finished(self, *_args):
            self.tool_finished_calls += 1

        def tool_aborted(self, handle):
            handle.stop()

        def tool_skipped(self, *_args):
            return None

    logger = InMemoryLogger()
    progress = ProgressReporter()
    engine = ExecutionEngine(
        model=OneToolModel(),
        tools=ExplodingTools(),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
        progress_reporter=progress,
    )

    with pytest.raises(RuntimeError, match="tool exploded"):
        engine.execute(UserRequest(prompt="run tool", cwd=str(tmp_path)))

    assert progress.handle.stopped is True
    assert progress.tool_finished_calls == 0


def test_engine_clears_current_session_when_model_raises(tmp_path):
    class FailingModel:
        def respond(self, _session):
            raise RuntimeError("model failed")

    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=FailingModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
    )

    with pytest.raises(RuntimeError, match="model failed"):
        engine.execute(UserRequest(prompt="fail", cwd=str(tmp_path)))

    assert engine.current_session is None


def test_engine_finishes_falsy_progress_handles(tmp_path):
    class DoneModel:
        def respond(self, _session):
            return ModelReply(message="done", done=True)

    class ProgressReporter:
        def __init__(self):
            self.model_finished_handles = []

        def model_started(self):
            return 0

        def model_finished(self, handle):
            self.model_finished_handles.append(handle)

        def tool_started(self, _action_name):
            return None

        def tool_finished(self, *_args):
            return None

        def tool_aborted(self, *_args):
            return None

        def tool_skipped(self, *_args):
            return None

    logger = InMemoryLogger()
    progress = ProgressReporter()
    engine = ExecutionEngine(
        model=DoneModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
        progress_reporter=progress,
    )

    summary = engine.execute(UserRequest(prompt="done", cwd=str(tmp_path)))

    assert summary.final_message == "done"
    assert progress.model_finished_handles == [0]


def test_engine_executes_approved_patch_tool(tmp_path):
    class PatchModel:
        def respond(self, _session):
            diff = """--- /dev/null
+++ b/hello_world.py
@@ -0,0 +1 @@
+print("hello world")
"""
            return ModelReply(
                message="created hello_world.py",
                actions=[ToolAction(name="patch", arguments={"diff": diff})],
                done=True,
            )

    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=PatchModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
        approval_callback=lambda _action, _reason: True,
    )

    summary = engine.execute(UserRequest(prompt="write hello world", cwd=str(tmp_path)))

    assert summary.final_message == "created hello_world.py"
    assert summary.tool_results[0].success is True
    assert (tmp_path / "hello_world.py").read_text(encoding="utf-8") == 'print("hello world")\n'


def test_engine_blocks_patch_in_readonly_mode_without_prompting(tmp_path):
    class PatchModel:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            return ModelReply(
                message="try patch",
                actions=[
                    ToolAction(
                        name="patch",
                        arguments={
                            "diff": "--- /dev/null\n+++ b/blocked.py\n@@ -0,0 +1 @@\n+print('blocked')\n"
                        },
                    )
                ],
                done=False,
            )

    approvals = []
    logger = InMemoryLogger()
    model = PatchModel()
    engine = ExecutionEngine(
        model=model,
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(mode="readonly"), logger=logger),
        logger=logger,
        approval_callback=lambda action, _reason: approvals.append(action.name) or True,
    )

    summary = engine.execute(UserRequest(prompt="write file", cwd=str(tmp_path)))

    assert model.calls == 2
    assert approvals == []
    assert summary.tool_results[-1].error_code == "blocked_by_policy"
    assert not (tmp_path / "blocked.py").exists()


def test_engine_allows_patch_in_auto_mode_without_prompting(tmp_path):
    class PatchModel:
        def respond(self, _session):
            return ModelReply(
                message="created auto.py",
                actions=[
                    ToolAction(
                        name="patch",
                        arguments={"diff": "--- /dev/null\n+++ b/auto.py\n@@ -0,0 +1 @@\n+print('auto')\n"},
                    )
                ],
                done=True,
            )

    approvals = []
    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=PatchModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(mode="auto"), logger=logger),
        logger=logger,
        approval_callback=lambda action, _reason: approvals.append(action.name) or True,
    )

    summary = engine.execute(UserRequest(prompt="write file", cwd=str(tmp_path)))

    assert approvals == []
    assert summary.tool_results[0].success is True
    assert (tmp_path / "auto.py").read_text(encoding="utf-8") == "print('auto')\n"


def test_engine_remembers_approved_risk_group_within_execute(tmp_path):
    class ShellModel:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            if self.calls == 1:
                return ModelReply(
                    message="first",
                    actions=[ToolAction(name="shell_exec", arguments={"command": "printf one"})],
                    done=False,
                )
            return ModelReply(
                message="second",
                actions=[ToolAction(name="shell_exec", arguments={"command": "printf two"})],
                done=True,
            )

    approvals = []
    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=ShellModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
        approval_callback=lambda action, _reason: approvals.append(action.arguments["command"]) or True,
    )

    summary = engine.execute(UserRequest(prompt="run commands", cwd=str(tmp_path)))

    assert approvals == ["printf one"]
    assert len(summary.tool_results) == 2
    assert all(result.success for result in summary.tool_results)


def test_engine_does_not_remember_destructive_approval(tmp_path):
    class DestructiveShellModel:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            if self.calls == 1:
                return ModelReply(
                    message="first destructive",
                    actions=[ToolAction(name="shell_exec", arguments={"command": "git reset --hard"})],
                    done=False,
                )
            return ModelReply(
                message="second destructive",
                actions=[ToolAction(name="shell_exec", arguments={"command": "git clean -fd"})],
                done=True,
            )

    approvals = []
    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=DestructiveShellModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
        approval_callback=lambda action, _reason: approvals.append(action.arguments["command"]) or True,
    )

    summary = engine.execute(UserRequest(prompt="run destructive commands", cwd=str(tmp_path)))

    assert approvals == ["git reset --hard", "git clean -fd"]
    assert len(summary.tool_results) == 2


def test_engine_allows_model_to_fix_after_failed_tests(tmp_path):
    class TestFixModel:
        calls = 0

        def respond(self, session):
            self.calls += 1
            history = "\n".join(session.history)
            if "tests failed" not in history:
                return ModelReply(
                    message="run tests",
                    actions=[ToolAction(name="run_tests", arguments={"command": "exit 1"})],
                    done=False,
                )
            return ModelReply(
                message="fixed after seeing test output",
                actions=[
                    ToolAction(
                        name="patch",
                        arguments={
                            "diff": "--- /dev/null\n+++ b/fixed.py\n@@ -0,0 +1 @@\n+print('fixed')\n"
                        },
                    )
                ],
                done=True,
            )

    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=TestFixModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(mode="auto"), logger=logger),
        logger=logger,
        approval_callback=lambda _action, _reason: True,
    )

    summary = engine.execute(UserRequest(prompt="fix tests", cwd=str(tmp_path)))

    assert summary.final_message == "fixed after seeing test output"
    assert summary.tool_results[0].name == "run_tests"
    assert summary.tool_results[0].error_code == "nonzero_exit"
    assert summary.tool_results[1].success is True
    assert (tmp_path / "fixed.py").exists()


def test_engine_stops_after_repeated_failed_test_runs(tmp_path):
    class FailingTestsModel:
        def respond(self, _session):
            return ModelReply(
                message="try tests",
                actions=[ToolAction(name="run_tests", arguments={"command": "exit 1"})],
                done=False,
            )

    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=FailingTestsModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(mode="auto"), logger=logger),
        logger=logger,
        approval_callback=lambda _action, _reason: True,
    )

    summary = engine.execute(UserRequest(prompt="test loop", cwd=str(tmp_path)))

    assert "Stopping after 3 consecutive failing test runs" in summary.final_message
    assert [result.name for result in summary.tool_results] == ["run_tests", "run_tests", "run_tests"]


def test_engine_keeps_read_state_within_single_execute(tmp_path):
    (tmp_path / "target.txt").write_text("before\n", encoding="utf-8")

    class ReadThenPatchModel:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            if self.calls == 1:
                return ModelReply(
                    message="read target",
                    actions=[ToolAction(name="read_file", arguments={"path": "target.txt"})],
                    done=False,
                )
            return ModelReply(
                message="patched",
                actions=[
                    ToolAction(
                        name="patch",
                        arguments={
                            "diff": "--- a/target.txt\n+++ b/target.txt\n@@ -1 +1 @@\n-before\n+after\n"
                        },
                    )
                ],
                done=True,
            )

    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=ReadThenPatchModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(mode="auto"), logger=logger),
        logger=logger,
    )

    summary = engine.execute(UserRequest(prompt="update target", cwd=str(tmp_path)))

    assert summary.final_message == "patched"
    assert summary.tool_results[-1].success is True
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "after\n"


def test_engine_resets_read_state_between_executes(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    diff = "--- a/target.txt\n+++ b/target.txt\n@@ -1 +1 @@\n-before\n+after\n"

    class SingleActionModel:
        def __init__(self, action):
            self.action = action

        def respond(self, _session):
            return ModelReply(message="run action", actions=[self.action], done=True)

    logger = InMemoryLogger()
    tools = build_builtin_registry(logger)
    engine = ExecutionEngine(
        model=SingleActionModel(ToolAction(name="read_file", arguments={"path": "target.txt"})),
        tools=tools,
        guardrails=Guardrails(policy=DefaultPolicy(mode="auto"), logger=logger),
        logger=logger,
    )
    first = engine.execute(UserRequest(prompt="read target", cwd=str(tmp_path)))

    engine.model = SingleActionModel(ToolAction(name="patch", arguments={"diff": diff}))
    second = engine.execute(UserRequest(prompt="patch target", cwd=str(tmp_path)))

    assert first.tool_results[0].success is True
    assert second.tool_results[0].error_code == "file_not_read"
    assert target.read_text(encoding="utf-8") == "before\n"


def test_engine_requests_workspace_access_for_outside_read_path(tmp_path):
    outside = tmp_path.parent

    class OutsideReadModel:
        def respond(self, _session):
            return ModelReply(
                message="list outside",
                actions=[ToolAction(name="list_dir", arguments={"path": str(outside)})],
                done=True,
            )

    approvals = []
    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=OutsideReadModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
        approval_callback=lambda action, _reason: approvals.append(action.name) or True,
    )

    summary = engine.execute(UserRequest(prompt="list parent", cwd=str(tmp_path)))

    assert approvals == ["workspace_access"]
    assert summary.tool_results[0].success is True
    assert summary.tool_results[0].metadata["workspace_grant"] == str(outside.resolve())


def test_engine_requests_workspace_access_for_shell_cd_escape(tmp_path):
    class OutsideShellModel:
        def respond(self, _session):
            return ModelReply(
                message="pwd parent",
                actions=[ToolAction(name="shell_exec", arguments={"command": "cd .. && pwd"})],
                done=True,
            )

    approvals = []
    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=OutsideShellModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
        approval_callback=lambda action, _reason: approvals.append(action.name) or True,
    )

    summary = engine.execute(UserRequest(prompt="cd parent", cwd=str(tmp_path)))

    assert approvals == ["shell_exec", "workspace_access"]
    assert summary.tool_results[0].success is True
    assert summary.tool_results[0].metadata["stdout"].strip() == str(tmp_path.parent)
    assert summary.tool_results[0].metadata["workspace_grant"] == str(tmp_path.parent.resolve())


def test_engine_reports_denied_workspace_access(tmp_path):
    class OutsideReadModel:
        def respond(self, _session):
            return ModelReply(
                message="list outside",
                actions=[ToolAction(name="list_dir", arguments={"path": str(tmp_path.parent)})],
                done=True,
            )

    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=OutsideReadModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
        approval_callback=lambda _action, _reason: False,
    )

    summary = engine.execute(UserRequest(prompt="list parent", cwd=str(tmp_path)))

    assert summary.tool_results[0].success is False
    assert summary.tool_results[0].error_code == "approval_denied"
    assert "declined by the user" in summary.tool_results[0].output


def test_engine_reports_user_denial_separately_from_missing_approval(tmp_path):
    class ShellModel:
        def respond(self, _session):
            return ModelReply(
                message="run echo",
                actions=[ToolAction(name="shell_exec", arguments={"command": "echo hello"})],
                done=True,
            )

    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=ShellModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
        approval_callback=lambda _action, _reason: False,
    )

    summary = engine.execute(UserRequest(prompt="run echo", cwd=str(tmp_path)))

    assert summary.tool_results[0].error_code == "approval_denied"
    assert summary.tool_results[0].output == "Tool execution was declined by the user."


def test_engine_skips_duplicate_successful_tool_action(tmp_path):
    class DuplicateShellModel:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            if self.calls <= 2:
                return ModelReply(
                    message="write marker",
                    actions=[ToolAction(name="shell_exec", arguments={"command": "printf x >> marker.txt"})],
                    done=False,
                )
            return ModelReply(message="done", done=True)

    approvals = []
    logger = InMemoryLogger()
    model = DuplicateShellModel()
    engine = ExecutionEngine(
        model=model,
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
        approval_callback=lambda action, _reason: approvals.append(action.name) or True,
    )

    summary = engine.execute(UserRequest(prompt="write marker", cwd=str(tmp_path)))

    assert summary.final_message == "done"
    assert approvals == ["shell_exec"]
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8") == "x"
    assert summary.tool_results[1].success is True
    assert summary.tool_results[1].error_code is None
    assert summary.tool_results[1].metadata["duplicate"] is True
    assert summary.tool_results[1].metadata["duplicate_count"] == 1
    logged_results = [event for event in logger.events if event.name == "tool.result"]
    assert len(logged_results) == 2
    assert logged_results[1].payload["metadata"]["duplicate"] is True


def test_engine_stops_repeated_duplicate_tool_actions(tmp_path):
    class RepeatingDuplicateModel:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            return ModelReply(
                message="repeat",
                actions=[ToolAction(name="shell_exec", arguments={"command": "printf x >> marker.txt"})],
                done=False,
            )

    approvals = []
    logger = InMemoryLogger()
    model = RepeatingDuplicateModel()
    engine = ExecutionEngine(
        model=model,
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
        approval_callback=lambda action, _reason: approvals.append(action.name) or True,
    )

    summary = engine.execute(UserRequest(prompt="write marker", cwd=str(tmp_path)))

    assert model.calls == 6
    assert approvals == ["shell_exec"]
    assert "repeated the same action" in summary.final_message
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8") == "x"
    assert [result.error_code for result in summary.tool_results] == [
        None,
        None,
        None,
        None,
        "duplicate_tool_call",
        "duplicate_tool_call",
    ]
    logged_results = [event for event in logger.events if event.name == "tool.result"]
    assert len(logged_results) == len(summary.tool_results)


def test_engine_recovers_read_duplicate_loop_for_file_change_request(tmp_path):
    class InspectLoopThenPatchModel:
        calls = 0

        def respond(self, session):
            self.calls += 1
            if any("progress_required" in item for item in session.history):
                return ModelReply(
                    message="created standard metadata",
                    actions=[
                        ToolAction(
                            name="patch",
                            arguments={
                                "diff": "--- /dev/null\n+++ b/pyproject.toml\n@@ -0,0 +1,2 @@\n+[project]\n+name = \"demo\"\n"
                            },
                        )
                    ],
                    done=True,
                )
            return ModelReply(
                message="inspect project",
                actions=[ToolAction(name="list_dir", arguments={"path": "."})],
                done=False,
            )

    logger = InMemoryLogger()
    model = InspectLoopThenPatchModel()
    engine = ExecutionEngine(
        model=model,
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(mode="auto"), logger=logger),
        logger=logger,
    )

    summary = engine.execute(UserRequest(prompt="升级这个项目，生成标准项目文件夹", cwd=str(tmp_path)))

    assert model.calls == 3
    assert summary.final_message == "created standard metadata"
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == '[project]\nname = "demo"\n'
    assert any(result.error_code == "progress_required" for result in summary.tool_results)
    assert summary.tool_results[-1].name == "patch"
    assert summary.tool_results[-1].success is True


def test_blocked_write_does_not_reset_completed_read_context(tmp_path):
    target = tmp_path / "config.py"
    target.write_text("API_KEY = None\n", encoding="utf-8")

    class ReadBlockedWriteReadModel:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            if self.calls in {1, 3}:
                return ModelReply(
                    message="read config",
                    actions=[
                        ToolAction(
                            name="read_file",
                            arguments={"path": "config.py"},
                        )
                    ],
                    done=self.calls == 3,
                )
            return ModelReply(
                message="unsafe patch",
                actions=[
                    ToolAction(
                        name="patch",
                        arguments={
                            "diff": (
                                "--- a/config.py\n"
                                "+++ b/config.py\n"
                                "@@ -1 +1 @@\n"
                                "-API_KEY = None\n"
                                '+API_KEY = "1234567890abcdef1234567890abcdef"\n'
                            )
                        },
                    )
                ],
                done=False,
            )

    logger = InMemoryLogger()
    summary = ExecutionEngine(
        model=ReadBlockedWriteReadModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(DefaultPolicy(mode="auto"), logger),
        logger=logger,
    ).execute(
        UserRequest(prompt="modify config.py", cwd=str(tmp_path))
    )

    assert summary.tool_results[1].error_code == "blocked_by_security_policy"
    assert summary.tool_results[2].metadata["duplicate"] is True
    assert target.read_text(encoding="utf-8") == "API_KEY = None\n"


def test_progress_guard_can_trigger_again_after_successful_write(tmp_path):
    class TwoGenerationLoopModel:
        calls = 0

        def respond(self, session):
            self.calls += 1
            recovery_count = sum(
                "progress_required" in item
                for item in session.history
            )
            if recovery_count >= 2:
                return ModelReply(message="done", done=True)
            if recovery_count == 1 and not any(
                result.name == "patch" and result.success
                for result in session.tool_results
            ):
                return ModelReply(
                    message="write marker",
                    actions=[
                        ToolAction(
                            name="patch",
                            arguments={
                                "diff": (
                                    "--- /dev/null\n"
                                    "+++ b/marker.txt\n"
                                    "@@ -0,0 +1 @@\n"
                                    "+written\n"
                                )
                            },
                        )
                    ],
                    done=False,
                )
            return ModelReply(
                message="inspect",
                actions=[
                    ToolAction(
                        name="list_dir",
                        arguments={"path": "."},
                    )
                ],
                done=False,
            )

    logger = InMemoryLogger()
    summary = ExecutionEngine(
        model=TwoGenerationLoopModel(),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(DefaultPolicy(mode="auto"), logger),
        logger=logger,
    ).execute(
        UserRequest(prompt="create project files", cwd=str(tmp_path))
    )

    recoveries = [
        result
        for result in summary.tool_results
        if result.error_code == "progress_required"
    ]
    assert len(recoveries) == 2
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8") == "written\n"


def test_engine_skips_duplicate_non_retryable_failures(tmp_path):
    class MissingPathModel:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            return ModelReply(
                message="read missing",
                actions=[ToolAction(name="read_file", arguments={"path": "missing.py"})],
                done=False,
            )

    logger = InMemoryLogger()
    model = MissingPathModel()
    engine = ExecutionEngine(
        model=model,
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(mode="auto"), logger=logger),
        logger=logger,
    )

    summary = engine.execute(UserRequest(prompt="read missing", cwd=str(tmp_path)))

    assert model.calls == 2
    assert summary.tool_results[0].error_code == "path_not_found"
    assert summary.tool_results[1].metadata["duplicate"] is True
    assert "repeated the same action" in summary.final_message
    logged_results = [event for event in logger.events if event.name == "tool.result"]
    assert len(logged_results) == 2
    assert logged_results[1].payload["error_code"] == "duplicate_tool_call"


def test_engine_persists_shell_cd_within_execute(tmp_path):
    (tmp_path / "child").mkdir()

    class CdThenPwdModel:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            if self.calls == 1:
                return ModelReply(
                    message="enter directory",
                    actions=[ToolAction(name="shell_exec", arguments={"command": "cd child"})],
                    done=False,
                )
            return ModelReply(
                message="check directory",
                actions=[ToolAction(name="shell_exec", arguments={"command": "pwd"})],
                done=True,
            )

    logger = InMemoryLogger()
    model = CdThenPwdModel()
    engine = ExecutionEngine(
        model=model,
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
        approval_callback=lambda _action, _reason: True,
    )

    summary = engine.execute(UserRequest(prompt="cd child", cwd=str(tmp_path)))

    assert model.calls == 2
    assert summary.tool_results[0].metadata["cwd"] == str(tmp_path / "child")
    assert summary.tool_results[1].metadata["stdout"].strip() == str(tmp_path / "child")


def test_engine_persists_shell_cd_across_same_session_executes(tmp_path):
    (tmp_path / "child").mkdir()

    class SingleActionModel:
        def __init__(self, action):
            self.action = action

        def respond(self, _session):
            return ModelReply(message="run action", actions=[self.action], done=True)

    logger = InMemoryLogger()
    engine = ExecutionEngine(
        model=SingleActionModel(ToolAction(name="shell_exec", arguments={"command": "cd child"})),
        tools=build_builtin_registry(logger),
        guardrails=Guardrails(policy=DefaultPolicy(), logger=logger),
        logger=logger,
        approval_callback=lambda _action, _reason: True,
    )

    request_metadata = {"session_id": "session-1"}
    first = engine.execute(UserRequest(prompt="cd child", cwd=str(tmp_path), metadata=request_metadata))

    engine.model = SingleActionModel(ToolAction(name="shell_exec", arguments={"command": "pwd"}))
    second = engine.execute(UserRequest(prompt="pwd", cwd=str(tmp_path), metadata=request_metadata))

    engine.model = SingleActionModel(ToolAction(name="shell_exec", arguments={"command": "pwd"}))
    third = engine.execute(UserRequest(prompt="pwd", cwd=str(tmp_path), metadata={"session_id": "session-2"}))

    assert first.tool_results[0].metadata["cwd"] == str(tmp_path / "child")
    assert second.tool_results[0].metadata["stdout"].strip() == str(tmp_path / "child")
    assert third.tool_results[0].metadata["stdout"].strip() == str(tmp_path)


def test_session_store_lists_latest_first(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    first = store.create(cwd="/repo/a", messages=[{"role": "user", "content": "first prompt"}])
    second = store.create(cwd="/repo/b", messages=[{"role": "user", "content": "second prompt"}])

    sessions = store.list_sessions()

    assert [item.session_id for item in sessions] == [second.session_id, first.session_id]
    assert sessions[0].preview == "second prompt"


def test_session_store_loads_legacy_session_without_run_ids(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    session = store.create(cwd="/repo", messages=[{"role": "user", "content": "hello"}])
    path = store.base_dir / f"{session.session_id}.json"
    payload = path.read_text(encoding="utf-8")
    path.write_text(payload.replace(',\n  "run_ids": []', ""), encoding="utf-8")

    loaded = store.load(session.session_id)

    assert loaded is not None
    assert loaded.run_ids == []


def test_session_store_skips_corrupt_records_and_rejects_path_traversal(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    valid = store.create(cwd="/repo", messages=[{"role": "user", "content": "valid"}])
    store.base_dir.mkdir(parents=True, exist_ok=True)
    (store.base_dir / "broken.json").write_text("{not json", encoding="utf-8")

    sessions = store.list_sessions()

    assert [item.session_id for item in sessions] == [valid.session_id]
    assert store.load("../outside") is None
    assert store.latest() is not None


def test_session_store_latest_returns_none_when_empty(tmp_path):
    store = SessionStore(base_dir=tmp_path)

    assert store.list_sessions() == []
    assert store.latest() is None


def test_session_store_normalizes_invalid_messages_and_run_ids(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    session = store.create(cwd="/repo")
    path = store.base_dir / f"{session.session_id}.json"
    payload = {
        "session_id": session.session_id,
        "cwd": "/repo",
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "status": "active",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": 42},
            "bad",
        ],
        "run_ids": ["run-1", "", 2],
    }
    import json

    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.load(session.session_id)

    assert loaded is not None
    assert loaded.messages == [{"role": "user", "content": "hello"}]
    assert loaded.run_ids == ["run-1"]


def test_chat_persists_and_closes_session(tmp_path, monkeypatch):
    store = SessionStore(base_dir=tmp_path)
    cli = CLI(engine=None, presenter=ConsolePresenter(), logger=None, session_store=store)

    answers = iter(["hello", "quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        cli,
        "_run_once",
        lambda request: type("Summary", (), {"final_message": f"echo:{request.prompt}", "tool_results": []})(),
    )

    cli.chat(cwd=str(tmp_path))

    sessions = store.list_sessions()
    assert len(sessions) == 1
    stored = store.load(sessions[0].session_id)
    assert stored is not None
    assert stored.status == "closed"
    assert stored.messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "echo:hello"},
    ]


def test_cli_creates_one_run_directory_per_request(tmp_path):
    class EchoEngine:
        def execute(self, request):
            return type("Summary", (), {"final_message": f"echo:{request.prompt}", "tool_results": []})()

    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    cli = CLI(engine=EchoEngine(), presenter=ConsolePresenter(), logger=logger)

    cli.run(UserRequest(prompt="first", cwd=str(tmp_path)))
    first_run_id = logger.last_run_id
    cli.run(UserRequest(prompt="second", cwd=str(tmp_path)))
    second_run_id = logger.last_run_id

    assert first_run_id is not None
    assert second_run_id is not None
    assert first_run_id != second_run_id
    assert (tmp_path / "runs" / first_run_id / "events.jsonl").exists()
    assert (tmp_path / "runs" / second_run_id / "events.jsonl").exists()
    assert "first" in (tmp_path / "runs" / first_run_id / "details.log").read_text(encoding="utf-8")
    assert "second" in (tmp_path / "runs" / second_run_id / "details.log").read_text(encoding="utf-8")


def test_chat_persists_run_ids_on_session(tmp_path, monkeypatch):
    class EchoEngine:
        def execute(self, request):
            return type("Summary", (), {"final_message": f"echo:{request.prompt}", "tool_results": []})()

    store = SessionStore(base_dir=tmp_path)
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    cli = CLI(
        engine=EchoEngine(),
        presenter=ConsolePresenter(),
        logger=logger,
        session_store=store,
    )

    answers = iter(["hello", "quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    cli.chat(cwd=str(tmp_path))

    sessions = store.list_sessions()
    stored = store.load(sessions[0].session_id)
    assert stored is not None
    assert stored.run_ids == [logger.last_run_id]
    assert (tmp_path / "runs" / stored.run_ids[0] / "events.jsonl").exists()


def test_chat_only_passes_recent_session_trace_to_execution(tmp_path, monkeypatch):
    class CapturingEngine:
        context_loaders = []

        def __init__(self):
            self.trace = None

        def execute(self, request):
            self.trace = request.metadata["session_trace"]
            return type("Summary", (), {"final_message": "done", "tool_results": []})()

    store = SessionStore(base_dir=tmp_path)
    session = store.create(cwd=str(tmp_path))
    session.trace = [
        SessionRunTrace(
            run_id=f"run-{index}",
            started_at="",
            completed_at="",
            prompt=f"prompt-{index}",
            final_message="done",
            outcome="completed",
            event_count=0,
            turn_count=0,
        )
        for index in range(8)
    ]
    store.save(session)
    engine = CapturingEngine()
    cli = CLI(
        engine=engine,
        presenter=ConsolePresenter(),
        logger=InMemoryLogger(base_dir=str(tmp_path / "runs")),
        session_store=store,
    )
    answers = iter(["continue", "quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    cli.chat(cwd=str(tmp_path), session_id=session.session_id)

    assert [item.run_id for item in engine.trace] == [f"run-{index}" for index in range(2, 8)]


def test_chat_persists_run_id_before_execute_finishes(tmp_path, monkeypatch):
    class InspectingEngine:
        def __init__(self, store):
            self.store = store
            self.seen_run_ids = None

        def execute(self, _request):
            sessions = self.store.list_sessions()
            stored = self.store.load(sessions[0].session_id)
            self.seen_run_ids = list(stored.run_ids)
            return type("Summary", (), {"final_message": "done", "tool_results": []})()

    store = SessionStore(base_dir=tmp_path)
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    engine = InspectingEngine(store)
    cli = CLI(
        engine=engine,
        presenter=ConsolePresenter(),
        logger=logger,
        session_store=store,
    )

    answers = iter(["hello", "quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    cli.chat(cwd=str(tmp_path))

    assert engine.seen_run_ids == [logger.last_run_id]


def test_session_store_persists_session_trace_and_writes_trace_log(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    session = store.create(
        cwd=str(tmp_path),
        messages=[
            {"role": "user", "content": "inspect workspace"},
            {"role": "assistant", "content": "done"},
        ],
    )
    session.active_capability_ids = ["mcp:amap:maps_direction_driving"]
    session.trace.append(
        SessionRunTrace(
            run_id="run-1",
            started_at="2026-07-10T02:00:00Z",
            completed_at="2026-07-10T02:00:03Z",
            prompt="inspect workspace",
            final_message="done",
            outcome="completed",
            event_count=6,
            turn_count=1,
            tool_names=["list_dir"],
            turns=[
                SessionTurnTrace(
                    turn=1,
                    message="Model requested tool calls.",
                    actions=["list_dir"],
                    tool_results=["list_dir:ok"],
                    action_details=['list_dir args={"path":"."}'],
                    tool_result_details=["list_dir [ok] found 3 entries"],
                )
            ],
        )
    )

    store.save(session)
    loaded = store.load(session.session_id)

    assert loaded is not None
    assert len(loaded.trace) == 1
    assert loaded.trace[0].run_id == "run-1"
    assert loaded.resume_state.last_run_id == "run-1"
    assert loaded.resume_state.last_outcome == "completed"
    assert loaded.active_capability_ids == ["mcp:amap:maps_direction_driving"]
    trace_log = tmp_path / ".testcode" / "sessions" / f"{session.session_id}.trace.log"
    assert trace_log.exists()
    text = trace_log.read_text(encoding="utf-8")
    assert "Session Trace Summary" in text
    assert "Run run-1" in text
    assert "- prompt: inspect workspace" in text
    assert 'action detail: list_dir args={"path":"."}' not in text
    assert "result detail: list_dir [ok] found 3 entries" not in text
    replay_log = tmp_path / ".testcode" / "sessions" / f"{session.session_id}.replay.log"
    assert replay_log.exists() is False


def test_session_store_tolerates_null_trace_fields(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    session_id = "session-with-null-trace"
    store.base_dir.mkdir(parents=True)
    (store.base_dir / f"{session_id}.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "cwd": str(tmp_path),
                "created_at": "2026-07-10T02:00:00Z",
                "updated_at": "2026-07-10T02:00:00Z",
                "messages": [],
                "active_skills": None,
                "trace": [
                    {
                        "run_id": "run-1",
                        "turns": [
                            {
                                "turn": None,
                                "actions": None,
                                "tool_results": None,
                                "action_details": None,
                                "tool_result_details": None,
                            }
                        ],
                        "event_count": None,
                        "turn_count": None,
                        "tool_names": None,
                    }
                ],
                "resume_state": {"last_tool_names": None},
            }
        ),
        encoding="utf-8",
    )

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.active_skills == []
    assert loaded.trace[0].event_count == 0
    assert loaded.trace[0].turns[0].actions == []
    assert loaded.resume_state.last_tool_names == []
    assert [item.session_id for item in store.list_sessions()] == [session_id]


def test_session_resume_state_uses_latest_interrupted_trace(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    session = store.create(
        cwd=str(tmp_path),
        messages=[
            {"role": "user", "content": "first task"},
            {"role": "assistant", "content": "first task completed"},
        ],
    )
    session.trace.append(
        SessionRunTrace(
            run_id="run-2",
            started_at="2026-07-10T02:00:00Z",
            completed_at="2026-07-10T02:00:01Z",
            prompt="second task",
            final_message="Interrupted",
            outcome="interrupted",
            event_count=2,
            turn_count=0,
        )
    )

    store.save(session)

    assert session.resume_state.last_run_id == "run-2"
    assert session.resume_state.last_user_prompt == "second task"
    assert session.resume_state.last_assistant_message == "Interrupted"
    assert session.resume_state.last_outcome == "interrupted"


def test_chat_persists_session_trace_from_logger_summary(tmp_path, monkeypatch):
    class EchoEngine:
        def execute(self, request):
            return type("Summary", (), {"final_message": f"echo:{request.prompt}", "tool_results": []})()

    store = SessionStore(base_dir=tmp_path)
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    cli = CLI(
        engine=EchoEngine(),
        presenter=ConsolePresenter(),
        logger=logger,
        session_store=store,
    )

    answers = iter(["hello", "quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    cli.chat(cwd=str(tmp_path))

    sessions = store.list_sessions()
    stored = store.load(sessions[0].session_id)
    assert stored is not None
    assert len(stored.trace) == 1
    assert stored.trace[0].prompt == "hello"
    assert stored.trace[0].final_message == "echo:hello"
    assert stored.resume_state.last_run_id == stored.trace[0].run_id
    assert stored.resume_state.last_user_prompt == "hello"
    assert stored.resume_state.last_assistant_message == "echo:hello"
    trace_log = tmp_path / ".testcode" / "sessions" / f"{stored.session_id}.trace.log"
    trace_text = trace_log.read_text(encoding="utf-8")
    assert "Resume State" in trace_text
    assert "Action detail:" not in trace_text
    replay_log = tmp_path / ".testcode" / "sessions" / f"{stored.session_id}.replay.log"
    assert replay_log.exists() is False


def test_chat_persists_interrupted_trace_and_resets_logger(tmp_path, monkeypatch):
    class InterruptEngine:
        context_loaders = []
        current_session = None

        def execute(self, _request):
            self.current_session = type("Session", (), {"tool_results": []})()
            raise KeyboardInterrupt

        def _finish(self, summary):
            self.current_session = None
            return summary

    store = SessionStore(base_dir=tmp_path)
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    cli = CLI(
        engine=InterruptEngine(),
        presenter=ConsolePresenter(),
        logger=logger,
        session_store=store,
    )
    answers = iter(["work", "quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    cli.chat(cwd=str(tmp_path))

    stored = store.load(store.list_sessions()[0].session_id)
    assert stored is not None
    assert len(stored.trace) == 1
    assert stored.trace[0].prompt == "work"
    assert stored.trace[0].outcome == "interrupted"
    assert stored.resume_state.last_outcome == "interrupted"
    assert logger.run_dir is None


def test_chat_close_cleans_up_runtime_tool_state(tmp_path, monkeypatch):
    class Tools:
        reset_count = 0

        def reset_state(self):
            self.reset_count += 1

    class Engine:
        tools = Tools()

    store = SessionStore(base_dir=tmp_path)
    cli = CLI(
        engine=Engine(),
        presenter=ConsolePresenter(),
        session_store=store,
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "quit")

    cli.chat(cwd=str(tmp_path))

    assert cli.engine.tools.reset_count == 1
    stored = store.load(store.list_sessions()[0].session_id)
    assert stored is not None
    assert stored.status == "closed"


def test_interrupted_run_cleans_up_runtime_tool_state(tmp_path):
    class Tools:
        reset_count = 0

        def reset_state(self):
            self.reset_count += 1

    class Engine:
        tools = Tools()
        current_session = None

        def execute(self, _request):
            raise KeyboardInterrupt

        def _finish(self, _summary):
            pass

    cli = CLI(engine=Engine(), presenter=ConsolePresenter())

    with pytest.raises(KeyboardInterrupt):
        cli.run(UserRequest(prompt="run", cwd=str(tmp_path)))

    assert cli.engine.tools.reset_count == 1


def test_engine_interrupt_cleans_up_runtime_tool_state_for_direct_call(tmp_path):
    class Tools:
        reset_count = 0

        def definitions(self):
            return []

        def provider_statuses(self):
            return []

        def reset_state(self):
            self.reset_count += 1

    class InterruptingModel:
        def respond(self, _session):
            raise KeyboardInterrupt

    tools = Tools()
    engine = ExecutionEngine(
        model=InterruptingModel(),
        tools=tools,
        guardrails=Guardrails(policy=DefaultPolicy(), logger=InMemoryLogger()),
        logger=InMemoryLogger(),
    )

    with pytest.raises(KeyboardInterrupt):
        engine.execute(UserRequest(prompt="run", cwd=str(tmp_path), metadata={"session_id": "session-1"}))

    assert tools.reset_count == 2
    assert engine.current_session is None


def test_logger_summary_captures_action_and_result_details(tmp_path):
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    request = UserRequest(prompt="inspect", cwd=str(tmp_path))
    logger.start_run(request)
    logger.record("model.request", {"messages": [{"role": "user", "content": "inspect"}]})
    logger.record("tool.execute", {"name": "read_file", "arguments": {"path": "README.md"}})
    logger.record(
        "tool.result",
        {
            "name": "read_file",
            "success": True,
            "output": "hello world",
            "error_code": None,
            "metadata": {"action_arguments": {"path": "README.md"}},
        },
    )
    logger.record("model.reply", {"turn": 1, "message": "done", "done": True, "actions": []})
    summary = type(
        "Summary",
        (),
        {
            "final_message": "done",
            "tool_results": [
                type(
                    "ToolResultLike",
                    (),
                    {
                        "name": "read_file",
                        "success": True,
                        "output": "hello world",
                        "error_code": None,
                        "metadata": {"action_arguments": {"path": "README.md"}},
                    },
                )()
            ],
            "active_skills": [],
        },
    )()
    logger.finalize(request, summary)

    run_summary = logger.last_run_summary
    assert run_summary is not None
    assert run_summary.turns[0].action_details[0].startswith('read_file args=')
    assert "README.md" in run_summary.turns[0].action_details[0]
    assert run_summary.turns[0].tool_result_details[0].startswith("read_file [ok]")
