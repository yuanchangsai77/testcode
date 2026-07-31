from __future__ import annotations

import os
import json
import shlex
import sys
import tomllib
from collections import deque
from pathlib import Path

from .types import ProjectProfile, ResolvedTestCommand

MARKERS = (
    ("pyproject.toml", "Python"),
    ("package.json", "Node.js"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
)
IGNORED_PROJECT_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".testcode",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
}


class ProjectDetector:
    """Detect supported project profiles from one shared marker catalog."""

    def __init__(
        self,
        *,
        max_nested_depth: int = 3,
        max_scanned_directories: int = 500,
    ) -> None:
        self.max_nested_depth = max(0, int(max_nested_depth))
        self.max_scanned_directories = max(1, int(max_scanned_directories))

    def detect(
        self,
        start: Path,
        *,
        boundary: Path | None = None,
    ) -> list[ProjectProfile]:
        start = start.expanduser().resolve()
        boundary = boundary.expanduser().resolve() if boundary is not None else start
        for root in self._candidate_roots(start, boundary):
            profiles = self._profiles_at(root)
            if profiles:
                return profiles
        return [
            profile
            for root in self._nested_roots(start)
            for profile in self._profiles_at(root)
        ]

    def _candidate_roots(self, start: Path, boundary: Path):
        current = start
        while True:
            yield current
            if current == boundary:
                return
            try:
                current.relative_to(boundary)
            except ValueError:
                return
            parent = current.parent
            if parent == current:
                return
            current = parent

    def _nested_roots(self, start: Path):
        queue = deque([(start, 0)])
        scanned = 0
        while queue and scanned < self.max_scanned_directories:
            current, depth = queue.popleft()
            scanned += 1
            if depth > 0 and any((current / marker).is_file() for marker, _ in MARKERS):
                yield current
                continue
            if depth >= self.max_nested_depth:
                continue
            try:
                children = sorted(
                    (
                        child
                        for child in current.iterdir()
                        if child.is_dir()
                        and not child.is_symlink()
                        and child.name not in IGNORED_PROJECT_DIRS
                    ),
                    key=lambda child: child.name.casefold(),
                )
            except OSError:
                continue
            queue.extend((child, depth + 1) for child in children)

    def _profiles_at(self, root: Path) -> list[ProjectProfile]:
        profiles = []
        for marker, language in MARKERS:
            if not (root / marker).is_file():
                continue
            profiles.append(
                ProjectProfile(
                    language=language,
                    marker=marker,
                    test_commands=self._test_commands(root, language),
                    root=str(root),
                    source_layout=self._source_layout(root, language),
                    virtual_environment=self._virtual_environment(root, language),
                )
            )
        return profiles

    def _test_commands(self, root: Path, language: str) -> list[str]:
        if language == "Python":
            return ["python -m pytest"] if self._has_pytest_evidence(root) else []
        if language == "Node.js":
            return ["npm test"] if self._has_node_test_script(root) else []
        if language == "Rust":
            return ["cargo test"]
        if language == "Go":
            return ["go test ./..."]
        return []

    def _has_pytest_evidence(self, root: Path) -> bool:
        if (root / "tests").is_dir() or any(root.glob("test_*.py")):
            return True
        data = self._read_pyproject(root)
        tool = data.get("tool", {})
        if isinstance(tool, dict) and isinstance(tool.get("pytest"), dict):
            return True
        dependency_groups = [
            data.get("project", {}).get("dependencies", []),
            *(
                data.get("project", {}).get("optional-dependencies", {}).values()
                if isinstance(data.get("project", {}).get("optional-dependencies"), dict)
                else []
            ),
        ]
        return any(
            isinstance(dependency, str)
            and dependency.casefold().split(maxsplit=1)[0].startswith("pytest")
            for group in dependency_groups
            if isinstance(group, list)
            for dependency in group
        )

    def _has_node_test_script(self, root: Path) -> bool:
        try:
            data = json.loads((root / "package.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        scripts = data.get("scripts", {})
        command = scripts.get("test") if isinstance(scripts, dict) else None
        if not isinstance(command, str) or not command.strip():
            return False
        return "no test specified" not in command.casefold()

    def _source_layout(self, root: Path, language: str) -> str | None:
        if language != "Python" or not (root / "src").is_dir():
            return None
        data = self._read_pyproject(root)
        tool = data.get("tool", {})
        if not isinstance(tool, dict):
            return None
        setuptools = tool.get("setuptools", {})
        if isinstance(setuptools, dict):
            package_dir = setuptools.get("package-dir", {})
            if isinstance(package_dir, dict) and "src" in package_dir.values():
                return "src"
            packages = setuptools.get("packages", {})
            find = packages.get("find", {}) if isinstance(packages, dict) else {}
            where = find.get("where", []) if isinstance(find, dict) else []
            if isinstance(where, list) and "src" in where:
                return "src"
        poetry = tool.get("poetry", {})
        packages = poetry.get("packages", []) if isinstance(poetry, dict) else []
        if isinstance(packages, list) and any(
            isinstance(item, dict) and item.get("from") == "src"
            for item in packages
        ):
            return "src"
        return None

    def _read_pyproject(self, root: Path) -> dict:
        try:
            with (root / "pyproject.toml").open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _virtual_environment(self, root: Path, language: str) -> str | None:
        if language != "Python":
            return None
        candidates = (
            root / ".venv" / "bin" / "python",
            root / "venv" / "bin" / "python",
            root / ".venv" / "Scripts" / "python.exe",
            root / "venv" / "Scripts" / "python.exe",
        )
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None


class ProjectCommandResolver:
    def resolve(self, profile: ProjectProfile) -> ResolvedTestCommand:
        if not profile.test_commands:
            raise ValueError(f"project has no reliable test command: {profile.marker}")
        if profile.language == "Python":
            return self._python(profile)
        return ResolvedTestCommand(
            command=profile.test_commands[0],
            project_root=profile.root,
            command_source=f"detected:{profile.marker}",
            environment_source="system",
        )

    def _python(self, profile: ProjectProfile) -> ResolvedTestCommand:
        if profile.virtual_environment:
            interpreter = profile.virtual_environment
            environment_source = "project_virtual_environment"
        else:
            interpreter = sys.executable or "python"
            environment_source = "current_interpreter"

        command = f"{shlex.quote(interpreter)} -m pytest"
        if profile.source_layout:
            command = (
                f"PYTHONPATH={shlex.quote(profile.source_layout)} "
                f"{command}"
            )
        return ResolvedTestCommand(
            command=command,
            project_root=profile.root,
            command_source=f"detected:{profile.marker}",
            environment_source=environment_source,
        )
