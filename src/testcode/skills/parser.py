from __future__ import annotations

from pathlib import Path
from typing import Any


def parse_frontmatter(content: str) -> dict[str, Any]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    frontmatter_lines = []
    found_end = False
    for line in lines[1:]:
        if line.strip() == "---":
            found_end = True
            break
        frontmatter_lines.append(line)

    if not found_end:
        return {}

    metadata = {}
    current_key = None
    list_accumulator = []

    for line in frontmatter_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Is it a list item under a key?
        if stripped.startswith("-"):
            val = stripped[1:].strip()
            # remove quotes
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            list_accumulator.append(val)
            continue

        # If we had a list accumulator, save it to the previous key
        if current_key and list_accumulator:
            metadata[current_key] = list_accumulator
            list_accumulator = []
            current_key = None

        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()

            # Clean quotes if any
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]

            # If val is empty, it could be a list start like `triggers:`
            if not val:
                current_key = key
                list_accumulator = []
            else:
                # Could it be an inline list like ["a", "b"]?
                if val.startswith("[") and val.endswith("]"):
                    # parse inline list
                    items = []
                    for item in val[1:-1].split(","):
                        item = item.strip()
                        if (item.startswith('"') and item.endswith('"')) or (item.startswith("'") and item.endswith("'")):
                            item = item[1:-1]
                        if item:
                            items.append(item)
                    metadata[key] = items
                else:
                    metadata[key] = val

    if current_key and list_accumulator:
        metadata[current_key] = list_accumulator

    return metadata


def read_frontmatter_only(path: Path) -> str:
    lines = []
    try:
        with path.open("r", encoding="utf-8") as f:
            first_line = f.readline()
            if not first_line or first_line.strip() != "---":
                return ""
            lines.append(first_line)

            for line in f:
                lines.append(line)
                if line.strip() == "---":
                    break
    except OSError:
        return ""
    return "".join(lines)


def load_skill_content(path: Path) -> tuple[dict[str, Any], str]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return {}, ""

    metadata = parse_frontmatter(content)

    lines = content.splitlines()
    body = ""
    if lines and lines[0].strip() == "---":
        found_end = False
        idx = 1
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                found_end = True
                idx = i + 1
                break
        if found_end:
            body = "\n".join(lines[idx:])
        else:
            body = content
    else:
        body = content

    return metadata, body.strip()
