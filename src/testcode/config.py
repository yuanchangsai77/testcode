from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .mcp.config import MCPServerConfig, load_mcp_server_configs


# These caps are deliberately kept in code as safety boundaries. Values in
# config.toml may tune defaults, but cannot make a run consume unbounded time,
# memory, or model context.
MAX_MODEL_RETRIES = 20
MAX_RETRY_DELAY_SECONDS = 60.0
MAX_MODEL_TURNS = 500
MAX_MCP_TOOLS_PER_SERVER = 1_024
MAX_ACTIVE_CAPABILITIES = 32
MAX_TOOL_OUTPUT_BYTES = 1_048_576
MAX_READ_FILE_BYTES = 1_048_576
MAX_TOOL_RESULTS = 2_000


@dataclass(frozen=True, slots=True)
class ModelRetryConfig:
    max_retries: int = 7
    delays: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0)


@dataclass(frozen=True, slots=True)
class OrchestrationConfig:
    max_turns: int = 100


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    mcp_tools_per_server: int = 256
    active_capabilities: int = 8
    tool_output_bytes: int = 32_000
    read_file_bytes: int = 64_000
    list_dir_entries: int = 200
    search_results: int = 200


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    model_base_url: str = ""
    model_name: str = "gpt-5.4"
    model_timeout: float = 60.0
    mode: str = "confirm"
    mcp_servers: tuple[MCPServerConfig, ...] = ()
    model_retry: ModelRetryConfig = ModelRetryConfig()
    orchestration: OrchestrationConfig = OrchestrationConfig()
    limits: RuntimeLimits = RuntimeLimits()


def load_dotenv(env_path: str | Path | None = None) -> None:
    path = Path(env_path) if env_path is not None else Path(__file__).resolve().parents[2] / ".env"
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue

        if (
            (value.startswith('"') and value.endswith('"')) or
            (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1]

        os.environ[key] = value


def load_runtime_config(mode: str | None = None, cwd: str | Path | None = None) -> RuntimeConfig:
    tuning = _load_tuning(cwd)
    return RuntimeConfig(
        model_base_url=os.getenv("TESTCODE_MODEL_BASE_URL", "").strip(),
        model_name=os.getenv("TESTCODE_MODEL_NAME", "gpt-5.4").strip() or "gpt-5.4",
        model_timeout=_float_env("TESTCODE_MODEL_TIMEOUT", 60.0),
        mode=mode or os.getenv("TESTCODE_MODE", "confirm").strip() or "confirm",
        mcp_servers=load_mcp_server_configs(cwd=cwd),
        model_retry=tuning[0],
        orchestration=tuning[1],
        limits=tuning[2],
    )


def _load_tuning(cwd: str | Path | None) -> tuple[ModelRetryConfig, OrchestrationConfig, RuntimeLimits]:
    root = Path(cwd or os.getcwd())
    values: dict[str, object] = {}
    for path in (Path.home() / ".testcode" / "config.toml", root / ".testcode" / "config.toml"):
        if not path.exists():
            continue
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            continue
        _merge_table(values, "model.retry", raw.get("model", {}), "retry")
        _merge_table(values, "orchestration", raw.get("orchestration", {}))
        _merge_table(values, "limits", raw.get("limits", {}))

    max_retries = _bounded_int(values.get("model.retry.max_retries"), 7, 0, MAX_MODEL_RETRIES, "model.retry.max_retries")
    delays = _retry_delays(values.get("model.retry.delays"), max_retries)
    return (
        ModelRetryConfig(max_retries=max_retries, delays=delays),
        OrchestrationConfig(
            max_turns=_bounded_int(values.get("orchestration.max_turns"), 100, 1, MAX_MODEL_TURNS, "orchestration.max_turns")
        ),
        RuntimeLimits(
            mcp_tools_per_server=_bounded_int(values.get("limits.mcp_tools_per_server"), 256, 1, MAX_MCP_TOOLS_PER_SERVER, "limits.mcp_tools_per_server"),
            active_capabilities=_bounded_int(values.get("limits.active_capabilities"), 8, 1, MAX_ACTIVE_CAPABILITIES, "limits.active_capabilities"),
            tool_output_bytes=_bounded_int(values.get("limits.tool_output_bytes"), 32_000, 1, MAX_TOOL_OUTPUT_BYTES, "limits.tool_output_bytes"),
            read_file_bytes=_bounded_int(values.get("limits.read_file_bytes"), 64_000, 1, MAX_READ_FILE_BYTES, "limits.read_file_bytes"),
            list_dir_entries=_bounded_int(values.get("limits.list_dir_entries"), 200, 1, MAX_TOOL_RESULTS, "limits.list_dir_entries"),
            search_results=_bounded_int(values.get("limits.search_results"), 200, 1, MAX_TOOL_RESULTS, "limits.search_results"),
        ),
    )


def _merge_table(target: dict[str, object], prefix: str, raw: object, child: str | None = None) -> None:
    if not isinstance(raw, dict):
        return
    table = raw.get(child, {}) if child else raw
    if not isinstance(table, dict):
        return
    for key, value in table.items():
        target[f"{prefix}.{key}"] = value


def _bounded_int(value: object, default: int, minimum: int, maximum: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum} (internal hard limit)")
    return value


def _retry_delays(value: object, max_retries: int) -> tuple[float, ...]:
    default = ModelRetryConfig().delays
    if value is None:
        return default
    if not isinstance(value, list) or not value:
        raise ValueError("model.retry.delays must be a non-empty array of seconds")
    if len(value) > MAX_MODEL_RETRIES:
        raise ValueError(f"model.retry.delays can contain at most {MAX_MODEL_RETRIES} entries (internal hard limit)")
    delays: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not 0 <= float(item) <= MAX_RETRY_DELAY_SECONDS:
            raise ValueError(f"model.retry.delays entries must be between 0 and {MAX_RETRY_DELAY_SECONDS}")
        delays.append(float(item))
    if max_retries and not delays:
        raise ValueError("model.retry.delays is required when model.retry.max_retries is greater than 0")
    return tuple(delays)


def _float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default

    try:
        value = float(raw_value)
    except ValueError:
        return default

    return value if value > 0 else default
