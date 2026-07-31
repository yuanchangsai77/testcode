from __future__ import annotations

from typing import Protocol

from ..secret_patterns import (
    AWS_ACCESS_KEY_RE,
    assigned_secret_candidates,
    BEARER_VALUE_RE,
    PRIVATE_KEY_RE,
    TOKEN_VALUE_RE,
    URL_CREDENTIAL_RE,
    is_placeholder,
)
from .models import ContentMutation, SafetyFinding


class ContentScanner(Protocol):
    def scan(self, mutation: ContentMutation) -> list[SafetyFinding]:
        ...


class SecretScanner:
    policy_id = "SEC-CREDENTIAL-001"

    def scan(self, mutation: ContentMutation) -> list[SafetyFinding]:
        text = mutation.added_text
        categories: list[str] = []

        if PRIVATE_KEY_RE.search(text):
            categories.append("private_key")
        if TOKEN_VALUE_RE.search(text):
            categories.append("provider_token")
        if AWS_ACCESS_KEY_RE.search(text):
            categories.append("cloud_access_key")
        if BEARER_VALUE_RE.search(text):
            categories.append("bearer_token")
        if URL_CREDENTIAL_RE.search(text):
            categories.append("url_credential")
        if self._contains_secret_assignment(text):
            categories.append("credential_assignment")

        return [
            SafetyFinding(
                policy_id=self.policy_id,
                category=category,
                path=mutation.path,
                line=mutation.line,
            )
            for category in dict.fromkeys(categories)
        ]

    def _contains_secret_assignment(self, text: str) -> bool:
        return any(
            len(candidate) >= 12 and not is_placeholder(candidate)
            for candidate in assigned_secret_candidates(text)
        )
