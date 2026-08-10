from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..sessions.cluster import (
    ClusterMember,
    SessionCluster,
    SessionClusterStore,
    SessionImage,
    SessionImageStore,
    SharedStateEntry,
)
from ..sessions.store import SessionStore
from ..types import StoredSession


@dataclass(slots=True)
class SubagentLaunchSpec:
    source: str = "inherit"
    task_summary: str = ""
    cwd: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)
    active_skills: list[str] = field(default_factory=list)
    active_capability_ids: list[str] = field(default_factory=list)
    image_id: str = ""


class SubagentCoordinator:
    """Creates related sessions and exposes only their versioned public state."""

    def __init__(
        self,
        session_store: SessionStore,
        cluster_store: SessionClusterStore,
        image_store: SessionImageStore,
    ) -> None:
        self.session_store = session_store
        self.cluster_store = cluster_store
        self.image_store = image_store

    def ensure_cluster(self, session: StoredSession) -> SessionCluster:
        if session.cluster_id:
            cluster = self.cluster_store.load(session.cluster_id)
            if cluster is None:
                raise RuntimeError(f"session references a missing cluster: {session.cluster_id}")
            if not any(member.session_id == session.session_id for member in cluster.members):
                raise RuntimeError("session is not registered in its referenced cluster")
            return cluster

        cluster = self.cluster_store.create(session.session_id)
        session.cluster_id = cluster.cluster_id
        session.session_role = "primary"
        self.session_store.save(session)
        return cluster

    def launch_subagent(self, parent: StoredSession, spec: SubagentLaunchSpec) -> StoredSession:
        if spec.source not in {"inherit", "fresh", "image"}:
            raise ValueError(f"unsupported subagent launch source: {spec.source}")
        cluster = self.ensure_cluster(parent)
        if not any(member.session_id == parent.session_id for member in cluster.members):
            raise ValueError("parent session is not a member of the session cluster")

        cwd, messages, skills, capabilities, image_id = self._launch_material(parent, spec)
        child = self.session_store.create(
            cwd=cwd,
            messages=messages,
            parent_session_id=parent.session_id,
            cluster_id=cluster.cluster_id,
            session_role="subagent",
            launch_source=spec.source,
            session_image_id=image_id,
        )
        child.active_skills = skills
        child.active_capability_ids = capabilities
        self.session_store.save(child)

        now = child.created_at
        member = ClusterMember(
            session_id=child.session_id,
            role="subagent",
            parent_session_id=parent.session_id,
            launch_source=spec.source,
            state="ready",
            created_at=now,
            updated_at=now,
            task_summary=spec.task_summary.strip(),
            session_image_id=image_id,
        )
        self.cluster_store.add_member(cluster.cluster_id, member)
        return child

    def resume_subagent(
        self,
        parent: StoredSession,
        session_id: str,
        task_summary: str,
    ) -> StoredSession:
        """Continue an existing direct child with its persisted context and trace."""
        cluster = self.ensure_cluster(parent)
        member = next(
            (item for item in cluster.members if item.session_id == session_id),
            None,
        )
        if member is None or member.parent_session_id != parent.session_id:
            raise ValueError("session is not a direct subagent of the requester")
        child = self.session_store.load(session_id)
        if child is None:
            raise KeyError(f"unknown subagent session: {session_id}")
        self.cluster_store.resume_member(cluster.cluster_id, session_id, task_summary)
        child.status = "active"
        self.session_store.save(child)
        return child

    def save_image(
        self,
        session: StoredSession,
        *,
        name: str,
        description: str = "",
    ) -> SessionImage:
        return self.image_store.create_from_session(session, name=name, description=description)

    def update_member_state(
        self,
        session: StoredSession,
        state: str,
        *,
        task_summary: str | None = None,
    ) -> SessionCluster:
        if not session.cluster_id:
            raise ValueError("session is not part of a cluster")
        return self.cluster_store.update_member_state(
            session.cluster_id,
            session.session_id,
            state,
            task_summary=task_summary,
        )

    def publish_state(
        self,
        session: StoredSession,
        kind: str,
        summary: str,
        *,
        artifact_ref: str = "",
        metadata: dict[str, object] | None = None,
    ) -> SharedStateEntry:
        if not session.cluster_id:
            raise ValueError("session is not part of a cluster")
        return self.cluster_store.publish(
            session.cluster_id,
            session.session_id,
            kind,
            summary,
            artifact_ref=artifact_ref,
            metadata=metadata,
        )

    def finish_attempt(
        self,
        session: StoredSession,
        attempt: int,
        state: str,
        kind: str,
        summary: str,
        *,
        expected_states: frozenset[str] = frozenset({"running"}),
        artifact_ref: str = "",
        metadata: dict[str, object] | None = None,
    ) -> SharedStateEntry | None:
        if not session.cluster_id:
            raise ValueError("session is not part of a cluster")
        return self.cluster_store.finish_member_attempt(
            session.cluster_id,
            session.session_id,
            attempt,
            state,
            kind,
            summary,
            expected_states=expected_states,
            artifact_ref=artifact_ref,
            metadata=metadata,
        )

    def snapshot(self, session: StoredSession) -> SessionCluster:
        if not session.cluster_id:
            raise ValueError("session is not part of a cluster")
        cluster = self.cluster_store.load(session.cluster_id)
        if cluster is None:
            raise KeyError(f"unknown session cluster: {session.cluster_id}")
        if not any(member.session_id == session.session_id for member in cluster.members):
            raise ValueError("only cluster members may read public state")
        return cluster

    def _launch_material(
        self,
        parent: StoredSession,
        spec: SubagentLaunchSpec,
    ) -> tuple[str, list[dict[str, str]], list[str], list[str], str]:
        parent_root = Path(parent.cwd).resolve()
        if spec.source == "inherit":
            return (
                str(parent_root),
                list(parent.messages),
                list(parent.active_skills),
                list(parent.active_capability_ids),
                "",
            )
        if spec.source == "fresh":
            child_root = self._authorized_child_root(parent_root, spec.cwd or ".")
            return (
                str(child_root),
                list(spec.messages),
                list(spec.active_skills),
                list(spec.active_capability_ids),
                "",
            )
        if not spec.image_id:
            raise ValueError("image_id is required for an image launch")
        image = self.image_store.load(spec.image_id)
        if image is None:
            raise KeyError(f"unknown session image: {spec.image_id}")
        child_root = self._authorized_child_root(parent_root, image.cwd)
        return (
            str(child_root),
            list(image.messages),
            list(image.active_skills),
            list(image.active_capability_ids),
            image.image_id,
        )

    def _authorized_child_root(self, parent_root: Path, requested_cwd: str) -> Path:
        candidate = Path(requested_cwd)
        if not candidate.is_absolute():
            candidate = parent_root / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(parent_root):
            raise ValueError("subagent cwd must remain within the parent workspace root")
        return candidate
