from __future__ import annotations

from dataclasses import dataclass

import pytest

from testcode.app import create_app
from testcode.capabilities.model import (
    ActivatedCapability,
    CapabilityEntry,
    CapabilityManifest,
    ManifestItem,
)
from testcode.capabilities.mcp_source import MCPToolboxSource
from testcode.capabilities.warehouse import CapabilityWarehouse
from testcode.mcp.config import MCPServerConfig
from testcode.model.prompt import ModelPromptBuilder
from testcode.observability.logger import InMemoryLogger
from testcode.orchestration.session import SessionContext
from testcode.tools.base import SimpleTool
from testcode.tools.registry import ToolRegistry
from testcode.types import ToolAction, ToolResult, UserRequest


def _tool(name: str) -> SimpleTool:
    return SimpleTool(
        name=name,
        description=f"Tool {name}",
        arguments={},
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda action, _context: ToolResult(name=action.name, success=True, output="ok"),
    )


@dataclass
class FakeSource:
    tools: tuple[str, ...] = ("lookup",)
    open_calls: int = 0

    def catalog_entries(self):
        return (
            CapabilityEntry(
                id="fake:maps",
                name="maps",
                kind="toolbox",
                source="fake",
                description="Map lookup and routes",
                tags=("map", "route"),
            ),
        )

    def owns_toolbox(self, toolbox_id):
        return toolbox_id == "fake:maps"

    def open_toolbox(self, toolbox_id):
        self.open_calls += 1
        return CapabilityManifest(
            toolbox_id=toolbox_id,
            name="maps",
            source="fake",
            state="ready",
            origin="local",
            items=tuple(
                ManifestItem(
                    id=f"fake:maps:{name}",
                    toolbox_id=toolbox_id,
                    name=name,
                    kind="tool",
                    description=f"Tool {name}",
                )
                for name in self.tools
            ),
        )

    def activate(self, capability_id):
        name = capability_id.rsplit(":", 1)[1]
        return ActivatedCapability(
            id=capability_id,
            toolbox_id="fake:maps",
            kind="tool",
            tool=_tool(name),
        )


def _warehouse(source=None, **kwargs):
    logger = InMemoryLogger()
    registry = ToolRegistry(logger)
    return CapabilityWarehouse([source or FakeSource()], registry, logger=logger, **kwargs), registry


def test_catalog_lists_described_toolboxes_without_opening_them():
    source = FakeSource()
    warehouse, registry = _warehouse(source)

    entries = warehouse.list_entries()

    assert [entry.id for entry in entries] == ["fake:maps"]
    assert entries[0].description == "Map lookup and routes"
    assert source.open_calls == 0
    assert registry.definition_for("lookup") is None
    status = warehouse.status("fake:maps")["entries"][0]
    assert status["lifecycle_state"] == "stored"
    assert status["health_state"] == "unknown"
    assert status["connection_state"] == "not_applicable"


def test_mcp_declared_capabilities_form_the_outer_description():
    source = MCPToolboxSource(
        configs=(
            MCPServerConfig(
                name="amap",
                transport="stdio",
                command="amap",
                capabilities=("地图搜索", "路线规划"),
            ),
        ),
        discovery=object(),
        manager=object(),
    )

    entry = source.catalog_entries()[0]

    assert entry.description == "Provides 地图搜索, 路线规划."


def test_lifecycle_records_open_activate_use_and_release_states():
    warehouse, registry = _warehouse()
    warehouse.open_toolbox("fake:maps")
    warehouse.activate(["fake:maps:lookup"], reason="route lookup")

    assert warehouse.status("fake:maps")["entries"][0]["lifecycle_state"] == "activated"
    assert registry.definition_for("lookup") is not None

    warehouse.mark_used("lookup", success=False, error_code="remote_error")
    active = warehouse.status("fake:maps")["active"][0]
    assert active["state"] == "used"
    assert active["use_count"] == 1
    assert active["last_success"] is False
    assert active["last_error_code"] == "remote_error"

    warehouse.release(["fake:maps:lookup"], reason="step complete")
    status = warehouse.status("fake:maps")
    assert registry.definition_for("lookup") is None
    assert status["released"][0]["state"] == "released"
    assert status["released"][0]["release_reason"] == "step complete"


def test_missing_toolbox_and_leaf_descriptions_receive_fallbacks():
    source = FakeSource()
    source.catalog_entries = lambda: (
        CapabilityEntry("fake:maps", "maps", "toolbox", "fake", ""),
    )
    source.open_toolbox = lambda toolbox_id: CapabilityManifest(
        toolbox_id=toolbox_id,
        name="maps",
        source="fake",
        state="ready",
        items=(ManifestItem("fake:maps:lookup", toolbox_id, "lookup", "tool", ""),),
    )
    warehouse, _ = _warehouse(source)

    assert warehouse.list_entries()[0].description
    assert warehouse.open_toolbox("fake:maps").items[0].description


def test_catalog_pagination_uses_same_sequence_for_disabled_entries():
    class ManySource(FakeSource):
        def catalog_entries(self):
            return tuple(
                CapabilityEntry(
                    f"fake:{index:02}",
                    f"box-{index:02}",
                    "toolbox",
                    "fake",
                    f"Toolbox {index}",
                    enabled=index != 5,
                )
                for index in range(22)
            )

    warehouse, _ = _warehouse(ManySource())

    first_page = warehouse.list_entries(offset=0, max_results=20)
    second_page = warehouse.list_entries(offset=20, max_results=20)

    assert [entry.id for entry in first_page][-1] == "fake:19"
    assert [entry.id for entry in second_page] == ["fake:20", "fake:21"]
    assert first_page[5].enabled is False


def test_activation_is_transactional_when_a_tool_name_conflicts():
    warehouse, registry = _warehouse(FakeSource(tools=("fresh", "existing")))
    assert registry.register(_tool("existing")) is True
    warehouse.open_toolbox("fake:maps")

    with pytest.raises(ValueError, match="name conflict"):
        warehouse.activate(["fake:maps:fresh", "fake:maps:existing"])

    assert registry.definition_for("fresh") is None
    assert warehouse.status("fake:maps")["active"] == []


def test_activation_rejects_duplicate_tool_names_within_one_batch():
    warehouse, registry = _warehouse(FakeSource(tools=("one", "two")))
    warehouse.open_toolbox("fake:maps")
    warehouse.sources[0].activate = lambda capability_id: ActivatedCapability(
        id=capability_id,
        toolbox_id="fake:maps",
        kind="tool",
        tool=_tool("shared"),
    )

    with pytest.raises(ValueError, match="name conflict: shared"):
        warehouse.activate(["fake:maps:one", "fake:maps:two"])

    assert registry.definition_for("shared") is None
    assert warehouse.status("fake:maps")["active"] == []


def test_failed_activation_does_not_update_existing_scope_or_reason():
    warehouse, _ = _warehouse(
        FakeSource(tools=("one", "two")),
        max_active_capabilities=1,
    )
    warehouse.open_toolbox("fake:maps")
    warehouse.activate(["fake:maps:one"], scope="run", reason="initial")

    with pytest.raises(ValueError, match="activation limit exceeded"):
        warehouse.activate(
            ["fake:maps:one", "fake:maps:two"],
            scope="session",
            reason="failed update",
        )

    active = warehouse.status("fake:maps")["active"][0]
    assert active["scope"] == "run"
    assert active["reason"] == "initial"


def test_reactivation_updates_scope_and_reason():
    warehouse, _ = _warehouse()
    warehouse.open_toolbox("fake:maps")
    warehouse.activate(["fake:maps:lookup"], scope="run", reason="one request")

    record = warehouse.activate(
        ["fake:maps:lookup"],
        scope="session",
        reason="continued work",
    )[0]

    assert record.scope == "session"
    assert record.reason == "continued work"
    assert warehouse.persisted_capability_ids() == ["fake:maps:lookup"]


def test_mcp_catalog_target_removes_url_credentials_and_query():
    source = MCPToolboxSource(
        configs=(
            MCPServerConfig(
                name="remote",
                transport="streamable_http",
                url="https://user:password@example.test:8443/mcp?token=secret#fragment",
            ),
        ),
        discovery=object(),
        manager=object(),
    )

    entry = source.catalog_entries()[0]

    assert entry.metadata["target"] == "https://example.test:8443/mcp"


def test_session_capability_ids_restore_into_a_fresh_warehouse():
    first, _ = _warehouse()
    first.open_toolbox("fake:maps")
    first.activate(["fake:maps:lookup"], scope="session")

    restored, restored_registry = _warehouse()
    restored.restore_capabilities(first.persisted_capability_ids())

    assert restored.active_ids({"session"}) == ["fake:maps:lookup"]
    assert restored_registry.definition_for("lookup") is not None


def test_activation_budget_rejects_excess_leaf_tools():
    warehouse, registry = _warehouse(
        FakeSource(tools=("one", "two")),
        max_active_capabilities=1,
    )
    warehouse.open_toolbox("fake:maps")

    with pytest.raises(ValueError, match="activation limit exceeded"):
        warehouse.activate(["fake:maps:one", "fake:maps:two"])

    assert registry.definition_for("one") is None
    assert registry.definition_for("two") is None


def test_only_session_scoped_skills_are_selected_for_persistence(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".testcode" / "skills" / "route-guide"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: route-guide
description: Route planning guidance
---
ROUTE INSTRUCTIONS
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    app = create_app(workspace_root=tmp_path)
    warehouse = app.engine.capability_warehouse
    warehouse.open_toolbox("skill:route-guide")

    warehouse.activate(["skill:route-guide:instructions"], scope="run")
    assert warehouse.persisted_skills() == []
    warehouse.release(reason="change scope")
    warehouse.activate(["skill:route-guide:instructions"], scope="session")
    assert [skill.metadata.name for skill in warehouse.persisted_skills()] == ["route-guide"]


def test_skill_body_enters_prompt_only_after_leaf_activation(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".testcode" / "skills" / "route-guide"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: route-guide
description: Route planning guidance
triggers: ["route"]
version: 1.0.0
---
PRIVATE ROUTE INSTRUCTIONS
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    app = create_app(workspace_root=tmp_path)
    warehouse = app.engine.capability_warehouse
    session = SessionContext(request=UserRequest(prompt="route", cwd=str(tmp_path)))

    warehouse.apply_to_session(session)
    initial_prompt = str(ModelPromptBuilder().build_messages(session)[0]["content"])
    assert "skill:route-guide" not in initial_prompt
    assert "PRIVATE ROUTE INSTRUCTIONS" not in initial_prompt

    warehouse.open_toolbox("skill:route-guide")
    warehouse.activate(["skill:route-guide:instructions"])
    warehouse.apply_to_session(session)
    active_prompt = str(ModelPromptBuilder().build_messages(session)[0]["content"])
    assert "PRIVATE ROUTE INSTRUCTIONS" in active_prompt
