from __future__ import annotations

import re

TOKEN_VALUE_RE = re.compile(
    r"(?i)\b(?:sk|pk|ghp|github_pat|xox[baprs])[-_a-z0-9]{12,}\b"
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
)
BEARER_VALUE_RE = re.compile(
    r"(?i)\bbearer\s+[-._~+/=a-z0-9]{12,}\b"
)
AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
ASSIGNMENT_PREFIX_RE = re.compile(
    r"""(?ix)
    (?<![a-z0-9_.-])
    (?P<field_quote>["']?)
    (?P<field>[a-z_][a-z0-9_.-]{0,127})
    (?P=field_quote)
    \s*[:=]\s*
    """
)
QUOTED_FRAGMENT_RE = re.compile(r"""["'](?P<value>[^"']*)["']""")
UNQUOTED_VALUE_RE = re.compile(r"(?i)^(?P<value>[a-z0-9_./+=-]{12,})")
RUNTIME_REFERENCE_RE = re.compile(
    r"(?ix)^(?:"
    r"os\.getenv|(?:os\.)?environ|process\.env|settings\.|config\.get|"
    r"\$\{?|\bgetenv\s*\()"
)
URL_CREDENTIAL_RE = re.compile(
    r"(?i)\bhttps?://[^/\s:@]+:[^/\s@]+@[^/\s]+"
)

PLACEHOLDER_RE = re.compile(
    r"""(?ix)^(
        \$\{?[a-z_][a-z0-9_]*\}?
        |your[_-][a-z0-9_-]+
        |<[^>]+>
        |example[_-][a-z0-9_-]+
        |replace[_-]?me
        |changeme
    )$"""
)


def is_placeholder(value: str) -> bool:
    return PLACEHOLDER_RE.fullmatch(value.strip()) is not None


def is_sensitive_field(field: str) -> bool:
    normalized = field.strip("\"'").casefold()
    segments = [part for part in re.split(r"[_.-]+", normalized) if part]
    if any(
        marker in normalized
        for marker in (
            "api_key",
            "apikey",
            "auth",
            "secret",
            "token",
            "password",
            "passwd",
            "credential",
        )
    ):
        return True
    if "key" in segments:
        return True
    return re.search(
        r"(?:api|access|client|service|private|web)key$",
        normalized,
    ) is not None


def assigned_secret_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for match in ASSIGNMENT_PREFIX_RE.finditer(text):
        if not is_sensitive_field(match.group("field")):
            continue
        window = text[match.end():match.end() + 512].lstrip()
        if window.startswith("("):
            closing = window.find(")")
            window = window if closing < 0 else window[:closing + 1]
        else:
            window = window.splitlines()[0] if window else ""
        window = window.strip()
        if not window or RUNTIME_REFERENCE_RE.match(window):
            continue
        fragments = [
            item.group("value")
            for item in QUOTED_FRAGMENT_RE.finditer(window)
        ]
        if fragments:
            candidates.append("".join(fragments[:8]))
            continue
        unquoted = UNQUOTED_VALUE_RE.match(window)
        if unquoted is not None:
            candidates.append(unquoted.group("value"))
    return candidates
