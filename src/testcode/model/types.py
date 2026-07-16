from __future__ import annotations

from dataclasses import dataclass


class ModelRetryableError(RuntimeError):
    """Raised when a model request can reasonably be attempted again."""

    retry_status = "Model request failed"


class ModelTimeoutError(ModelRetryableError):
    """Raised when one model API attempt exceeds its timeout."""

    retry_status = "Model request timed out"


class ModelConnectionError(ModelRetryableError):
    """Raised when a transient network failure interrupts a model request."""

    retry_status = "Model connection interrupted"


class ModelServiceError(ModelRetryableError):
    """Raised when the model service asks the client to retry later."""

    retry_status = "Model service temporarily unavailable"


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
