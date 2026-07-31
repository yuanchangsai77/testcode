from .extractors import (
    FullContentMutationExtractor,
    PatchMutationExtractor,
    ShellCommandMutationExtractor,
)
from .interceptor import SecretWriteGuard
from .models import ContentMutation, SafetyFinding
from .scanner import SecretScanner


def build_content_safety_interceptors(logger=None):
    return [
        SecretWriteGuard(
            extractors=[
                PatchMutationExtractor(),
                FullContentMutationExtractor(),
                ShellCommandMutationExtractor(),
            ],
            scanners=[SecretScanner()],
            logger=logger,
        )
    ]


__all__ = [
    "ContentMutation",
    "FullContentMutationExtractor",
    "PatchMutationExtractor",
    "SafetyFinding",
    "ShellCommandMutationExtractor",
    "SecretScanner",
    "SecretWriteGuard",
    "build_content_safety_interceptors",
]
