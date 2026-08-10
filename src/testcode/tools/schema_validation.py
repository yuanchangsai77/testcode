from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SchemaIssue:
    keyword: str
    path: str
    message: str
    expected: object | None = None
    actual: object | None = None


def validate_schema(value: object, schema: dict[str, Any], path: str = "") -> SchemaIssue | None:
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _matches_type(value, expected_type):
        return SchemaIssue("type", path, f"must have type {expected_type}", expected_type, type(value).__name__)

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return SchemaIssue("enum", path, f"must be one of {enum!r}", enum, value)

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = [str(name) for name in required if name not in value]
            if missing:
                return SchemaIssue("required", path, f"missing required argument(s): {', '.join(missing)}", missing)
        if schema.get("additionalProperties") is False:
            unknown = sorted(str(name) for name in value if name not in properties)
            if unknown:
                return SchemaIssue("additionalProperties", path, f"unknown argument(s): {', '.join(unknown)}", unknown)
        for name, item in value.items():
            child_schema = properties.get(name)
            if isinstance(child_schema, dict):
                issue = validate_schema(item, child_schema, _join(path, str(name)))
                if issue is not None:
                    return issue

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            return SchemaIssue("minItems", path, f"must contain at least {minimum} item(s)", minimum, len(value))
        if isinstance(maximum, int) and len(value) > maximum:
            return SchemaIssue("maxItems", path, f"must contain at most {maximum} item(s)", maximum, len(value))
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                issue = validate_schema(item, item_schema, f"{path}[{index}]")
                if issue is not None:
                    return issue

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            return SchemaIssue("minLength", path, f"must contain at least {minimum} character(s)", minimum, len(value))
        if isinstance(maximum, int) and len(value) > maximum:
            return SchemaIssue("maxLength", path, f"must contain at most {maximum} character(s)", maximum, len(value))
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                matched = re.search(pattern, value)
            except re.error:
                matched = None
            if matched is None:
                return SchemaIssue("pattern", path, f"must match pattern {pattern!r}", pattern, value)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            return SchemaIssue("finite", path, "must be finite", None, value)
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            return SchemaIssue("minimum", path, f"must be at least {minimum}", minimum, value)
        if isinstance(maximum, (int, float)) and value > maximum:
            return SchemaIssue("maximum", path, f"must be at most {maximum}", maximum, value)
    return None


def _matches_type(value: object, expected: str) -> bool:
    checks = {
        "array": lambda item: isinstance(item, list),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "object": lambda item: isinstance(item, dict),
        "string": lambda item: isinstance(item, str),
        "null": lambda item: item is None,
    }
    check = checks.get(expected)
    return True if check is None else check(value)


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name
