from testcode.app import create_app
from testcode.interaction.cli import CLI
from testcode.interaction.presenter import ConsolePresenter
from testcode.session_store import SessionStore
from testcode.types import UserRequest


def test_scaffold_runs_end_to_end(tmp_path):
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
