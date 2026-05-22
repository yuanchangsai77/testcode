from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelClientConfig:
    base_url: str
    model: str = "gpt-5.4"
    timeout: float = 60.0


@dataclass(frozen=True, slots=True)
class CleanedContent:
    message: str
    thinking: str = ""
    had_protocol_tags: bool = False
