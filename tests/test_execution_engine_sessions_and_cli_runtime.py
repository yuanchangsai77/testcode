from testcode.app import create_app
from testcode.interaction.cli import CLI
from testcode.interaction.presenter import ConsolePresenter
from testcode.observability.logger import InMemoryLogger
from testcode.orchestration.engine import ExecutionEngine
from testcode.safety.guardrails import Guardrails
from testcode.safety.policy import DefaultPolicy
from testcode.sessions import SessionStore
from testcode.tools.builtin import build_builtin_registry
from testcode.types import ModelReply, ToolAction, UserRequest


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
    assert summary.tool_results[0].error_code == "approval_required"
    assert "outside the current workspace" in summary.tool_results[0].output


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

    assert model.calls == 6
    assert summary.final_message == "created standard metadata"
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == '[project]\nname = "demo"\n'
    assert any(result.error_code == "progress_required" for result in summary.tool_results)
    assert summary.tool_results[-1].name == "patch"
    assert summary.tool_results[-1].success is True


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
