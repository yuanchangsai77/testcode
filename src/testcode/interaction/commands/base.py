from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ...types import StoredSession
    from ..cli import CLI


@dataclass(frozen=True)
class SlashCommand:
    name: str
    description: str
    usage: str = ""
    handler: Callable[..., bool | None] | None = None


class SlashCommandRegistry:
    """Decoupled registry for CLI slash commands and autocomplete providers."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, command: SlashCommand) -> None:
        self._commands[command.name.lower()] = command

    def get(self, name: str) -> SlashCommand | None:
        return self._commands.get(name.lower())

    def list_commands(self) -> list[SlashCommand]:
        unique_cmds: dict[str, SlashCommand] = {}
        for cmd in self._commands.values():
            if cmd.name not in unique_cmds:
                unique_cmds[cmd.name] = cmd
        return sorted(unique_cmds.values(), key=lambda c: c.name)

    def get_completions(self, prefix: str) -> list[tuple[str, str]]:
        if not prefix.startswith("/"):
            return []
        prefix_lower = prefix.lower()
        completions: list[tuple[str, str]] = []
        for cmd in self.list_commands():
            if cmd.name.lower().startswith(prefix_lower):
                completions.append((cmd.name, cmd.description))
        return completions

    def execute(
        self,
        cli: CLI,
        prompt: str,
        *,
        session: StoredSession | None = None,
        conversation: list[dict[str, str]] | None = None,
    ) -> bool:
        parts = prompt.strip().split()
        if not parts:
            return False

        raw_cmd = parts[0].lower()
        if raw_cmd in {"?", "？"}:
            raw_cmd = "/help"

        cmd = self.get(raw_cmd)
        if cmd and cmd.handler:
            result = cmd.handler(cli, parts[1:], session=session, conversation=conversation)
            return bool(result)

        if cli.presenter and hasattr(cli.presenter, "show_unknown_command"):
            cli.presenter.show_unknown_command(raw_cmd)
        return False
