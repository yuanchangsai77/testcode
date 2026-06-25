import sys

import pytest

import testcode.app as app_module


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

    def run(self, request):
        self.runs.append(request)

    def chat(self, **kwargs):
        self.chats.append(kwargs)

    def list_sessions(self):
        return ["session-record"]

    def load_session(self, _session_id):
        return None

    def latest_session(self):
        return None


def test_main_once_dispatches_prompt_to_app_run(monkeypatch, tmp_path):
    fake = FakeApp()
    monkeypatch.setattr(sys, "argv", ["testcode", "--once", "inspect", "workspace"])
    monkeypatch.setattr(app_module, "create_app", lambda mode: fake)
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
    monkeypatch.setattr(app_module, "create_app", lambda mode: fake)
    monkeypatch.chdir(tmp_path)

    app_module.main()

    assert fake.runs[0].metadata["context_paths"] == ["README.md", "docs/*.md"]


def test_main_chat_passes_context_paths(monkeypatch, tmp_path):
    fake = FakeApp()
    monkeypatch.setattr(sys, "argv", ["testcode", "--context", "README.md", "inspect"])
    monkeypatch.setattr(app_module, "create_app", lambda mode: fake)
    monkeypatch.chdir(tmp_path)

    app_module.main()

    assert fake.chats[0]["context_paths"] == ["README.md"]


def test_main_list_dispatches_to_presenter(monkeypatch):
    fake = FakeApp()
    monkeypatch.setattr(sys, "argv", ["testcode", "--list"])
    monkeypatch.setattr(app_module, "create_app", lambda mode: fake)

    app_module.main()

    assert fake.presenter.session_lists == [["session-record"]]


def test_main_rejects_resume_and_last_together(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["testcode", "--resume", "abc", "--last"])
    monkeypatch.setattr(app_module, "create_app", lambda mode: FakeApp())

    with pytest.raises(SystemExit, match="Use either --resume or --last"):
        app_module.main()
