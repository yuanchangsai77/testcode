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


def handle_tasks(cli: CLI, args: list[str], **kwargs) -> bool:
    cli.presenter.show_tasks()
    return False


def handle_exit(cli: CLI, args: list[str], **kwargs) -> bool:
    return True
