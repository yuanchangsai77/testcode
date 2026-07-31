from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .secret_patterns import TOKEN_VALUE_RE, is_sensitive_field

REDACTED = "[REDACTED]"

ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    (?<![a-z0-9_.-])
    (?P<field_quote>["']?)
    (?P<field>[a-z_][a-z0-9_.-]{0,127})
    (?P=field_quote)
    (?P<separator>\s*(?:=|:(?!//))\s*)
    (?P<value>[^\s,;&]+)
    """
)
BEARER_RE = re.compile(
    r"(?i)\b(bearer)\s+[-._~+/=a-z0-9]{12,}\b"
)

SENSITIVE_FILENAMES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".npmrc",
    ".pypirc",
}
SENSITIVE_SUFFIXES = {
    ".key",
    ".pem",
    ".p12",
    ".pfx",
}


def is_sensitive_path(path: Path) -> bool:
    name = path.name.lower()
    if name in SENSITIVE_FILENAMES:
        return True
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        return True
    return any(part.lower() in {"secrets", ".secrets"} for part in path.parts)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_field(str(key)) and str(key) not in {
                "prompt_tokens", "completion_tokens", "total_tokens",
                "cached_tokens", "reasoning_tokens",
                "completion_tokens_details", "prompt_tokens_details"
            } else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    redacted = ASSIGNMENT_RE.sub(_redact_assignment, value)
    redacted = BEARER_RE.sub(lambda match: f"{match.group(1)} {REDACTED}", redacted)
    return TOKEN_VALUE_RE.sub(REDACTED, redacted)


def _redact_assignment(match: re.Match) -> str:
    field = match.group("field")
    if not is_sensitive_field(field):
        return match.group(0)
    if "auth" in field.casefold() and match.group("value").casefold() == "bearer":
        return match.group(0)
    separator = ":" if ":" in match.group("separator") else "="
    quote = match.group("field_quote")
    return f"{quote}{field}{quote}{separator}{REDACTED}"
