from __future__ import annotations

import glob
from dataclasses import dataclass
from pathlib import Path

from ..orchestration.ext import ContextLoader
from ..orchestration.session import SessionContext
from ..safety.redaction import is_sensitive_path
from ..tools.shared import looks_binary
from ..types import UserRequest

MAX_CONTEXT_FILES = 20
MAX_CONTEXT_BYTES = 32_000


@dataclass(slots=True)
class ExplicitContextItem:
    source: str
    path: str
    kind: str
    content: str = ""
    truncated: bool = False
    error: str | None = None


class ExplicitContextLoader(ContextLoader):
    """Load user-selected files or directories from request metadata."""

    def __init__(
        self,
        logger=None,
        max_files: int = MAX_CONTEXT_FILES,
        max_bytes: int = MAX_CONTEXT_BYTES,
    ) -> None:
        self.logger = logger
        self.max_files = max(1, int(max_files))
        self.max_bytes = max(1, int(max_bytes))

    def load_context(self, request: UserRequest, session: SessionContext) -> None:
        raw_context = request.metadata.get("context_paths", [])
        if not isinstance(raw_context, list):
            return

        root = Path(request.cwd).expanduser().resolve()
        items = self._load_items(root, [str(item) for item in raw_context if str(item).strip()])
        session.explicit_context = items
        if self.logger is not None and items:
            self.logger.record(
                "context.explicit",
                {
                    "items": [
                        {
                            "source": item.source,
                            "path": item.path,
                            "kind": item.kind,
                            "truncated": item.truncated,
                            "error": item.error,
                        }
                        for item in items
                    ]
                },
            )

    def _load_items(self, root: Path, raw_paths: list[str]) -> list[ExplicitContextItem]:
        items: list[ExplicitContextItem] = []
        for raw_path in raw_paths:
            expanded = self._expand(root, raw_path)
            if not expanded:
                items.append(ExplicitContextItem(source=raw_path, path=raw_path, kind="error", error="path_not_found"))
                continue
            for path in expanded:
                if len([item for item in items if item.kind == "file" and item.error is None]) >= self.max_files:
                    items.append(
                        ExplicitContextItem(
                            source=raw_path,
                            path="",
                            kind="limit",
                            error=f"explicit context file limit reached: {self.max_files}",
                        )
                    )
                    return items
                items.extend(self._item_for_path(root, raw_path, path))
        return items

    def _expand(self, root: Path, raw_path: str) -> list[Path]:
        if glob.has_magic(raw_path):
            pattern = raw_path if Path(raw_path).is_absolute() else str(root / raw_path)
            return sorted(Path(path) for path in glob.glob(pattern, recursive=True))
        return [Path(raw_path)]

    def _item_for_path(self, root: Path, source: str, raw_path: Path) -> list[ExplicitContextItem]:
        resolved = self._resolve_inside_root(root, raw_path)
        if resolved is None:
            return [ExplicitContextItem(source=source, path=str(raw_path), kind="error", error="path_outside_workspace")]
        if not resolved.exists():
            return [ExplicitContextItem(source=source, path=self._display_path(root, resolved), kind="error", error="path_not_found")]
        if resolved.is_dir():
            return [self._directory_item(root, source, resolved)]
        if not resolved.is_file():
            return [ExplicitContextItem(source=source, path=self._display_path(root, resolved), kind="error", error="unsupported_path")]
        if is_sensitive_path(resolved):
            return [
                ExplicitContextItem(
                    source=source,
                    path=self._display_path(root, resolved),
                    kind="file",
                    error="sensitive_file",
                )
            ]
        return [self._file_item(root, source, resolved)]

    def _resolve_inside_root(self, root: Path, raw_path: Path) -> Path | None:
        candidate = raw_path.expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            return None
        return resolved

    def _display_path(self, root: Path, path: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)

    def _directory_item(self, root: Path, source: str, path: Path) -> ExplicitContextItem:
        entries = []
        try:
            children = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError as error:
            return ExplicitContextItem(source=source, path=self._display_path(root, path), kind="directory", error=str(error))
        visible_children = [child for child in children if not is_sensitive_path(child)]
        for child in visible_children[: self.max_files]:
            suffix = "/" if child.is_dir() else ""
            entries.append(f"{child.relative_to(root)}{suffix}")
        truncated = len(visible_children) > len(entries)
        return ExplicitContextItem(
            source=source,
            path=self._display_path(root, path),
            kind="directory",
            content="\n".join(entries),
            truncated=truncated,
        )

    def _file_item(self, root: Path, source: str, path: Path) -> ExplicitContextItem:
        data = path.read_bytes()
        if looks_binary(data[:4096]):
            return ExplicitContextItem(
                source=source,
                path=self._display_path(root, path),
                kind="file",
                error="binary_file",
            )
        truncated = len(data) > self.max_bytes
        content = data[: self.max_bytes].decode("utf-8", errors="replace")
        return ExplicitContextItem(
            source=source,
            path=self._display_path(root, path),
            kind="file",
            content=content,
            truncated=truncated,
        )
