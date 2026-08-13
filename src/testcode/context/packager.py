from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextPackageStats:
    budget_chars: int
    included_chars: int
    omitted_messages: int = 0
    truncated_system: bool = False
    truncated_current: bool = False


@dataclass(frozen=True, slots=True)
class ContextSegment:
    content: str
    label: str
    priority: int = 50
    required: bool = False
    truncatable: bool = True


class ContextPackager:
    """Build a bounded prompt while preserving control-plane facts first."""

    def __init__(self, max_chars: int = 120_000) -> None:
        self.max_chars = max(4_000, int(max_chars))
        self.last_stats = ContextPackageStats(self.max_chars, 0)

    def package(
        self,
        system_content: str,
        conversation: list[dict[str, object]],
        current_content: str,
    ) -> list[dict[str, object]]:
        return self.package_segments(
            [ContextSegment(system_content, "system", required=True)],
            conversation,
            [ContextSegment(current_content, "current request", priority=100, required=True)],
        )

    def package_segments(
        self,
        system_segments: list[ContextSegment],
        conversation: list[dict[str, object]],
        current_segments: list[ContextSegment],
    ) -> list[dict[str, object]]:
        system_limit = max(2_000, int(self.max_chars * 0.55))
        system, truncated_system = self._pack_segments(system_segments, system_limit)
        current_budget = max(1_000, self.max_chars - len(system))
        current, truncated_current = self._pack_segments(current_segments, current_budget)
        remaining = max(0, self.max_chars - len(system) - len(current))

        selected: list[dict[str, object]] = []
        omitted = 0
        for message in reversed(conversation):
            content = message.get("content")
            role = message.get("role")
            if role not in {"user", "assistant"} or not isinstance(content, str) or not content:
                continue
            cost = len(content)
            if cost > remaining:
                omitted += 1
                continue
            selected.append({"role": role, "content": content})
            remaining -= cost
        selected.reverse()
        if omitted:
            marker = f"[Runtime context omitted {omitted} older conversation messages.]"
            while selected and len(marker) > remaining:
                removed = selected.pop(0)
                remaining += len(str(removed.get("content", "")))
                omitted += 1
                marker = f"[Runtime context omitted {omitted} older conversation messages.]"
            if len(marker) <= remaining:
                selected.insert(0, {"role": "user", "content": marker})

        included = len(system) + len(current) + sum(
            len(str(item.get("content", ""))) for item in selected
        )
        self.last_stats = ContextPackageStats(
            budget_chars=self.max_chars,
            included_chars=included,
            omitted_messages=omitted,
            truncated_system=truncated_system,
            truncated_current=truncated_current,
        )
        return [
            {"role": "system", "content": system},
            *selected,
            {"role": "user", "content": current},
        ]

    def _pack_segments(
        self,
        segments: list[ContextSegment],
        limit: int,
    ) -> tuple[str, bool]:
        candidates = [item for item in segments if item.content]
        required = [(index, item) for index, item in enumerate(candidates) if item.required]
        required_cost = sum(len(item.content) for _, item in required) + max(0, len(required) - 1) * 2
        if required and required_cost > limit:
            selected: dict[int, str] = {}
            remaining = limit
            for position, (index, segment) in enumerate(required):
                separator = 2 if selected else 0
                available = remaining - separator
                share = available // max(1, len(required) - position)
                clipped = (
                    segment.content
                    if len(segment.content) <= share
                    else self._clip_segment(segment, share)
                )
                if clipped:
                    selected[index] = clipped
                    remaining -= len(clipped) + separator
            return "\n\n".join(selected[index] for index in sorted(selected)), True
        ranked = sorted(
            enumerate(candidates),
            key=lambda item: (not item[1].required, -item[1].priority, item[0]),
        )
        selected: dict[int, str] = {}
        remaining = limit
        truncated = False
        for index, segment in ranked:
            separator = 2 if selected else 0
            available = remaining - separator
            if available <= 0:
                truncated = True
                continue
            if len(segment.content) <= available:
                selected[index] = segment.content
                remaining -= len(segment.content) + separator
                continue
            truncated = True
            if not segment.truncatable and not segment.required:
                continue
            clipped = self._clip_segment(segment, available)
            if clipped:
                selected[index] = clipped
                remaining -= len(clipped) + separator
        return "\n\n".join(selected[index] for index in sorted(selected)), truncated

    def _clip_segment(self, segment: ContextSegment, limit: int) -> str:
        marker = f"\n[Runtime truncated {segment.label}.]\n"
        if limit <= len(marker):
            return ""
        lines = segment.content.splitlines(keepends=True)
        if len(lines) <= 1:
            return self._clip_ends(segment.content, limit)
        available = limit - len(marker)
        head: list[str] = []
        tail: list[str] = []
        head_chars = 0
        tail_chars = 0
        head_limit = int(available * 0.6)
        for line in lines:
            if head_chars + len(line) > head_limit:
                break
            head.append(line)
            head_chars += len(line)
        for line in reversed(lines[len(head):]):
            if head_chars + tail_chars + len(line) > available:
                break
            tail.append(line)
            tail_chars += len(line)
        return "".join(head) + marker + "".join(reversed(tail))

    def _clip_ends(self, value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        marker = "\n[Runtime omitted lower-priority context.]\n"
        available = max(0, limit - len(marker))
        head = max(1, int(available * 0.4))
        tail = max(0, available - head)
        return value[:head] + marker + (value[-tail:] if tail else "")
