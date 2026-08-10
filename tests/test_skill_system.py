from __future__ import annotations

from pathlib import Path
import tomllib
from testcode.skills import Skill, SkillMetadata, SkillRegistry, parse_frontmatter

from testcode.capabilities import CapabilityWarehouse, LocalToolboxSource, skill_toolbox_specs
from testcode.orchestration.session import SessionContext
from testcode.orchestration.engine import ExecutionEngine
from testcode.orchestration.ext import ContextLoader
from testcode.types import UserRequest, ToolDefinition, ToolAction, ToolResult, ModelReply
from testcode.model.prompt import ModelPromptBuilder
from testcode.observability.logger import InMemoryLogger
from testcode.tools.registry import ToolRegistry


def test_parse_frontmatter():
    # 1. Test block list format
    yaml_block = """---
name: python-unittest-helper
description: Guidelines for Python unit tests.
triggers:
  - "run tests"
  - "python test"
  - "unittest"
version: 1.0.0
---
body contents"""
    meta = parse_frontmatter(yaml_block)
    assert meta["name"] == "python-unittest-helper"
    assert meta["description"] == "Guidelines for Python unit tests."
    assert meta["version"] == "1.0.0"
    assert meta["triggers"] == ["run tests", "python test", "unittest"]

    # 2. Test inline list format
    yaml_inline = """---
name: git-helper
triggers: ["git commit", "git push"]
version: 2.0
---"""
    meta2 = parse_frontmatter(yaml_inline)
    assert meta2["name"] == "git-helper"
    assert meta2["triggers"] == ["git commit", "git push"]
    assert meta2["version"] == "2.0"


def test_builtin_skill_metadata_and_guidance_are_actionable():
    builtins = Path(__file__).parents[1] / "src" / "testcode" / "skills" / "builtins"
    registry = SkillRegistry(builtins_dir=builtins, global_dir=None, project_dir=None)
    registry.scan_metadata()

    pytest_skill = registry.get_skill("pytest-helper")
    git_skill = registry.get_skill("git-helper")

    assert pytest_skill is not None
    assert pytest_skill.metadata.description == "A focused pytest workflow with on-demand project-aware test execution."
    assert "Prefer `run_tests`" in pytest_skill.content
    assert git_skill is not None
    assert "Use `git_status`" in git_skill.content
    assert "read-only Git tools" in git_skill.content


def test_builtin_skill_files_are_declared_as_package_data():
    root = Path(__file__).parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert "builtins/*/SKILL.md" in config["tool"]["setuptools"]["package-data"]["testcode.skills"]


def test_registry_scanning_and_override(tmp_path):
    # Setup builtins, global, and project dirs
    builtins = tmp_path / "builtins"
    globals_dir = tmp_path / "global"
    project = tmp_path / "project"

    for d in (builtins, globals_dir, project):
        d.mkdir()

    # 1. Create a builtin skill
    skill_a = builtins / "skill-a"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text("""---
name: skill-a
description: Builtin version of skill-a
triggers: ["trigger-a"]
version: 1.0.0
---
Builtin skill-a body""", encoding="utf-8")

    # 2. Create a global skill that overrides skill-a
    skill_a_global = globals_dir / "skill-a"
    skill_a_global.mkdir()
    (skill_a_global / "SKILL.md").write_text("""---
name: skill-a
description: Global version of skill-a
triggers: ["trigger-a-global"]
version: 1.1.0
---
Global skill-a body""", encoding="utf-8")

    # 3. Create a project skill that overrides skill-a global
    skill_a_project = project / "skill-a"
    skill_a_project.mkdir()
    (skill_a_project / "SKILL.md").write_text("""---
name: skill-a
description: Project version of skill-a
triggers: ["trigger-a-proj"]
version: 1.2.0
---
Project skill-a body""", encoding="utf-8")

    # 4. Create a unique project skill
    skill_b = project / "skill-b"
    skill_b.mkdir()
    (skill_b / "SKILL.md").write_text("""---
name: skill-b
description: Unique project skill
triggers: ["trigger-b"]
version: 2.0.0
---
Project skill-b body""", encoding="utf-8")

    registry = SkillRegistry(builtins_dir=builtins, global_dir=globals_dir, project_dir=project)
    registry.scan_metadata()

    # Verify override priority (project overrides global overrides builtins)
    assert len(registry._skills) == 2
    assert "skill-a" in registry._skills
    assert "skill-b" in registry._skills

    # skill-a must be project version (v1.2.0)
    meta_a = registry._skills["skill-a"]
    assert meta_a.version == "1.2.0"
    assert meta_a.description == "Project version of skill-a"
    assert meta_a.triggers == ["trigger-a-proj"]

    # Verify get_skill fully loads content
    loaded_a = registry.get_skill("skill-a")
    assert loaded_a is not None
    assert loaded_a.content == "Project skill-a body"
    assert loaded_a.metadata.version == "1.2.0"


def test_skill_activation_has_one_warehouse_owned_path(tmp_path):
    builtins = tmp_path / "builtins"
    builtins.mkdir()

    skill_test = builtins / "test-skill"
    skill_test.mkdir()
    (skill_test / "SKILL.md").write_text("""---
name: test-skill
description: Skill for testing
triggers: ["test", "run tests"]
version: 1.0.0
---
body""", encoding="utf-8")

    registry = SkillRegistry(builtins_dir=builtins, global_dir=None, project_dir=None)
    registry.scan_metadata()

    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    warehouse = CapabilityWarehouse(
        sources=[LocalToolboxSource(skill_toolbox_specs(registry))],
        registry=ToolRegistry(logger),
        logger=logger,
    )
    request = UserRequest(prompt="use the test skill", cwd=str(tmp_path), metadata={})
    session = SessionContext(request=request)
    manifest = warehouse.open_toolbox("skill:test-skill")
    warehouse.activate([manifest.items[0].id], scope="session", reason="explicit selection")
    warehouse.apply_to_session(session)

    assert len(session.active_instructions) == 1
    assert session.active_instructions[0].name == "test-skill"

    # Verify prompt formatting
    builder = ModelPromptBuilder()
    msgs = builder.build_messages(session)
    system_prompt = msgs[0]["content"]
    assert "### Active Workflow Instructions:" in system_prompt
    assert "[Workflow: test-skill]" in system_prompt
    assert "body" in system_prompt
    assert warehouse.persisted_capability_ids() == ["skill:test-skill:instructions"]
    assert not any(event.name == "skills.matched" for event in logger.events)
