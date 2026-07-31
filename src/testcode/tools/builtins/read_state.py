from __future__ import annotations

import hashlib
from pathlib import Path


def snapshot(path: Path) -> tuple[bytes, str, int]:
    data = path.read_bytes()
    return data, hashlib.sha256(data).hexdigest(), path.stat().st_mtime_ns


def text_lines(data: bytes) -> list[str]:
    return data.decode("utf-8", errors="replace").splitlines()


def record_observation(
    state: dict,
    path: Path,
    *,
    sha256: str,
    mtime_ns: int,
    lines: dict[int, str],
    empty: bool = False,
) -> None:
    read_files = state.setdefault("read_files", {})
    entry = read_files.setdefault(str(path), {"path": str(path), "observations": []})
    entry["sha256"] = sha256
    entry["mtime_ns"] = mtime_ns
    entry.setdefault("observations", []).append(
        {
            "sha256": sha256,
            "mtime_ns": mtime_ns,
            "lines": dict(lines),
            "empty": bool(empty),
        }
    )


def observed_line_matches(entry: dict, line_no: int, content: str) -> bool:
    return any(
        observation.get("lines", {}).get(line_no) == content
        for observation in entry.get("observations", [])
    )


def observed_line(entry: dict, line_no: int) -> bool:
    return any(
        line_no in observation.get("lines", {})
        for observation in entry.get("observations", [])
    )


def observed_empty_file(entry: dict) -> bool:
    return any(observation.get("empty") for observation in entry.get("observations", []))


def snapshot_changed(entry: dict, current_sha256: str) -> bool:
    observations = entry.get("observations", [])
    return bool(observations) and all(
        observation.get("sha256") != current_sha256
        for observation in observations
    )
