from __future__ import annotations

from pathlib import Path

from testcode.observability.logger import InMemoryLogger
from testcode.project import ProjectCommandResolver, ProjectDetector
from testcode.tools.builtin_provider import build_builtin_registry
from testcode.types import ToolAction


def test_project_detector_returns_shared_profiles(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        "name='demo'\n\n"
        "[tool.setuptools]\n"
        "package-dir = {\"\" = \"src\"}\n",
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()

    profiles = ProjectDetector().detect(tmp_path)

    assert len(profiles) == 1
    assert profiles[0].language == "Python"
    assert profiles[0].marker == "pyproject.toml"
    assert profiles[0].test_commands == ["python -m pytest"]
    assert profiles[0].source_layout == "src"


def test_project_detector_walks_to_nearest_project_root(tmp_path):
    (tmp_path / "go.mod").write_text("module example.test/demo\n", encoding="utf-8")
    nested = tmp_path / "src" / "nested"
    nested.mkdir(parents=True)

    profiles = ProjectDetector().detect(nested, boundary=tmp_path)

    assert len(profiles) == 1
    assert profiles[0].root == str(tmp_path)


def test_python_command_resolver_prefers_project_virtual_environment(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    interpreter.chmod(0o755)
    profile = ProjectDetector().detect(tmp_path)[0]

    resolved = ProjectCommandResolver().resolve(profile)

    assert str(interpreter) in resolved.command
    assert resolved.environment_source == "project_virtual_environment"


def test_run_tests_detects_python_command_and_src_layout(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        "name='demo'\n\n"
        "[tool.setuptools]\n"
        "package-dir = {\"\" = \"src\"}\n",
        encoding="utf-8",
    )
    source = tmp_path / "src"
    source.mkdir()
    (source / "demo.py").write_text("VALUE = 42\n", encoding="utf-8")
    (tmp_path / "test_demo.py").write_text(
        "from demo import VALUE\n\n"
        "def test_value():\n"
        "    assert VALUE == 42\n",
        encoding="utf-8",
    )
    registry = build_builtin_registry(InMemoryLogger())

    result = registry.execute(
        ToolAction(name="run_tests"),
        cwd=str(tmp_path),
    )

    assert result.success is True
    assert result.metadata["command_source"] == "detected:pyproject.toml"
    assert result.metadata["project_root"] == str(tmp_path)
    assert "PYTHONPATH=src" in result.metadata["command"]
    assert result.metadata["timeout_seconds"] == 120


def test_run_tests_requires_explicit_command_for_ambiguous_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node test.js"}}\n', encoding="utf-8")
    registry = build_builtin_registry(InMemoryLogger())

    result = registry.execute(
        ToolAction(name="run_tests"),
        cwd=str(tmp_path),
    )

    assert result.error_code == "test_command_ambiguous"
    assert {item["marker"] for item in result.metadata["candidates"]} == {
        "pyproject.toml",
        "package.json",
    }
    assert not registry.state_for("shell_session")


def test_project_detector_discovers_nested_workspace_projects(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "pyproject.toml").write_text(
        "[project]\nname='backend'\n",
        encoding="utf-8",
    )
    (backend / "tests").mkdir()
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        '{"scripts":{"test":"node test.js"}}\n',
        encoding="utf-8",
    )

    profiles = ProjectDetector().detect(tmp_path)

    assert {profile.root for profile in profiles} == {
        str(backend),
        str(frontend),
    }


def test_run_tests_reports_nested_project_roots_when_ambiguous(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "go.mod").write_text(
        "module example.test/backend\n",
        encoding="utf-8",
    )
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        '{"scripts":{"test":"node test.js"}}\n',
        encoding="utf-8",
    )

    result = build_builtin_registry(InMemoryLogger()).execute(
        ToolAction(name="run_tests"),
        cwd=str(tmp_path),
    )

    assert result.error_code == "test_command_ambiguous"
    assert {item["root"] for item in result.metadata["candidates"]} == {
        str(backend),
        str(frontend),
    }


def test_run_tests_reports_when_no_project_is_detected(tmp_path):
    result = build_builtin_registry(InMemoryLogger()).execute(
        ToolAction(name="run_tests"),
        cwd=str(tmp_path),
    )

    assert result.error_code == "test_command_not_detected"


def test_run_tests_does_not_guess_from_markers_without_test_evidence(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='demo'\n",
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        '{"scripts":{"build":"node build.js"}}\n',
        encoding="utf-8",
    )

    result = build_builtin_registry(InMemoryLogger()).execute(
        ToolAction(name="run_tests"),
        cwd=str(tmp_path),
    )

    assert result.error_code == "test_command_not_detected"
    assert result.metadata["detected_markers"] == [
        "pyproject.toml",
        "package.json",
    ]


def test_run_tests_preserves_explicit_timeout(tmp_path):
    result = build_builtin_registry(InMemoryLogger()).execute(
        ToolAction(
            name="run_tests",
            arguments={"command": "printf ok", "timeout": 7},
        ),
        cwd=str(tmp_path),
    )

    assert result.success is True
    assert result.metadata["timeout_seconds"] == 7
