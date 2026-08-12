from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator
from uuid import uuid4

from ..types import StoredSession

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows support will need a platform lock adapter.
    fcntl = None


MEMBER_STATES = frozenset({"ready", "running", "blocked", "completed", "failed", "cancelled"})
PUBLIC_ENTRY_KINDS = frozenset({"status", "finding", "blocker", "verification", "artifact"})


@dataclass(slots=True)
class SessionImage:
    image_id: str
    name: str
    created_at: str
    cwd: str
    messages: list[dict[str, str]] = field(default_factory=list)
    active_capability_ids: list[str] = field(default_factory=list)
    source_session_id: str = ""
    description: str = ""


@dataclass(slots=True)
class ClusterMember:
    session_id: str
    role: str
    parent_session_id: str
    launch_source: str
    state: str
    created_at: str
    updated_at: str
    task_summary: str = ""
    session_image_id: str = ""
    attempt: int = 1
    task_id: str = ""
    allowed_effects: list[str] = field(default_factory=lambda: ["read"])
    allowed_resources: list[str] = field(default_factory=lambda: ["."])
    required_evidence: list[str] = field(default_factory=lambda: ["response"])
    approval_policy: str = "block"


@dataclass(slots=True)
class SharedStateEntry:
    entry_id: str
    author_session_id: str
    kind: str
    summary: str
    created_at: str
    revision: int
    artifact_ref: str = ""
    metadata: dict[str, object] = field(default_factory=dict)
    task_id: str = ""
    attempt: int = 1
    lifecycle_state: str = "active"
    trust_class: str = "untrusted_observation"
    validation_state: str = "unchecked"
    supersedes: str = ""


@dataclass(slots=True)
class SessionCluster:
    cluster_id: str
    root_session_id: str
    created_at: str
    updated_at: str
    revision: int = 0
    members: list[ClusterMember] = field(default_factory=list)
    shared_state: list[SharedStateEntry] = field(default_factory=list)


class SessionImageStore:
    """Immutable launch images, deliberately separate from session history."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
        self.base_dir = root / ".testcode" / "session-images"

    def create(
        self,
        name: str,
        *,
        cwd: str,
        messages: list[dict[str, str]] | None = None,
        active_capability_ids: list[str] | None = None,
        source_session_id: str = "",
        description: str = "",
    ) -> SessionImage:
        name = _bounded_text(name, "image name", 120, required=True)
        image = SessionImage(
            image_id=_new_id("image"),
            name=name,
            created_at=_timestamp(),
            cwd=str(Path(cwd).resolve()),
            messages=_normalize_messages(messages or []),
            active_capability_ids=_string_list(active_capability_ids or []),
            source_session_id=source_session_id,
            description=_bounded_text(description, "image description", 1000),
        )
        self._write_new(image)
        return image

    def create_from_session(
        self,
        session: StoredSession,
        *,
        name: str,
        description: str = "",
    ) -> SessionImage:
        return self.create(
            name,
            cwd=session.cwd,
            messages=session.messages,
            active_capability_ids=session.active_capability_ids,
            source_session_id=session.session_id,
            description=description,
        )

    def load(self, image_id: str) -> SessionImage | None:
        if not _valid_id(image_id, "image"):
            return None
        path = self.base_dir / f"{image_id}.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return SessionImage(
            image_id=str(payload["image_id"]),
            name=str(payload["name"]),
            created_at=str(payload["created_at"]),
            cwd=str(payload["cwd"]),
            messages=_normalize_messages(payload.get("messages", [])),
            active_capability_ids=_string_list(payload.get("active_capability_ids", [])),
            source_session_id=str(payload.get("source_session_id", "")),
            description=str(payload.get("description", "")),
        )

    def list_images(self) -> list[SessionImage]:
        if not self.base_dir.exists():
            return []
        images: list[SessionImage] = []
        for path in self.base_dir.glob("image-*.json"):
            try:
                image = self.load(path.stem)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            if image is not None:
                images.append(image)
        return sorted(images, key=lambda item: item.created_at, reverse=True)

    def _write_new(self, image: SessionImage) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self.base_dir / f"{image.image_id}.json"
        with _locked(path.with_suffix(".lock")):
            if path.exists():
                raise FileExistsError(f"session image already exists: {image.image_id}")
            _atomic_json_write(path, asdict(image))


class SessionClusterStore:
    """Versioned public state shared by related sessions without direct messaging."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
        self.base_dir = root / ".testcode" / "session-clusters"

    def create(self, root_session_id: str) -> SessionCluster:
        if not root_session_id:
            raise ValueError("root_session_id is required")
        now = _timestamp()
        cluster = SessionCluster(
            cluster_id=_new_id("cluster"),
            root_session_id=root_session_id,
            created_at=now,
            updated_at=now,
            members=[
                ClusterMember(
                    session_id=root_session_id,
                    role="primary",
                    parent_session_id="",
                    launch_source="direct",
                    state="running",
                    created_at=now,
                    updated_at=now,
                )
            ],
        )
        self._write_new(cluster)
        return cluster

    def load(self, cluster_id: str) -> SessionCluster | None:
        if not _valid_id(cluster_id, "cluster"):
            return None
        path = self.base_dir / f"{cluster_id}.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return self._from_payload(payload)

    def add_member(self, cluster_id: str, member: ClusterMember) -> SessionCluster:
        if member.role != "subagent":
            raise ValueError("new cluster members must have the subagent role")
        if member.launch_source not in {"inherit", "fresh", "image"}:
            raise ValueError(f"unsupported launch source: {member.launch_source}")
        if member.state not in MEMBER_STATES:
            raise ValueError(f"unsupported member state: {member.state}")
        member.task_summary = _bounded_text(member.task_summary, "task summary", 2000)

        def mutate(cluster: SessionCluster) -> None:
            if any(item.session_id == member.session_id for item in cluster.members):
                raise ValueError(f"session is already a cluster member: {member.session_id}")
            if not any(item.session_id == member.parent_session_id for item in cluster.members):
                raise ValueError("parent session is not a cluster member")
            cluster.members.append(member)

        return self._update(cluster_id, mutate)

    def update_member_state(
        self,
        cluster_id: str,
        session_id: str,
        state: str,
        *,
        task_summary: str | None = None,
    ) -> SessionCluster:
        if state not in MEMBER_STATES:
            raise ValueError(f"unsupported member state: {state}")

        def mutate(cluster: SessionCluster) -> None:
            member = _member(cluster, session_id)
            member.state = state
            member.updated_at = _timestamp()
            if task_summary is not None:
                member.task_summary = _bounded_text(task_summary, "task summary", 2000)

        return self._update(cluster_id, mutate)

    def claim_ready_member(self, cluster_id: str, session_id: str) -> bool:
        """Atomically claim one ready member for a runner."""
        claimed: list[bool] = []

        def mutate(cluster: SessionCluster) -> None:
            member = _member(cluster, session_id)
            if member.state != "ready":
                claimed.append(False)
                return
            member.state = "running"
            member.updated_at = _timestamp()
            claimed.append(True)

        self._update(cluster_id, mutate, increment_if=lambda: claimed == [True])
        return claimed == [True]

    def resume_member(self, cluster_id: str, session_id: str, task_summary: str) -> SessionCluster:
        """Prepare a terminal subagent member for another attempt in the same session."""
        task_summary = _bounded_text(task_summary, "task summary", 2000, required=True)

        def mutate(cluster: SessionCluster) -> None:
            member = _member(cluster, session_id)
            if member.role != "subagent":
                raise ValueError("only subagent members may be resumed")
            if member.state not in {"blocked", "completed", "failed", "cancelled"}:
                raise ValueError(f"subagent in state {member.state!r} cannot be resumed")
            member.state = "ready"
            member.task_summary = task_summary
            member.attempt += 1
            member.updated_at = _timestamp()

        return self._update(cluster_id, mutate)

    def publish(
        self,
        cluster_id: str,
        author_session_id: str,
        kind: str,
        summary: str,
        *,
        artifact_ref: str = "",
        metadata: dict[str, object] | None = None,
    ) -> SharedStateEntry:
        if kind not in PUBLIC_ENTRY_KINDS:
            raise ValueError(f"unsupported public state kind: {kind}")
        summary = _bounded_text(summary, "public state summary", 2000, required=True)
        artifact_ref = _artifact_ref(artifact_ref)
        metadata = dict(metadata or {})
        if len(json.dumps(metadata, ensure_ascii=False)) > 8192:
            raise ValueError("public state metadata exceeds 8192 characters")
        created: list[SharedStateEntry] = []

        def mutate(cluster: SessionCluster) -> None:
            _member(cluster, author_session_id)
            entry = SharedStateEntry(
                entry_id=_new_id("state"),
                author_session_id=author_session_id,
                kind=kind,
                summary=summary,
                created_at=_timestamp(),
                revision=cluster.revision + 1,
                artifact_ref=artifact_ref,
                metadata=metadata,
                task_id=str(metadata.get("task_id", "")),
                attempt=int(metadata.get("attempt", 1)),
                lifecycle_state=str(metadata.get("lifecycle_state", "active")),
                trust_class=str(metadata.get("trust_class", "untrusted_observation")),
                validation_state=str(metadata.get("validation_state", "unchecked")),
                supersedes=str(metadata.get("supersedes", "")),
            )
            cluster.shared_state.append(entry)
            created.append(entry)

        self._update(cluster_id, mutate)
        return created[0]

    def finish_member_attempt(
        self,
        cluster_id: str,
        session_id: str,
        attempt: int,
        state: str,
        kind: str,
        summary: str,
        *,
        expected_states: frozenset[str] = frozenset({"running"}),
        artifact_ref: str = "",
        metadata: dict[str, object] | None = None,
    ) -> SharedStateEntry | None:
        """Atomically publish one terminal result if the claimed attempt still owns the member."""
        if state not in MEMBER_STATES:
            raise ValueError(f"unsupported member state: {state}")
        if kind not in PUBLIC_ENTRY_KINDS:
            raise ValueError(f"unsupported public state kind: {kind}")
        summary = _bounded_text(summary, "public state summary", 2000, required=True)
        artifact_ref = _artifact_ref(artifact_ref)
        metadata = dict(metadata or {})
        if len(json.dumps(metadata, ensure_ascii=False)) > 8192:
            raise ValueError("public state metadata exceeds 8192 characters")
        created: list[SharedStateEntry] = []
        committed: list[bool] = []

        def mutate(cluster: SessionCluster) -> None:
            member = _member(cluster, session_id)
            if member.attempt != attempt or member.state not in expected_states:
                committed.append(False)
                return
            member.state = state
            member.updated_at = _timestamp()
            previous = next(
                (
                    item for item in reversed(cluster.shared_state)
                    if item.author_session_id == session_id
                ),
                None,
            )
            entry = SharedStateEntry(
                entry_id=_new_id("state"),
                author_session_id=session_id,
                kind=kind,
                summary=summary,
                created_at=_timestamp(),
                revision=cluster.revision + 1,
                artifact_ref=artifact_ref,
                metadata=metadata,
                task_id=member.task_id,
                attempt=attempt,
                lifecycle_state=state,
                trust_class=str(metadata.get("trust_class", "untrusted_observation")),
                validation_state=str(metadata.get("validation_state", "validated")),
                supersedes=previous.entry_id if previous is not None else "",
            )
            cluster.shared_state.append(entry)
            created.append(entry)
            committed.append(True)

        self._update(cluster_id, mutate, increment_if=lambda: committed == [True])
        return created[0] if created else None

    def _update(
        self,
        cluster_id: str,
        mutate: Callable[[SessionCluster], None],
        *,
        increment_if: Callable[[], bool] | None = None,
    ) -> SessionCluster:
        if not _valid_id(cluster_id, "cluster"):
            raise ValueError("invalid cluster id")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self.base_dir / f"{cluster_id}.json"
        with _locked(path.with_suffix(".lock")):
            cluster = self.load(cluster_id)
            if cluster is None:
                raise KeyError(f"unknown session cluster: {cluster_id}")
            mutate(cluster)
            if increment_if is not None and not increment_if():
                return cluster
            cluster.revision += 1
            cluster.updated_at = _timestamp()
            _atomic_json_write(path, asdict(cluster))
            return cluster

    def _write_new(self, cluster: SessionCluster) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self.base_dir / f"{cluster.cluster_id}.json"
        with _locked(path.with_suffix(".lock")):
            if path.exists():
                raise FileExistsError(f"session cluster already exists: {cluster.cluster_id}")
            _atomic_json_write(path, asdict(cluster))

    def _from_payload(self, payload: dict[str, object]) -> SessionCluster:
        members = [ClusterMember(**item) for item in payload.get("members", []) if isinstance(item, dict)]
        entries = [
            SharedStateEntry(**item) for item in payload.get("shared_state", []) if isinstance(item, dict)
        ]
        return SessionCluster(
            cluster_id=str(payload["cluster_id"]),
            root_session_id=str(payload["root_session_id"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            revision=int(payload.get("revision", 0)),
            members=members,
            shared_state=entries,
        )


def _member(cluster: SessionCluster, session_id: str) -> ClusterMember:
    for member in cluster.members:
        if member.session_id == session_id:
            return member
    raise ValueError("only cluster members may access public state")


def _normalize_messages(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {"role": str(item["role"]), "content": str(item["content"])}
        for item in value
        if isinstance(item, dict) and isinstance(item.get("role"), str) and isinstance(item.get("content"), str)
    ]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _bounded_text(value: str, label: str, limit: int, *, required: bool = False) -> str:
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{label} is required")
    if len(normalized) > limit:
        raise ValueError(f"{label} exceeds {limit} characters")
    return normalized


def _artifact_ref(value: str) -> str:
    value = _bounded_text(value, "artifact reference", 500)
    if not value:
        return ""
    if value.startswith("artifact:"):
        return value
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("artifact reference must be an opaque artifact id or a safe relative path")
    return value


def _valid_id(value: str, prefix: str) -> bool:
    return bool(value) and value.startswith(f"{prefix}-") and Path(value).name == value


def _new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:12]}"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _atomic_json_write(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
