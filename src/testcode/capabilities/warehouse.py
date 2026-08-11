from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Iterable

from ..orchestration.session import SessionContext
from ..tools.registry import ToolRegistry
from .model import (
    ActivatedCapability,
    ActivationRecord,
    CapabilityEntry,
    CapabilityManifest,
    ManifestItem,
)
from .source import CapabilitySource


MAX_ACTIVE_CAPABILITIES = 8
MAX_ACTIVE_SCHEMA_CHARS = 40_000
MAX_CATALOG_RESULTS = 20


@dataclass(slots=True)
class CapabilityWarehouse:
    sources: list[CapabilitySource]
    registry: ToolRegistry
    logger: object | None = None
    max_active_capabilities: int = MAX_ACTIVE_CAPABILITIES
    max_active_schema_chars: int = MAX_ACTIVE_SCHEMA_CHARS
    _opened: dict[str, CapabilityManifest] = field(default_factory=dict)
    _active: dict[str, ActivationRecord] = field(default_factory=dict)
    _active_instructions: dict[str, object] = field(default_factory=dict)
    _released: list[dict[str, object]] = field(default_factory=list)

    def catalog_entries(self) -> list[CapabilityEntry]:
        entries: list[CapabilityEntry] = []
        seen: set[str] = set()
        for source in self.sources:
            for raw_entry in source.catalog_entries():
                entry = self._normalize_entry(raw_entry)
                if entry.id in seen:
                    if self.logger is not None:
                        self.logger.record(
                            "capability.catalog.conflict",
                            {"entry_id": entry.id, "source": entry.source},
                        )
                    continue
                seen.add(entry.id)
                entries.append(entry)
        return sorted(entries, key=lambda item: (item.kind, item.name.casefold(), item.id))

    def catalog_overview(self, limit: int = MAX_CATALOG_RESULTS) -> list[dict[str, object]]:
        return [self._entry_payload(entry) for entry in self.catalog_entries()[: max(1, limit)]]

    def list_entries(
        self,
        *,
        offset: int = 0,
        max_results: int = MAX_CATALOG_RESULTS,
    ) -> list[CapabilityEntry]:
        entries = self.catalog_entries()
        bounded_limit = max(1, min(int(max_results), MAX_CATALOG_RESULTS))
        bounded_offset = max(0, int(offset))
        return entries[bounded_offset : bounded_offset + bounded_limit]

    def open_toolbox(self, toolbox_id: str) -> CapabilityManifest:
        source = self._source_for_toolbox(toolbox_id)
        manifest = self._normalize_manifest(source.open_toolbox(toolbox_id))
        self._opened[toolbox_id] = manifest
        if self.logger is not None:
            self.logger.record(
                "capability.toolbox.opened",
                {
                    "toolbox_id": toolbox_id,
                    "source": manifest.source,
                    "state": manifest.state,
                    "origin": manifest.origin,
                    "item_count": len(manifest.items),
                    "error_code": manifest.error_code,
                },
            )
        return manifest

    def activate(
        self,
        capability_ids: Iterable[str],
        *,
        scope: str = "run",
        reason: str = "selected for current task",
    ) -> list[ActivationRecord]:
        if scope not in {"turn", "run", "session"}:
            raise ValueError(f"unsupported activation scope: {scope}")
        ids = list(dict.fromkeys(str(item) for item in capability_ids if str(item)))
        if not ids:
            raise ValueError("at least one capability id is required")

        pending: list[tuple[ActivatedCapability, int]] = []
        existing: list[ActivationRecord] = []
        for capability_id in ids:
            if capability_id in self._active:
                existing.append(self._active[capability_id])
                continue
            toolbox_id = self._toolbox_for_capability(capability_id)
            manifest = self._opened.get(toolbox_id)
            if manifest is None:
                raise ValueError(f"toolbox must be opened before activation: {toolbox_id}")
            if not any(item.id == capability_id for item in manifest.items):
                raise KeyError(f"unknown capability id in opened toolbox: {capability_id}")
            activated = self._source_for_toolbox(toolbox_id).activate(capability_id)
            schema_chars = self._schema_chars(activated)
            pending.append((activated, schema_chars))

        projected_toolboxes = set(self.active_toolbox_ids())
        projected_toolboxes.update(activated.toolbox_id for activated, _ in pending)
        if len(projected_toolboxes) > self.max_active_capabilities:
            raise ValueError(
                f"activation limit exceeded: max {self.max_active_capabilities} toolboxes"
            )
        current_chars = sum(record.schema_chars for record in self._active.values())
        pending_chars = sum(size for _, size in pending)
        if current_chars + pending_chars > self.max_active_schema_chars:
            raise ValueError(
                f"activation schema budget exceeded: max {self.max_active_schema_chars} chars"
            )

        now = time.time()
        pending_tool_names = [
            activated.tool.name
            for activated, _ in pending
            if activated.tool is not None
        ]
        duplicate_pending_names = {
            name for name in pending_tool_names if pending_tool_names.count(name) > 1
        }
        conflicts = sorted(
            duplicate_pending_names
            | {
                name
                for name in pending_tool_names
                if self.registry.definition_for(name) is not None
            }
        )
        if conflicts:
            raise ValueError(f"tool activation name conflict: {', '.join(conflicts)}")

        registered_names: list[str] = []
        for activated, _ in pending:
            if activated.tool is None:
                continue
            if not self.registry.register(activated.tool):
                for name in registered_names:
                    self.registry.unregister(name)
                raise ValueError(f"tool activation name conflict: {activated.tool.name}")
            registered_names.append(activated.tool.name)

        for record in existing:
            record.scope = scope
            record.reason = reason
        for activated, schema_chars in pending:
            tool_name = ""
            instruction_name = ""
            if activated.tool is not None:
                tool_name = activated.tool.name
            if activated.instruction is not None:
                instruction_name = activated.instruction.name
                self._active_instructions[activated.id] = activated.instruction
            record = ActivationRecord(
                capability_id=activated.id,
                toolbox_id=activated.toolbox_id,
                kind=activated.kind,
                scope=scope,
                reason=reason,
                activated_at=now,
                last_used_at=0.0,
                schema_chars=schema_chars,
                tool_name=tool_name,
                instruction_name=instruction_name,
            )
            self._active[activated.id] = record
            if self.logger is not None:
                self.logger.record("capability.activated", asdict(record))
        return [self._active[item] for item in ids if item in self._active]

    def mark_used(self, tool_name: str, *, success: bool, error_code: str | None = None) -> None:
        for record in self._active.values():
            if record.tool_name != tool_name:
                continue
            record.state = "used"
            record.use_count += 1
            record.last_used_at = time.time()
            record.last_success = success
            record.last_error_code = error_code
            if self.logger is not None:
                self.logger.record(
                    "capability.used",
                    {
                        "capability_id": record.capability_id,
                        "success": success,
                        "error_code": error_code,
                        "use_count": record.use_count,
                    },
                )
            return

    def release(
        self,
        capability_ids: Iterable[str] | None = None,
        *,
        reason: str = "explicit release",
    ) -> list[str]:
        ids = list(capability_ids) if capability_ids is not None else list(self._active)
        released: list[str] = []
        for capability_id in ids:
            record = self._active.pop(capability_id, None)
            if record is None:
                continue
            if record.tool_name:
                self.registry.unregister(record.tool_name)
            if record.instruction_name:
                self._active_instructions.pop(capability_id, None)
            released.append(capability_id)
            released_record = asdict(record)
            released_record.update(
                {"state": "released", "released_at": time.time(), "release_reason": reason}
            )
            self._released.append(released_record)
            self._released = self._released[-50:]
            if self.logger is not None:
                self.logger.record(
                    "capability.released",
                    {
                        "capability_id": capability_id,
                        "scope": record.scope,
                        "reason": reason,
                    },
                )
        return released

    def release_scopes(self, scopes: set[str]) -> list[str]:
        return self.release(
            [
                capability_id
                for capability_id, record in self._active.items()
                if record.scope in scopes
            ],
            reason=f"scope ended: {', '.join(sorted(scopes))}",
        )

    def active_ids(self, scopes: set[str] | None = None) -> list[str]:
        return [
            capability_id
            for capability_id, record in self._active.items()
            if scopes is None or record.scope in scopes
        ]

    def active_toolbox_ids(self) -> list[str]:
        return sorted({record.toolbox_id for record in self._active.values()})

    def opened_items(self, toolbox_id: str | None = None) -> list[ManifestItem]:
        return [
            item
            for manifest_id, manifest in sorted(self._opened.items())
            if toolbox_id is None or manifest_id == toolbox_id
            for item in manifest.items
        ]

    def expand_activation_targets(self, target_ids: Iterable[str]) -> list[str]:
        toolbox_ids = {entry.id for entry in self.catalog_entries() if entry.kind == "toolbox"}
        capability_ids: list[str] = []
        for target_id in dict.fromkeys(target_ids):
            if target_id in toolbox_ids:
                manifest = self._opened.get(target_id) or self.open_toolbox(target_id)
                if not manifest.items:
                    raise ValueError(f"toolbox has no activatable capabilities: {target_id}")
                capability_ids.extend(item.id for item in manifest.items)
                continue
            toolbox_id = self._toolbox_for_capability(target_id)
            if toolbox_id not in self._opened:
                self.open_toolbox(toolbox_id)
            capability_ids.append(target_id)
        return list(dict.fromkeys(capability_ids))

    def expand_release_targets(self, target_ids: Iterable[str]) -> list[str]:
        capability_ids: list[str] = []
        for target_id in dict.fromkeys(target_ids):
            grouped_ids = [
                capability_id
                for capability_id, record in self._active.items()
                if record.toolbox_id == target_id
            ]
            capability_ids.extend(grouped_ids or [target_id])
        return list(dict.fromkeys(capability_ids))

    def persisted_instructions(self) -> list[object]:
        ids = {
            capability_id
            for capability_id, record in self._active.items()
            if record.scope == "session" and record.instruction_name
        }
        return [
            instruction
            for capability_id, instruction in self._active_instructions.items()
            if capability_id in ids
        ]

    def persisted_capability_ids(self) -> list[str]:
        return [
            capability_id
            for capability_id, record in self._active.items()
            if record.scope == "session"
        ]

    def restore_capabilities(self, capability_ids: Iterable[str]) -> None:
        for capability_id in capability_ids:
            if capability_id in self._active:
                continue
            try:
                toolbox_id = self._toolbox_for_capability(capability_id)
                if toolbox_id not in self._opened:
                    self.open_toolbox(toolbox_id)
                self.activate(
                    [capability_id],
                    scope="session",
                    reason="restored from session",
                )
            except (KeyError, ValueError):
                continue

    def apply_to_session(self, session: SessionContext) -> None:
        session.active_instructions = list(self._active_instructions.values())

    def status(self, toolbox_id: str | None = None) -> dict[str, object]:
        entries = self.catalog_entries()
        if toolbox_id is not None:
            entries = [entry for entry in entries if entry.id == toolbox_id]
            if not entries:
                raise KeyError(f"unknown toolbox id: {toolbox_id}")
        return {
            "catalog_count": len(self.catalog_entries()),
            "opened": [
                self._manifest_payload(manifest)
                for key, manifest in sorted(self._opened.items())
                if toolbox_id is None or key == toolbox_id
            ],
            "active": [
                asdict(record)
                for record in self._active.values()
                if toolbox_id is None or record.toolbox_id == toolbox_id
            ],
            "released": [
                record
                for record in self._released
                if toolbox_id is None or record.get("toolbox_id") == toolbox_id
            ],
            "entries": [self._entry_payload(entry) for entry in entries],
            "budgets": {
                "max_active_capabilities": self.max_active_capabilities,
                "active_toolboxes": len(self.active_toolbox_ids()),
                "active_capabilities": len(self._active),
                "max_active_schema_chars": self.max_active_schema_chars,
                "active_schema_chars": sum(item.schema_chars for item in self._active.values()),
            },
        }

    def close(self) -> None:
        self.release(reason="warehouse closed")
        self._opened.clear()
        self._released.clear()

    def describe_entry(self, entry: CapabilityEntry) -> dict[str, object]:
        return self._entry_payload(entry)

    def _source_for_toolbox(self, toolbox_id: str) -> CapabilitySource:
        for source in self.sources:
            if source.owns_toolbox(toolbox_id):
                return source
        raise KeyError(f"unknown toolbox id: {toolbox_id}")

    def _toolbox_for_capability(self, capability_id: str) -> str:
        matches = [
            toolbox_id
            for toolbox_id, manifest in self._opened.items()
            if any(item.id == capability_id for item in manifest.items)
        ]
        if not matches:
            parts = capability_id.split(":")
            if len(parts) >= 3:
                return ":".join(parts[:2])
            raise KeyError(f"capability does not identify a toolbox: {capability_id}")
        return matches[0]

    def _schema_chars(self, activated: ActivatedCapability) -> int:
        if activated.tool is not None:
            definition = activated.tool.definition()
            return len(
                json.dumps(
                    {
                        "name": definition.name,
                        "description": definition.description,
                        "input_schema": definition.input_schema,
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
        if activated.instruction is not None:
            return len(activated.instruction.content)
        return 0

    def _entry_payload(self, entry: CapabilityEntry) -> dict[str, object]:
        manifest = self._opened.get(entry.id)
        records = [item for item in self._active.values() if item.toolbox_id == entry.id]
        lifecycle_state = "stored"
        if manifest is not None:
            lifecycle_state = "opened"
        if records:
            lifecycle_state = "used" if any(item.state == "used" for item in records) else "activated"
        return {
            "id": entry.id,
            "name": entry.name,
            "kind": entry.kind,
            "source": entry.source,
            "description": entry.description,
            "tags": list(entry.tags),
            "configured": entry.configured,
            "enabled": entry.enabled,
            "lifecycle_state": lifecycle_state,
            "health_state": self._health_state(entry, manifest),
            "catalog_source": manifest.origin if manifest is not None else "configuration",
            "connection_state": self._connection_state(entry, manifest),
        }

    def _manifest_payload(self, manifest: CapabilityManifest) -> dict[str, object]:
        return {
            "toolbox_id": manifest.toolbox_id,
            "name": manifest.name,
            "source": manifest.source,
            "state": manifest.state,
            "lifecycle_state": "opened",
            "health_state": manifest.state,
            "catalog_source": manifest.origin,
            "connection_state": self._connection_state(None, manifest),
            "origin": manifest.origin,
            "refreshed_at": manifest.refreshed_at,
            "item_count": len(manifest.items),
            "error_code": manifest.error_code,
            "error_message": manifest.error_message,
        }

    def _health_state(
        self,
        entry: CapabilityEntry | None,
        manifest: CapabilityManifest | None,
    ) -> str:
        if entry is not None and not entry.enabled:
            return "disabled"
        if manifest is None:
            return "unknown"
        records = [
            record for record in self._active.values() if record.toolbox_id == manifest.toolbox_id
        ]
        failures = [record for record in records if record.last_success is False]
        if any(
            (record.last_error_code or "").startswith("mcp_transport_")
            or record.last_error_code in {"mcp_protocol_error", "mcp_server_unavailable"}
            for record in failures
        ):
            return "unavailable"
        if failures and manifest.state == "ready":
            return "degraded"
        return manifest.state

    def _connection_state(
        self,
        entry: CapabilityEntry | None,
        manifest: CapabilityManifest | None,
    ) -> str:
        source = manifest.source if manifest is not None else (entry.source if entry else "")
        if source != "mcp":
            return "not_applicable"
        if manifest is None:
            return "not_connected"
        if manifest.state == "disabled":
            return "not_connected"
        if manifest.origin == "cache":
            return "not_connected"
        if manifest.state == "unavailable":
            return "failed"
        if manifest.origin == "stale":
            return "disconnected"
        if any(
            record.toolbox_id == manifest.toolbox_id
            and record.last_success is False
            and (
                (record.last_error_code or "").startswith("mcp_transport_")
                or record.last_error_code in {"mcp_protocol_error", "mcp_server_unavailable"}
            )
            for record in self._active.values()
        ):
            return "disconnected"
        return "connected"

    def _normalize_entry(self, entry: CapabilityEntry) -> CapabilityEntry:
        description = entry.description.strip() or (
            f"{entry.kind.replace('_', ' ').title()} '{entry.name}' provided by {entry.source}."
        )
        return replace(entry, description=description)

    def _normalize_manifest(self, manifest: CapabilityManifest) -> CapabilityManifest:
        items = tuple(
            replace(
                item,
                description=item.description.strip()
                or f"{item.kind.replace('_', ' ').title()} '{item.name}'.",
            )
            for item in manifest.items
        )
        return replace(manifest, items=items)
