import sys
from types import SimpleNamespace

import pytest

import testcode.app as app_module
from testcode import __version__


class FakePresenter:
    def __init__(self):
        self.session_lists = []

    def show_session_list(self, sessions):
        self.session_lists.append(sessions)


class FakeApp:
    def __init__(self):
        self.presenter = FakePresenter()
        self.runs = []
        self.chats = []
        self.persisted_runs = []

    def run(self, request):
        self.runs.append(request)
        return SimpleNamespace(final_message="done", tool_results=[], active_skills=[])

    def persist_run(self, session, prompt, summary, **options):
        self.persisted_runs.append((session, prompt, summary, options))

    def chat(self, **kwargs):
        self.chats.append(kwargs)

    def list_sessions(self):
        return ["session-record"]

    def load_session(self, _session_id):
        return None

    def latest_session(self):
        return None


def test_main_version_reports_package_version(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["testcode", "--version"])

    with pytest.raises(SystemExit) as exit_info:
        app_module.main()

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"testcode {__version__}"


def test_main_once_dispatches_prompt_to_app_run(monkeypatch, tmp_path):
    fake = FakeApp()
    monkeypatch.setattr(sys, "argv", ["testcode", "--once", "inspect", "workspace"])
    monkeypatch.setattr(app_module, "create_app", lambda mode, **_kwargs: fake)
    monkeypatch.chdir(tmp_path)

    app_module.main()

    assert len(fake.runs) == 1
    assert fake.runs[0].prompt == "inspect workspace"
    assert fake.runs[0].cwd == str(tmp_path)


def test_main_once_passes_context_paths_to_request_metadata(monkeypatch, tmp_path):
    fake = FakeApp()
    monkeypatch.setattr(
        sys,
        "argv",
        ["testcode", "--once", "--context", "README.md", "--context", "docs/*.md", "inspect"],
    )
    monkeypatch.setattr(app_module, "create_app", lambda mode, **_kwargs: fake)
    monkeypatch.chdir(tmp_path)

    app_module.main()

    assert fake.runs[0].metadata["context_paths"] == ["README.md", "docs/*.md"]


def test_main_once_creates_persisted_session_for_subagent_runtime(monkeypatch, tmp_path):
    fake = FakeApp()
    created = []

    class Store:
        def create(self, cwd):
            session = SimpleNamespace(
                session_id="session-once",
                cwd=cwd,
                messages=[],
                active_skills=[],
                active_capability_ids=[],
                trace=[],
                resume_state=None,
            )
            created.append(session)
            return session

    fake.session_store = Store()
    monkeypatch.setattr(sys, "argv", ["testcode", "--once", "delegate"])
    monkeypatch.setattr(app_module, "create_app", lambda mode, **_kwargs: fake)
    monkeypatch.chdir(tmp_path)

    app_module.main()

    assert len(created) == 1
    assert fake.runs[0].metadata["session_id"] == "session-once"
    assert fake.persisted_runs[0][0] is created[0]
    assert fake.persisted_runs[0][3] == {"status": "closed", "close_runtime": True}


def test_main_once_resume_persists_completed_run(monkeypatch, tmp_path):
    fake = FakeApp()
    session = SimpleNamespace(
        session_id="session-1",
        cwd=str(tmp_path),
        messages=[{"role": "user", "content": "before"}],
        active_skills=[],
        trace=list(range(8)),
        resume_state=None,
    )
    fake.load_session = lambda _session_id: session
    monkeypatch.setattr(sys, "argv", ["testcode", "--once", "--resume", "session-1", "continue"])
    created_roots = []
    monkeypatch.setattr(
        app_module,
        "create_app",
        lambda mode, workspace_root=None: created_roots.append(workspace_root) or fake,
    )

    app_module.main()

    assert len(fake.persisted_runs) == 1
    assert fake.persisted_runs[0][0] is session
    assert fake.persisted_runs[0][1] == "continue"
    assert fake.persisted_runs[0][2].final_message == "done"
    assert fake.persisted_runs[0][3] == {"status": "closed", "close_runtime": True}
    assert fake.runs[0].metadata["session_trace"] == list(range(2, 8))
    assert created_roots[-1] == str(tmp_path)


def test_main_once_resume_persists_interrupted_run(monkeypatch, tmp_path):
    fake = FakeApp()
    session = SimpleNamespace(
        session_id="session-1",
        cwd=str(tmp_path),
        messages=[],
        active_skills=[],
        trace=[],
        resume_state=None,
    )
    fake.load_session = lambda _session_id: session

    def interrupt(_request):
        raise KeyboardInterrupt

    fake.run = interrupt
    monkeypatch.setattr(sys, "argv", ["testcode", "--once", "--resume", "session-1", "continue"])
    monkeypatch.setattr(app_module, "create_app", lambda mode, **_kwargs: fake)

    app_module.main()

    assert len(fake.persisted_runs) == 1
    assert fake.persisted_runs[0][2].final_message == "Interrupted"
    assert fake.persisted_runs[0][3] == {"status": "closed", "close_runtime": True}


def test_main_chat_passes_context_paths(monkeypatch, tmp_path):
    fake = FakeApp()
    monkeypatch.setattr(sys, "argv", ["testcode", "--context", "README.md", "inspect"])
    monkeypatch.setattr(app_module, "create_app", lambda mode, **_kwargs: fake)
    monkeypatch.chdir(tmp_path)

    app_module.main()

    assert fake.chats[0]["context_paths"] == ["README.md"]


def test_main_list_dispatches_to_presenter(monkeypatch):
    fake = FakeApp()
    monkeypatch.setattr(sys, "argv", ["testcode", "--list"])
    monkeypatch.setattr(app_module, "create_app", lambda mode, **_kwargs: fake)

    app_module.main()

    assert fake.presenter.session_lists == [["session-record"]]


def test_main_rejects_resume_and_last_together(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["testcode", "--resume", "abc", "--last"])
    monkeypatch.setattr(app_module, "create_app", lambda mode, **_kwargs: FakeApp())

    with pytest.raises(SystemExit, match="Use either --resume or --last"):
        app_module.main()
