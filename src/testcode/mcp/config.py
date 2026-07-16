from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
VALID_MCP_TRANSPORTS = {"stdio", "streamable_http", "sse"}
VALID_RISK_LEVELS = {"read", "write", "execute", "test", "network", "destructive", "confirm"}


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    name: str
    transport: str
    enabled: bool = True
    description: str = ""
    capabilities: tuple[str, ...] = ()
    tool_name_prefix: str = ""
    risk_overrides: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    read_timeout: float = 300.0
    headers: dict[str, str] = field(default_factory=dict)
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""

    @property
    def stable_prefix(self) -> str:
        return self.tool_name_prefix or self.name


def load_mcp_server_configs(cwd: str | Path | None = None) -> tuple[MCPServerConfig, ...]:
    project_root = Path(cwd or os.getcwd())
    global_path = Path.home() / ".testcode" / "config.toml"
    project_path = project_root / ".testcode" / "config.toml"

    merged: dict[str, MCPServerConfig] = {}
    for path in (global_path, project_path):
        if not path.exists():
            continue
        for server in _parse_mcp_servers(path):
            merged[server.name] = server
    return tuple(_apply_env_overrides(server) for server in merged.values())


def _parse_mcp_servers(path: Path) -> tuple[MCPServerConfig, ...]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_mcp = data.get("mcp", {})
    if not isinstance(raw_mcp, dict):
        raise ValueError(f"MCP config section must be a table in '{path}'")
    raw_servers = raw_mcp.get("servers", [])
    if not isinstance(raw_servers, list):
        raise ValueError(f"MCP servers must be an array of tables in '{path}'")
    parsed: list[MCPServerConfig] = []
    seen_names: set[str] = set()
    for raw in raw_servers:
        if not isinstance(raw, dict):
            raise ValueError(f"MCP server entry must be a table in '{path}'")
        server = _build_server_config(raw)
        if server is not None:
            if server.name in seen_names:
                raise ValueError(
                    f"duplicate MCP server name '{server.name}' in config file '{path}'"
                )
            seen_names.add(server.name)
            parsed.append(server)
    return tuple(parsed)


def _build_server_config(raw: dict[str, Any]) -> MCPServerConfig | None:
    name = _string_field(raw.get("name", ""), "name").strip()
    transport = _string_field(raw.get("transport", ""), "transport", server_name=name).strip()
    if not name or not transport:
        return None
    if transport not in VALID_MCP_TRANSPORTS:
        raise ValueError(f"unsupported MCP transport '{transport}' for server '{name}'")

    headers = _string_dict(raw.get("headers"), "headers", name)
    env = _string_dict(raw.get("env"), "env", name)
    args = _string_list(raw.get("args"), "args", name)
    capabilities = _string_list(raw.get("capabilities"), "capabilities", name)
    risk_overrides = _string_dict(raw.get("risk_overrides"), "risk_overrides", name)
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"MCP server '{name}' field 'enabled' must be a boolean")
    invalid_risks = sorted({value for value in risk_overrides.values() if value not in VALID_RISK_LEVELS})
    if invalid_risks:
        raise ValueError(f"invalid MCP risk override(s) for server '{name}': {', '.join(invalid_risks)}")
    config = MCPServerConfig(
        name=name,
        transport=transport,
        enabled=enabled,
        description=_string_field(raw.get("description", ""), "description", server_name=name).strip(),
        capabilities=capabilities,
        tool_name_prefix=_string_field(raw.get("tool_name_prefix", ""), "tool_name_prefix", server_name=name).strip(),
        risk_overrides=risk_overrides,
        timeout=_positive_float(raw.get("timeout"), 30.0),
        read_timeout=_positive_float(raw.get("read_timeout"), 300.0),
        headers={key: _expand_env(value) for key, value in headers.items()},
        command=_expand_env(_string_field(raw.get("command", ""), "command", server_name=name).strip()),
        args=tuple(_expand_env(arg) for arg in args),
        env={key: _expand_env(value) for key, value in env.items()},
        url=_expand_env(_string_field(raw.get("url", ""), "url", server_name=name).strip()),
    )
    if transport == "stdio" and not config.command:
        raise ValueError(f"stdio MCP server '{name}' requires command")
    if transport in {"streamable_http", "sse"} and not config.url:
        raise ValueError(f"{transport} MCP server '{name}' requires url")
    return config


def _string_field(value: Any, field_name: str, *, server_name: str = "") -> str:
    if not isinstance(value, str):
        context = f"MCP server '{server_name}'" if server_name else "MCP server"
        raise ValueError(f"{context} field '{field_name}' must be a string")
    return value


def _string_list(value: Any, field_name: str, server_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"MCP server '{server_name}' field '{field_name}' must be an array of strings")
    return tuple(value)


def _string_dict(value: Any, field_name: str, server_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError(f"MCP server '{server_name}' field '{field_name}' must be a string table")
    return dict(value)


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _expand_env(value: str) -> str:
    return _ENV_PATTERN.sub(lambda match: os.getenv(match.group(1), ""), value)


def _apply_env_overrides(server: MCPServerConfig) -> MCPServerConfig:
    normalized_name = re.sub(r"[^A-Z0-9]", "_", server.name.upper())
    prefix = f"TESTCODE_MCP_{normalized_name}_"
    values: dict[str, Any] = {}
    for field_name in ("transport", "tool_name_prefix", "command", "url"):
        raw = os.getenv(prefix + field_name.upper())
        if raw is not None:
            values[field_name] = _expand_env(raw.strip())
    for field_name in ("timeout", "read_timeout"):
        raw = os.getenv(prefix + field_name.upper())
        if raw is not None:
            values[field_name] = _positive_float(raw, getattr(server, field_name))
    enabled = os.getenv(prefix + "ENABLED")
    if enabled is not None:
        values["enabled"] = enabled.strip().lower() not in {"0", "false", "no", "off"}
    updated = replace(server, **values)
    if updated.transport not in VALID_MCP_TRANSPORTS:
        raise ValueError(f"unsupported MCP transport '{updated.transport}' for server '{updated.name}'")
    if updated.transport == "stdio" and not updated.command:
        raise ValueError(f"stdio MCP server '{updated.name}' requires command")
    if updated.transport in {"streamable_http", "sse"} and not updated.url:
        raise ValueError(f"{updated.transport} MCP server '{updated.name}' requires url")
    return updated
