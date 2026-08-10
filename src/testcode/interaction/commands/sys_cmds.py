from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..cli import CLI


def handle_help(cli: CLI, args: list[str], **kwargs) -> bool:
    cli.presenter.show_help(cli.command_registry)
    return False


def handle_clear(cli: CLI, args: list[str], **kwargs) -> bool:
    cli.presenter.clear_screen()
    return False


def handle_status(cli: CLI, args: list[str], session=None, **kwargs) -> bool:
    cli.presenter.show_status(session=session, engine=cli.engine)
    return False


def handle_mode(cli: CLI, args: list[str], **kwargs) -> bool:
    mode_arg = args[0].lower() if args else None
    cli.presenter.show_or_change_mode(cli.engine, mode_arg)
    return False


def handle_skills(cli: CLI, args: list[str], **kwargs) -> bool:
    cli.presenter.show_skills(cli.engine)
    return False


def handle_capabilities(cli: CLI, args: list[str], session=None, **kwargs) -> bool:
    warehouse = getattr(cli.engine, "capability_warehouse", None)
    if warehouse is None:
        cli.presenter.show_capabilities("Capability Warehouse", {"error": "warehouse unavailable"})
        return False
    _restore_session_capabilities(cli, session, warehouse)
    operation = args[0].lower() if args else "list"
    values = args[1:]
    try:
        if operation == "list":
            payload = {"entries": [warehouse.describe_entry(item) for item in warehouse.catalog_entries()]}
        elif operation == "open" and len(values) == 1:
            manifest = warehouse.open_toolbox(values[0])
            payload = {
                "toolbox_id": manifest.toolbox_id,
                "state": manifest.state,
                "items": [
                    {"id": item.id, "name": item.name, "risk": item.risk_level, "description": item.description}
                    for item in manifest.items
                ],
            }
        elif operation == "status" and len(values) <= 1:
            payload = warehouse.status(values[0] if values else None)
        elif operation == "activate" and values:
            scope = "session"
            capability_ids = []
            for value in values:
                if value.startswith("--scope="):
                    scope = value.split("=", 1)[1]
                else:
                    capability_ids.append(value)
            records = warehouse.activate(capability_ids, scope=scope, reason="activated by user command")
            payload = {"activated": [record.capability_id for record in records], "scope": scope}
            _sync_session_capabilities(cli, session, warehouse)
        elif operation == "release":
            payload = {"released": warehouse.release(values or None, reason="released by user command")}
            _sync_session_capabilities(cli, session, warehouse)
        else:
            payload = {"error": "usage: /capabilities [list|open <toolbox>|status [toolbox]|activate <id...> [--scope=turn|run|session]|release [id...]]"}
    except (KeyError, ValueError) as exc:
        payload = {"error": str(exc)}
    cli.presenter.show_capabilities("Capability Warehouse", payload)
    return False


def handle_skill(cli: CLI, args: list[str], session=None, **kwargs) -> bool:
    if len(args) != 1:
        cli.presenter.show_capabilities("Skill Activation", {"error": "usage: /skill <name>"})
        return False
    warehouse = getattr(cli.engine, "capability_warehouse", None)
    if warehouse is None:
        cli.presenter.show_capabilities("Skill Activation", {"error": "warehouse unavailable"})
        return False
    _restore_session_capabilities(cli, session, warehouse)
    toolbox_id = f"skill:{args[0]}"
    try:
        manifest = warehouse.open_toolbox(toolbox_id)
        capability_ids = [item.id for item in manifest.items]
        records = warehouse.activate(capability_ids, scope="session", reason="activated by /skill")
        _sync_session_capabilities(cli, session, warehouse)
        payload = {"activated": [record.capability_id for record in records], "scope": "session"}
    except (KeyError, ValueError) as exc:
        payload = {"error": str(exc)}
    cli.presenter.show_capabilities("Skill Activation", payload)
    return False


def _sync_session_capabilities(cli, session, warehouse) -> None:
    if session is not None:
        session.active_capability_ids = warehouse.persisted_capability_ids()
        if cli.session_store is not None:
            cli.session_store.save(session)


def _restore_session_capabilities(cli, session, warehouse) -> None:
    if session is not None:
        prepare = getattr(cli, "prepare_session_runtime", None)
        if callable(prepare):
            prepare(session)
        else:
            warehouse.restore_capabilities(
                getattr(session, "active_capability_ids", [])
            )


def handle_tasks(cli: CLI, args: list[str], **kwargs) -> bool:
    cli.presenter.show_tasks()
    return False


def handle_exit(cli: CLI, args: list[str], **kwargs) -> bool:
    return True
