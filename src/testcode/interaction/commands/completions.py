from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..cli import CLI


Completion = tuple[str, str]


def complete_mode(_cli: CLI, remainder: str) -> list[Completion]:
    if remainder.rstrip() != remainder and remainder.strip():
        return []
    return _complete_values(
        "/mode",
        (
            ("readonly", "Reject write operations"),
            ("confirm", "Ask before write operations"),
            ("auto", "Run allowed tools automatically"),
        ),
        remainder,
    )


def complete_resume(cli: CLI, remainder: str) -> list[Completion]:
    if " " in remainder.strip() or (remainder.rstrip() != remainder and remainder.strip()):
        return []
    sessions = cli.list_sessions() if cli.session_store is not None else []
    values = [
        (
            session.session_id,
            _short(
                f"{session.status} · {session.preview or '(no messages)'} · {session.cwd}"
            ),
        )
        for session in sessions
    ]
    return _complete_values("/resume", values, remainder)


def complete_skill(cli: CLI, remainder: str) -> list[Completion]:
    if " " in remainder.strip() or (remainder.rstrip() != remainder and remainder.strip()):
        return []
    warehouse = getattr(cli.engine, "capability_warehouse", None)
    if warehouse is None:
        return []
    values = [
        (entry.name, _short(entry.description))
        for entry in warehouse.catalog_entries()
        if entry.kind == "toolbox" and entry.source == "skill" and entry.enabled
    ]
    return _complete_values("/skill", values, remainder)


def complete_capabilities(cli: CLI, remainder: str) -> list[Completion]:
    operations = (
        ("list", "List the capability catalog"),
        ("open", "Open one toolbox manifest"),
        ("status", "Show warehouse or toolbox status"),
        ("activate", "Choose a scope and activate one toolbox"),
        ("release", "Release active toolboxes"),
    )
    tokens = remainder.split()
    trailing_space = bool(remainder) and remainder[-1].isspace()
    if not tokens:
        return _complete_values("/capabilities", operations, "")
    if len(tokens) == 1 and not trailing_space:
        return _complete_values("/capabilities", operations, tokens[0])

    operation = tokens[0].lower()
    arguments = tokens[1:]
    fragment = "" if trailing_space else (arguments.pop() if arguments else "")
    if (
        operation == "activate"
        and not arguments
        and fragment in {"--scope=turn", "--scope=run", "--scope=session"}
    ):
        arguments.append(fragment)
        fragment = ""
    prefix = f"/capabilities {operation}"
    warehouse = getattr(cli.engine, "capability_warehouse", None)
    if warehouse is None:
        return []

    if operation in {"open", "status"}:
        if arguments:
            return []
        values = [
            (entry.id, _short(f"{entry.source} · {entry.description}"))
            for entry in warehouse.catalog_entries()
            if entry.kind == "toolbox"
            and (operation == "status" or entry.enabled)
        ]
        return _complete_values(prefix, values, fragment)

    if operation == "activate":
        return _complete_activation(cli, prefix, arguments, fragment)

    if operation == "release":
        active_ids = warehouse.active_toolbox_ids()
        values = [(toolbox_id, "Active toolbox") for toolbox_id in active_ids]
        return _complete_values(prefix, values, fragment, committed=arguments)

    return []


def _complete_activation(
    cli: CLI,
    prefix: str,
    committed: list[str],
    fragment: str,
) -> list[Completion]:
    warehouse = cli.engine.capability_warehouse
    existing_scope = next((item for item in committed if item.startswith("--scope=")), None)
    if existing_scope is None:
        values = (
            ("--scope=turn", "Release after the current turn"),
            ("--scope=run", "Release after the current run"),
            ("--scope=session", "Keep active for this session"),
        )
    else:
        values = [
            (entry.id, _short(entry.description))
            for entry in warehouse.catalog_entries()
            if entry.kind == "toolbox" and entry.enabled
        ]
    return _complete_values(prefix, values, fragment, committed=committed)


def _complete_values(
    prefix: str,
    values,
    fragment: str,
    *,
    committed: list[str] | None = None,
) -> list[Completion]:
    committed = list(committed or [])
    normalized = fragment.strip().casefold()
    base = " ".join((prefix, *committed))
    return [
        (f"{base} {value}", description)
        for value, description in values
        if _matches_fragment(value, normalized)
    ]


def _matches_fragment(value: str, fragment: str) -> bool:
    if not fragment:
        return True
    normalized_value = str(value).casefold()
    if normalized_value.startswith(fragment):
        return True
    value_parts = normalized_value.replace("=", " ").replace(":", " ").split()
    fragment_parts = fragment.replace("=", " ").replace(":", " ").split()
    return all(
        any(value_part.startswith(fragment_part) for value_part in value_parts)
        for fragment_part in fragment_parts
    )


def _short(value: str, limit: int = 72) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"
