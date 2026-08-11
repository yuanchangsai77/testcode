from __future__ import annotations

from .base import SlashCommand, SlashCommandRegistry
from .completions import complete_capabilities, complete_mode, complete_resume, complete_skill
from .session_cmds import handle_compact, handle_reset, handle_resume
from .sys_cmds import (
    handle_clear,
    handle_capabilities,
    handle_exit,
    handle_help,
    handle_mode,
    handle_skills,
    handle_skill,
    handle_status,
    handle_tasks,
)

_default_registry: SlashCommandRegistry | None = None


def default_slash_command_registry() -> SlashCommandRegistry:
    global _default_registry
    if _default_registry is not None:
        return _default_registry

    registry = SlashCommandRegistry()
    registry.register(SlashCommand(name="/help", description="Show help message and shortcuts", handler=handle_help))
    registry.register(SlashCommand(name="/clear", description="Clear terminal screen", handler=handle_clear))
    registry.register(SlashCommand(name="/status", description="Show session and system status", handler=handle_status))
    registry.register(SlashCommand(name="/mode", description="Show or change safety mode", usage="/mode [mode]", handler=handle_mode, argument_completer=complete_mode))
    registry.register(SlashCommand(name="/skills", description="List Skill toolboxes in the capability warehouse", handler=handle_skills))
    registry.register(SlashCommand(name="/skill", description="List or activate one Skill toolbox", usage="/skill [name]", handler=handle_skill, argument_completer=complete_skill))
    registry.register(SlashCommand(name="/capabilities", description="List or manage capability warehouse entries", usage="/capabilities [operation]", handler=handle_capabilities, argument_completer=complete_capabilities))
    registry.register(SlashCommand(name="/tasks", description="List active background tasks", handler=handle_tasks))
    registry.register(SlashCommand(name="/resume", description="List or resume saved sessions", usage="/resume [session_id]", handler=handle_resume, argument_completer=complete_resume))
    registry.register(SlashCommand(name="/reset", description="Reset conversation context memory", handler=handle_reset))
    registry.register(SlashCommand(name="/new", description="Start a fresh conversation context", handler=handle_reset))
    registry.register(SlashCommand(name="/compact", description="Compact and summarize conversation context", handler=handle_compact))
    registry.register(SlashCommand(name="/exit", description="Exit workbench session", handler=handle_exit))
    registry.register(SlashCommand(name="/quit", description="Exit workbench session", handler=handle_exit))

    _default_registry = registry
    return registry


__all__ = [
    "SlashCommand",
    "SlashCommandRegistry",
    "default_slash_command_registry",
]
