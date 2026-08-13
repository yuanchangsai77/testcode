from __future__ import annotations

from testcode.app import create_app
from testcode.context import ContextPackager, ProjectRulesLoader
from testcode.model.prompt import ModelPromptBuilder
from testcode.observability.logger import InMemoryLogger
from testcode.orchestration.session import SessionContext
from testcode.types import ToolDefinition, UserRequest


def test_project_rules_loader_reads_agents_from_root_to_cwd(tmp_path):
    root = tmp_path
    nested = root / "pkg" / "feature"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("root rule\n", encoding="utf-8")
    (nested / "AGENTS.md").write_text("nested rule\n", encoding="utf-8")

    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    request = UserRequest(prompt="inspect", cwd=str(nested))
    logger.start_run(request)
    session = SessionContext(request=request)

    ProjectRulesLoader(logger=logger).load_context(request, session)

    assert [rule.content for rule in session.project_rules] == ["root rule\n", "nested rule\n"]
    assert session.project_rules[0].path.endswith("AGENTS.md")
    assert logger.events[-1].name == "context.project_rules"
    assert [item["truncated"] for item in logger.events[-1].payload["files"]] == [False, False]


def test_project_rules_loader_stops_at_project_boundary(tmp_path):
    parent = tmp_path
    root = parent / "repo"
    nested = root / "pkg"
    nested.mkdir(parents=True)
    (parent / "AGENTS.md").write_text("parent rule\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("repo rule\n", encoding="utf-8")

    request = UserRequest(prompt="inspect", cwd=str(nested))
    session = SessionContext(request=request)

    ProjectRulesLoader().load_context(request, session)

    assert [rule.content for rule in session.project_rules] == ["repo rule\n"]


def test_project_rules_loader_uses_project_marker_boundary_without_git(tmp_path):
    parent = tmp_path
    root = parent / "repo"
    nested = root / "pkg"
    nested.mkdir(parents=True)
    (parent / "AGENTS.md").write_text("parent rule\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("repo rule\n", encoding="utf-8")

    request = UserRequest(prompt="inspect", cwd=str(nested))
    session = SessionContext(request=request)

    ProjectRulesLoader().load_context(request, session)

    assert [rule.content for rule in session.project_rules] == ["repo rule\n"]


def test_project_rules_loader_truncates_large_agents_file(tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("abcdef", encoding="utf-8")
    request = UserRequest(prompt="inspect", cwd=str(tmp_path))
    session = SessionContext(request=request)

    ProjectRulesLoader(max_bytes=3).load_context(request, session)

    assert len(session.project_rules) == 1
    assert session.project_rules[0].content == "abc"
    assert session.project_rules[0].truncated is True


def test_prompt_includes_project_rules_before_tools(tmp_path):
    (tmp_path / "AGENTS.md").write_text("prefer rg\n", encoding="utf-8")
    request = UserRequest(prompt="inspect", cwd=str(tmp_path))
    session = SessionContext(request=request)
    ProjectRulesLoader().load_context(request, session)

    messages = ModelPromptBuilder().build_messages(session)
    system = str(messages[0]["content"])

    assert "### Project Rules:" in system
    assert "prefer rg" in system
    assert system.index("### Project Rules:") < system.index("Available tools:")


def test_prompt_keeps_project_rules_in_small_context_budget(tmp_path):
    (tmp_path / "AGENTS.md").write_text("MANDATORY PROJECT RULE\n", encoding="utf-8")
    session = SessionContext(request=UserRequest(prompt="inspect", cwd=str(tmp_path)))
    ProjectRulesLoader().load_context(session.request, session)
    session.available_tools = [
        ToolDefinition(name=f"tool_{index}", description="x" * 500)
        for index in range(20)
    ]

    messages = ModelPromptBuilder(ContextPackager(4_000)).build_messages(session)

    assert "MANDATORY PROJECT RULE" in str(messages[0]["content"])


def test_create_app_registers_context_loaders(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "")
    monkeypatch.chdir(tmp_path)

    app = create_app()

    loader_names = [loader.__class__.__name__ for loader in app.engine.context_loaders]
    assert loader_names == [
        "ProjectRulesLoader",
        "WorkspaceSummaryLoader",
        "ExplicitContextLoader",
    ]
