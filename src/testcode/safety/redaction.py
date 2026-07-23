from __future__ import annotations

import re
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"

SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|auth|credential|password|secret|token|^key$)",
    re.IGNORECASE,
)
TOKEN_VALUE_RE = re.compile(
    r"(?i)\b(?:sk|pk|ghp|github_pat|xox[baprs])[-_a-z0-9]{12,}\b"
)
ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:[a-z0-9_.-]*(?:api[_-]?key|credential|password|secret|token)[a-z0-9_.-]*|key))\s*([=:])\s*([^\s,;&]+)"
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
            key: REDACTED if SENSITIVE_KEY_RE.search(str(key)) and str(key) not in {
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
    redacted = ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", value)
    redacted = BEARER_RE.sub(lambda match: f"{match.group(1)} {REDACTED}", redacted)
    return TOKEN_VALUE_RE.sub(REDACTED, redacted)
