from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PermissionGrant:
    capability: str
    path: str
    scope: str = "run"


class PermissionContext:
    """Execution-scoped permission grants owned by the orchestration layer."""

    valid_scopes = {"run", "session", "user"}

    def __init__(self) -> None:
        self._grants: list[PermissionGrant] = []

    def grant_workspace_path(self, path: str, *, scope: str = "run") -> PermissionGrant:
        if scope not in self.valid_scopes:
            raise ValueError(f"unknown permission scope: {scope}")

        resolved = str(Path(path).expanduser().resolve(strict=False))
        grant = PermissionGrant(capability="workspace_path", path=resolved, scope=scope)
        if grant not in self._grants:
            self._grants.append(grant)
        return grant

    def workspace_roots(self, *, scopes: set[str] | None = None) -> list[str]:
        allowed_scopes = scopes or self.valid_scopes
        return [
            grant.path
            for grant in self._grants
            if grant.capability == "workspace_path" and grant.scope in allowed_scopes
        ]
