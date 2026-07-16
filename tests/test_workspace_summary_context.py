from __future__ import annotations

import subprocess

import pytest

from testcode.app import create_app
from testcode.context import WorkspaceSummaryLoader
from testcode.model.prompt import ModelPromptBuilder
from testcode.observability.logger import InMemoryLogger
from testcode.orchestration.session import SessionContext
from testcode.types import UserRequest


def test_workspace_summary_detects_project_markers_and_tree(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node test.js"}}\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    request = UserRequest(prompt="inspect", cwd=str(tmp_path))
    session = SessionContext(request=request)

    WorkspaceSummaryLoader().load_context(request, session)

    assert session.workspace_summary is not None
    signals = {(signal.language, signal.marker) for signal in session.workspace_summary.project_signals}
    assert ("Python", "pyproject.toml") in signals
    assert ("Node.js", "package.json") in signals
    assert "src/" in session.workspace_summary.tree
    assert "src/app.py" in session.workspace_summary.tree
    assert ".git/" not in session.workspace_summary.tree


def test_workspace_summary_reads_clean_git_status(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True, text=True)
    request = UserRequest(prompt="inspect", cwd=str(tmp_path))
    session = SessionContext(request=request)

    WorkspaceSummaryLoader().load_context(request, session)

    assert session.workspace_summary is not None
    assert session.workspace_summary.git is not None
    assert session.workspace_summary.git.status == "clean"
    assert session.workspace_summary.git.recent_commit is not None
    assert "initial" in session.workspace_summary.git.recent_commit


def test_workspace_summary_limits_tree_entries(tmp_path):
    for index in range(5):
        (tmp_path / f"file_{index}.txt").write_text(str(index), encoding="utf-8")
    request = UserRequest(prompt="inspect", cwd=str(tmp_path))
    session = SessionContext(request=request)

    WorkspaceSummaryLoader(max_tree_entries=3).load_context(request, session)

    assert session.workspace_summary is not None
    assert len(session.workspace_summary.tree) == 3
    assert session.workspace_summary.tree_truncated is True


def test_prompt_includes_workspace_summary(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
    request = UserRequest(prompt="inspect", cwd=str(tmp_path))
    session = SessionContext(request=request)
    WorkspaceSummaryLoader().load_context(request, session)

    messages = ModelPromptBuilder().build_messages(session)
    system = str(messages[0]["content"])

    assert "### Workspace Summary:" in system
    assert "Automatically collected context. Use it only when relevant to the user request." in system
    assert "Go: go.mod; suggested tests: go test ./..." in system
    assert "Available tools:" in system
    assert system.index("### Workspace Summary:") < system.index("Available tools:")


def test_workspace_summary_loader_logs_context_event(tmp_path):
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    request = UserRequest(prompt="inspect", cwd=str(tmp_path))
    logger.start_run(request)
    session = SessionContext(request=request)

    WorkspaceSummaryLoader(logger=logger).load_context(request, session)

    assert logger.events[-1].name == "context.workspace_summary"
    assert logger.events[-1].payload["root"] == str(tmp_path)


def test_workspace_summary_skips_non_project_external_service_request(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    request = UserRequest(prompt="使用高德 MCP 查询留仙洞到梅塘路线", cwd=str(tmp_path))
    logger.start_run(request)
    session = SessionContext(request=request)

    WorkspaceSummaryLoader(logger=logger).load_context(request, session)

    assert session.workspace_summary is None
    assert logger.events[-1].name == "context.workspace_summary.skipped"
    assert logger.events[-1].payload["reason"] == "non_project_request"


@pytest.mark.parametrize(
    "prompt",
    [
        "What is the genetic code?",
        "How does a blood test work?",
        "What makes a successful science project?",
    ],
)
def test_workspace_summary_skips_general_questions_with_ambiguous_project_words(
    tmp_path,
    prompt,
):
    request = UserRequest(prompt=prompt, cwd=str(tmp_path))
    session = SessionContext(request=request)

    WorkspaceSummaryLoader().load_context(request, session)

    assert session.workspace_summary is None


@pytest.mark.parametrize(
    "prompt",
    [
        "review the code changes",
        "fix the project tests",
        "检查并修复 MCP integration code",
    ],
)
def test_workspace_summary_keeps_project_action_and_target_requests(tmp_path, prompt):
    request = UserRequest(prompt=prompt, cwd=str(tmp_path))
    session = SessionContext(request=request)

    WorkspaceSummaryLoader().load_context(request, session)

    assert session.workspace_summary is not None


def test_workspace_summary_can_be_explicitly_enabled_for_non_project_prompt(tmp_path):
    request = UserRequest(
        prompt="查询路线",
        cwd=str(tmp_path),
        metadata={"include_workspace_context": True},
    )
    session = SessionContext(request=request)

    WorkspaceSummaryLoader().load_context(request, session)

    assert session.workspace_summary is not None


def test_create_app_registers_workspace_summary_loader(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "")
    monkeypatch.chdir(tmp_path)

    app = create_app()

    loader_names = [loader.__class__.__name__ for loader in app.engine.context_loaders]
    assert loader_names == [
        "ProjectRulesLoader",
        "WorkspaceSummaryLoader",
        "ExplicitContextLoader",
    ]
