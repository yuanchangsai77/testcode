from __future__ import annotations

from pathlib import Path
from typing import Any
from .model import Skill, SkillMetadata
from .parser import parse_frontmatter, read_frontmatter_only, load_skill_content


class SkillRegistry:
    def __init__(
        self,
        builtins_dir: str | Path | None,
        global_dir: str | Path | None,
        project_dir: str | Path | None,
    ) -> None:
        self.dirs = []
        for d in (builtins_dir, global_dir, project_dir):
            if d is not None:
                self.dirs.append(Path(d).expanduser())
        self._skills: dict[str, SkillMetadata] = {}

    def scan_metadata(self, project_dir: str | Path | None = None) -> None:
        """Lightweight scan of skill metadata. Does not load full file contents."""
        self._skills.clear()
        dirs_to_scan = list(self.dirs)
        if project_dir is not None:
            # We want to replace or set the third directory (project_dir)
            p_dir = Path(project_dir).expanduser()
            if len(dirs_to_scan) >= 3:
                dirs_to_scan[2] = p_dir
            elif len(dirs_to_scan) == 2:
                dirs_to_scan.append(p_dir)
            elif len(dirs_to_scan) == 1:
                # We also need a dummy/placeholder for global_dir
                dirs_to_scan.append(Path("~/.testcode/skills").expanduser())
                dirs_to_scan.append(p_dir)
            else:
                # If it was empty
                dirs_to_scan.append(Path("").resolve()) # builtins dummy
                dirs_to_scan.append(Path("~/.testcode/skills").expanduser())
                dirs_to_scan.append(p_dir)

        for d in dirs_to_scan:
            # Note: We resolve strict=False because directories may not exist yet
            resolved_dir = d.resolve(strict=False)
            if not resolved_dir.exists() or not resolved_dir.is_dir():
                continue
            for child in resolved_dir.iterdir():
                if child.is_dir():
                    skill_md_path = child / "SKILL.md"
                    if skill_md_path.exists() and skill_md_path.is_file():
                        fm_text = read_frontmatter_only(skill_md_path)
                        metadata_dict = parse_frontmatter(fm_text)

                        name = metadata_dict.get("name")
                        if not name:
                            name = child.name

                        description = metadata_dict.get("description", "")
                        version = str(metadata_dict.get("version", "0.1.0"))

                        triggers_raw = metadata_dict.get("triggers", [])
                        if isinstance(triggers_raw, list):
                            triggers = [str(t) for t in triggers_raw]
                        elif isinstance(triggers_raw, str):
                            triggers = [triggers_raw]
                        else:
                            triggers = []

                        metadata = SkillMetadata(
                            name=str(name),
                            description=str(description),
                            triggers=triggers,
                            version=version,
                            path=str(skill_md_path.resolve()),
                        )
                        self._skills[metadata.name] = metadata

    def get_skill(self, name: str) -> Skill | None:
        """Retrieve and fully load a skill by name."""
        metadata = self._skills.get(name)
        if not metadata:
            return None

        _, body = load_skill_content(Path(metadata.path))
        return Skill(metadata=metadata, content=body)

    def metadata_items(self) -> tuple[SkillMetadata, ...]:
        return tuple(self._skills.values())
