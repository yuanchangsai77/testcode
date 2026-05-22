from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    model_base_url: str = ""
    model_name: str = "gpt-5.4"
    model_timeout: float = 60.0
    mode: str = "confirm"


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


def load_runtime_config(mode: str | None = None) -> RuntimeConfig:
    return RuntimeConfig(
        model_base_url=os.getenv("TESTCODE_MODEL_BASE_URL", "").strip(),
        model_name=os.getenv("TESTCODE_MODEL_NAME", "gpt-5.4").strip() or "gpt-5.4",
        model_timeout=_float_env("TESTCODE_MODEL_TIMEOUT", 60.0),
        mode=mode or os.getenv("TESTCODE_MODE", "confirm").strip() or "confirm",
    )


def _float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default

    try:
        value = float(raw_value)
    except ValueError:
        return default

    return value if value > 0 else default
