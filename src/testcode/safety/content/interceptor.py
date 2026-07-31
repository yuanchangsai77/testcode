from __future__ import annotations

from typing import Protocol

from ...tools.base import ToolContext
from ...types import ToolAction, ToolDefinition, ToolResult
from .extractors import MutationExtractor
from .models import SafetyFinding
from .scanner import ContentScanner


class ExecutionInterceptor(Protocol):
    def before_execute(
        self,
        action: ToolAction,
        definition: ToolDefinition,
        context: ToolContext,
    ) -> ToolResult | None:
        ...


class SecretWriteGuard:
    def __init__(
        self,
        extractors: list[MutationExtractor],
        scanners: list[ContentScanner],
        logger=None,
    ) -> None:
        self.extractors = list(extractors)
        self.scanners = list(scanners)
        self.logger = logger

    def before_execute(
        self,
        action: ToolAction,
        definition: ToolDefinition,
        context: ToolContext,
    ) -> ToolResult | None:
        del context
        extractor = next(
            (
                candidate
                for candidate in self.extractors
                if candidate.supports(action, definition)
            ),
            None,
        )
        if extractor is None:
            return None

        mutations = extractor.extract(action)
        findings = [
            finding
            for mutation in mutations
            for scanner in self.scanners
            for finding in scanner.scan(mutation)
        ]
        findings = self._deduplicate(findings)
        self._record_scan(action.name, len(mutations), findings)
        if not findings:
            return None

        locations = [
            {
                "path": finding.path,
                **({"line": finding.line} if finding.line is not None else {}),
                "category": finding.category,
            }
            for finding in findings
        ]
        return ToolResult(
            name=action.name,
            success=False,
            output=(
                "Security policy blocked this write because it appears to contain "
                "a hardcoded credential. Read the value from a protected runtime "
                "source instead."
            ),
            error_code="blocked_by_security_policy",
            metadata={
                "policy_id": findings[0].policy_id,
                "finding_count": len(findings),
                "locations": locations,
            },
        )

    def _deduplicate(self, findings: list[SafetyFinding]) -> list[SafetyFinding]:
        return list(
            {
                (
                    finding.policy_id,
                    finding.category,
                    finding.path,
                    finding.line,
                ): finding
                for finding in findings
            }.values()
        )

    def _record_scan(
        self,
        tool_name: str,
        mutation_count: int,
        findings: list[SafetyFinding],
    ) -> None:
        if self.logger is None:
            return
        self.logger.record(
            (
                "safety.content_scan.blocked"
                if findings
                else "safety.content_scan.completed"
            ),
            {
                "tool": tool_name,
                "mutation_count": mutation_count,
                "finding_count": len(findings),
                "categories": sorted({item.category for item in findings}),
            },
        )
