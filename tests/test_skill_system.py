from __future__ import annotations

import re
from pathlib import Path
from testcode.skills import Skill, SkillMetadata, SkillRegistry, SkillContextLoader, parse_frontmatter

from testcode.orchestration.session import SessionContext
from testcode.orchestration.engine import ExecutionEngine
from testcode.orchestration.ext import ContextLoader
from testcode.types import UserRequest, ToolDefinition, ToolAction, ToolResult, ModelReply
from testcode.model.prompt import ModelPromptBuilder
from testcode.observability.logger import InMemoryLogger


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


def test_registry_trigger_matching_and_boundaries(tmp_path):
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

    # Exact boundary matching: "test" matches
    matches = registry.match_skills("we should test this")
    assert len(matches) == 1
    assert matches[0].metadata.name == "test-skill"
    assert matches[0].matched_trigger == "test"

    # Substring matching: "greatest" contains "test" but should NOT trigger
    matches_substring = registry.match_skills("this is the greatest thing")
    assert len(matches_substring) == 0

    # Case insensitivity: "RUN TESTS" matches "run tests"
    matches_case = registry.match_skills("RUN TESTS NOW")
    assert len(matches_case) == 1
    assert matches_case[0].metadata.name == "test-skill"
    assert matches_case[0].matched_trigger == "run tests"

    # Explicit command match: "/skill test-skill"
    matches_explicit = registry.match_skills("/skill test-skill")
    assert len(matches_explicit) == 1
    assert matches_explicit[0].metadata.name == "test-skill"
    assert matches_explicit[0].matched_trigger == "/skill"


def test_skill_loader_and_persistence(tmp_path):
    # Setup registry with git-helper
    builtins = tmp_path / "builtins"
    builtins.mkdir()

    skill_git = builtins / "git-helper"
    skill_git.mkdir()
    (skill_git / "SKILL.md").write_text("""---
name: git-helper
description: Git guidelines
triggers: ["git commit"]
version: 1.0.0
---
Git instructions""", encoding="utf-8")

    registry = SkillRegistry(builtins_dir=builtins, global_dir=None, project_dir=None)
    registry.scan_metadata()

    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    loader = SkillContextLoader(registry=registry, logger=logger)

    # Request 1: triggers git-helper
    request = UserRequest(prompt="let's git commit now", cwd=str(tmp_path), metadata={})
    session = SessionContext(request=request)

    # Initialize run log
    logger.start_run(request, registered_skills=["git-helper"])

    loader.load_context(request, session)

    assert len(session.active_skills) == 1
    assert session.active_skills[0].metadata.name == "git-helper"

    # Verify prompt formatting
    builder = ModelPromptBuilder()
    msgs = builder.build_messages(session)
    system_prompt = msgs[0]["content"]
    assert "### Active Skill Guidelines:" in system_prompt
    assert "[Skill: git-helper]" in system_prompt
    assert "Git instructions" in system_prompt

    # Verify skills.matched event was recorded in logger
    matched_events = [e for e in logger.events if e.name == "skills.matched"]
    assert len(matched_events) == 1
    payload = matched_events[0].payload
    assert payload["matched_skills"][0]["name"] == "git-helper"
    assert payload["matched_skills"][0]["matched_trigger"] == "git commit"

    # Turn 2: prompt does not match git-helper trigger, but active_skills is persisted via request.metadata
    request2 = UserRequest(
        prompt="and now push it",
        cwd=str(tmp_path),
        metadata={"active_skills": ["git-helper"]}
    )
    session2 = SessionContext(request=request2)
    loader.load_context(request2, session2)

    # Skill should still be active due to persistence!
    assert len(session2.active_skills) == 1
    assert session2.active_skills[0].metadata.name == "git-helper"
