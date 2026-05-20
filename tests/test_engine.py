from testcode.app import create_app
from testcode.interaction.cli import CLI
from testcode.interaction.presenter import ConsolePresenter
from testcode.observability.logger import InMemoryLogger
from testcode.orchestration.engine import ExecutionEngine
from testcode.safety.guardrails import Guardrails
from testcode.safety.policy import DefaultPolicy
from testcode.session_store import SessionStore
from testcode.tools.builtin import build_builtin_registry
from testcode.types import ModelReply, ToolAction, UserRequest


def test_scaffold_runs_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "")
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    app = create_app()
    summary = app.run(UserRequest(prompt="inspect workspace", cwd=str(tmp_path)))

    assert "scaffold is ready" in summary.final_message
    assert summary.tool_results
    assert summary.tool_results[0].success is True


def test_run_returns_message_when_model_request_fails(tmp_path, monkeypatch):
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
    assert summary.tool_results[1].metadata["duplicate"] is True


def test_session_store_lists_latest_first(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    first = store.create(cwd="/repo/a", messages=[{"role": "user", "content": "first prompt"}])
    second = store.create(cwd="/repo/b", messages=[{"role": "user", "content": "second prompt"}])

    sessions = store.list_sessions()

    assert [item.session_id for item in sessions] == [second.session_id, first.session_id]
    assert sessions[0].preview == "second prompt"


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
