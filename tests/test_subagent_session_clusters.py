import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from testcode.app import create_app
from testcode.orchestration.subagents import SubagentCoordinator, SubagentLaunchSpec
from testcode.orchestration.subagent_runner import SubagentRunner, _issue_subagent_grant
from testcode.sessions import SessionClusterStore, SessionImageStore, SessionStore
from testcode.types import ExecutionSummary, ModelReply, ToolResult, UserRequest
from testcode.tools.base import ToolContext
from testcode.tools.subagents import build_subagent_tools
from testcode.types import ToolAction


def build_coordinator(tmp_path):
    sessions = SessionStore(base_dir=tmp_path)
    clusters = SessionClusterStore(base_dir=tmp_path)
    images = SessionImageStore(base_dir=tmp_path)
    return sessions, clusters, images, SubagentCoordinator(sessions, clusters, images)


def test_parent_can_launch_multiple_independent_inherited_subagent_sessions(tmp_path):
    sessions, clusters, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(
        cwd=str(tmp_path),
        messages=[{"role": "user", "content": "build the runtime"}],
    )
    parent.active_capability_ids = ["toolbox:local"]
    sessions.save(parent)

    first = coordinator.launch_subagent(parent, SubagentLaunchSpec(task_summary="inspect storage"))
    second = coordinator.launch_subagent(parent, SubagentLaunchSpec(task_summary="inspect runtime"))

    assert first.session_id != second.session_id
    assert first.messages == parent.messages
    assert first.messages is not parent.messages
    assert first.active_capability_ids == ["toolbox:local"]
    assert first.parent_session_id == parent.session_id
    assert first.cluster_id == second.cluster_id == parent.cluster_id
    assert first.session_role == "subagent"
    assert first.launch_source == "inherit"
    cluster = clusters.load(parent.cluster_id)
    assert cluster is not None
    assert [member.role for member in cluster.members] == ["primary", "subagent", "subagent"]
    assert [member.task_summary for member in cluster.members[1:]] == ["inspect storage", "inspect runtime"]


def test_fresh_subagent_uses_explicit_config_without_parent_conversation(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(
        cwd=str(tmp_path),
        messages=[{"role": "user", "content": "private parent context"}],
    )

    child = coordinator.launch_subagent(
        parent,
        SubagentLaunchSpec(
            source="fresh",
            cwd=str(tmp_path / "child"),
            messages=[{"role": "system", "content": "review only"}],
            active_capability_ids=["skill:reviewer:instructions"],
        ),
    )

    assert child.cwd == str(tmp_path / "child")
    assert child.messages == [{"role": "system", "content": "review only"}]
    assert child.active_capability_ids == ["skill:reviewer:instructions"]
    assert "private parent context" not in json.dumps(child.messages)


def test_session_image_is_immutable_launch_material_not_a_live_session(tmp_path):
    sessions, _, images, coordinator = build_coordinator(tmp_path)
    template = sessions.create(
        cwd=str(tmp_path / "template"),
        messages=[{"role": "system", "content": "specialist template"}],
    )
    template.active_capability_ids = ["skill:specialist:instructions"]
    sessions.save(template)
    image = coordinator.save_image(template, name="specialist", description="Reusable specialist")
    parent = sessions.create(cwd=str(tmp_path))

    child = coordinator.launch_subagent(
        parent,
        SubagentLaunchSpec(source="image", image_id=image.image_id),
    )
    child.messages.append({"role": "user", "content": "new task"})
    sessions.save(child)

    reloaded_image = images.load(image.image_id)
    assert reloaded_image is not None
    assert reloaded_image.messages == [{"role": "system", "content": "specialist template"}]
    assert child.session_image_id == image.image_id
    assert child.launch_source == "image"
    assert child.cwd == str(tmp_path / "template")
    assert [item.image_id for item in images.list_images()] == [image.image_id]


def test_subagent_launch_cannot_expand_parent_workspace_root(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    parent_root = tmp_path / "parent"
    parent = sessions.create(cwd=str(parent_root))

    with pytest.raises(ValueError, match="parent workspace root"):
        coordinator.launch_subagent(
            parent,
            SubagentLaunchSpec(source="fresh", cwd=str(tmp_path / "sibling")),
        )

    template = sessions.create(cwd=str(tmp_path / "image-outside"))
    image = coordinator.save_image(template, name="outside")
    with pytest.raises(ValueError, match="parent workspace root"):
        coordinator.launch_subagent(
            parent,
            SubagentLaunchSpec(source="image", image_id=image.image_id),
        )


def test_fresh_subagent_resolves_relative_cwd_inside_parent_workspace(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(cwd=str(tmp_path))

    child = coordinator.launch_subagent(
        parent,
        SubagentLaunchSpec(source="fresh", cwd="nested/worktree"),
    )

    assert child.cwd == str(tmp_path / "nested" / "worktree")


def test_public_state_is_structured_versioned_and_member_only(tmp_path):
    sessions, clusters, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(cwd=str(tmp_path))
    child = coordinator.launch_subagent(parent, SubagentLaunchSpec())

    entry = coordinator.publish_state(
        child,
        "finding",
        "The session store has a stable persistence boundary.",
        artifact_ref="artifacts/storage-report.json",
        metadata={"confidence": "high"},
    )
    coordinator.update_member_state(child, "completed", task_summary="storage inspection complete")

    snapshot = coordinator.snapshot(parent)
    assert entry.revision == 2
    assert snapshot.revision == 3
    assert snapshot.shared_state[0].author_session_id == child.session_id
    assert snapshot.shared_state[0].kind == "finding"
    assert snapshot.members[1].state == "completed"
    with pytest.raises(ValueError, match="only cluster members"):
        clusters.publish(parent.cluster_id, "unknown-session", "status", "hello")
    with pytest.raises(ValueError, match="unsupported public state kind"):
        coordinator.publish_state(child, "message", "direct chat is not supported")
    with pytest.raises(ValueError, match="safe relative path"):
        coordinator.publish_state(child, "artifact", "bad ref", artifact_ref="../secret")


def test_concurrent_public_state_updates_do_not_overwrite_each_other(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(cwd=str(tmp_path))
    child = coordinator.launch_subagent(parent, SubagentLaunchSpec())

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda index: coordinator.publish_state(child, "status", f"update {index}"),
                range(12),
            )
        )

    snapshot = coordinator.snapshot(parent)
    assert len(snapshot.shared_state) == 12
    assert len({entry.entry_id for entry in snapshot.shared_state}) == 12
    assert sorted(entry.revision for entry in snapshot.shared_state) == list(range(2, 14))


def test_legacy_session_loads_with_primary_direct_defaults(tmp_path):
    sessions = SessionStore(base_dir=tmp_path)
    sessions.base_dir.mkdir(parents=True)
    (sessions.base_dir / "legacy.json").write_text(
        json.dumps(
            {
                "session_id": "legacy",
                "cwd": str(tmp_path),
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "messages": [],
            }
        ),
        encoding="utf-8",
    )

    loaded = sessions.load("legacy")
    assert loaded is not None
    assert loaded.cluster_id == ""
    assert loaded.parent_session_id == ""
    assert loaded.session_role == "primary"
    assert loaded.launch_source == "direct"


def test_stale_parent_save_preserves_cluster_relationship_created_by_runtime(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    stale_parent = sessions.create(cwd=str(tmp_path))
    runtime_parent = sessions.load(stale_parent.session_id)
    assert runtime_parent is not None

    coordinator.launch_subagent(runtime_parent, SubagentLaunchSpec(task_summary="inspect"))
    stale_parent.messages.append({"role": "assistant", "content": "delegated"})
    sessions.save(stale_parent)

    reloaded = sessions.load(stale_parent.session_id)
    assert reloaded is not None
    assert reloaded.cluster_id == runtime_parent.cluster_id
    assert coordinator.snapshot(reloaded).members[1].state == "ready"


def test_runner_executes_ready_children_and_reports_results_to_public_state(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(cwd=str(tmp_path))
    first = coordinator.launch_subagent(parent, SubagentLaunchSpec(task_summary="inspect storage"))
    second = coordinator.launch_subagent(parent, SubagentLaunchSpec(task_summary="inspect runtime"))
    seen_requests = []

    class Runtime:
        def run_background(self, request):
            seen_requests.append(request)
            return ExecutionSummary(
                final_message=f"completed {request.prompt}",
                tool_results=[ToolResult("read_file", True, "ok")],
            )

        def persist_run(self, session, prompt, summary, **_kwargs):
            session.messages.extend(
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": summary.final_message},
                ]
            )
            session.status = "completed"
            sessions.save(session)

    runner = SubagentRunner(coordinator, lambda _session, _grant: Runtime(), max_workers=2)
    results = runner.run_ready(parent)

    assert {result.session_id for result in results} == {first.session_id, second.session_id}
    assert {result.state for result in results} == {"completed"}
    assert {request.prompt for request in seen_requests} == {"inspect storage", "inspect runtime"}
    assert all(request.metadata["subagent"]["role"] == "subagent" for request in seen_requests)
    snapshot = coordinator.snapshot(parent)
    assert [member.state for member in snapshot.members[1:]] == ["completed", "completed"]
    assert len(snapshot.shared_state) == 2
    assert {entry.metadata["outcome"] for entry in snapshot.shared_state} == {"completed"}
    assert sessions.load(first.session_id).messages[-1]["content"] == "completed inspect storage"


def test_runner_marks_failed_execution_and_publishes_blocker(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(cwd=str(tmp_path))
    child = coordinator.launch_subagent(parent, SubagentLaunchSpec(task_summary="fail safely"))

    class FailingRuntime:
        def run_background(self, _request):
            raise RuntimeError("model unavailable")

        def persist_run(self, *_args, **_kwargs):
            raise AssertionError("failed execution must not be persisted as completed")

    result = SubagentRunner(coordinator, lambda _session, _grant: FailingRuntime()).run_ready(parent)[0]

    assert result.state == "failed"
    snapshot = coordinator.snapshot(parent)
    assert snapshot.members[1].state == "failed"
    assert snapshot.shared_state[0].kind == "blocker"
    assert snapshot.shared_state[0].summary == "model unavailable"
    assert sessions.load(child.session_id).status == "failed"


def test_runner_publishes_blocker_when_claimed_session_record_is_missing(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(cwd=str(tmp_path))
    child = coordinator.launch_subagent(parent, SubagentLaunchSpec(task_summary="inspect"))
    (sessions.base_dir / f"{child.session_id}.json").unlink()

    class NeverRuntime:
        def run_background(self, _request):
            raise AssertionError("missing session must not start a runtime")

    result = SubagentRunner(coordinator, lambda _session, _grant: NeverRuntime()).run_ready(parent)[0]
    snapshot = coordinator.snapshot(parent)

    assert result.state == "failed"
    assert snapshot.members[1].state == "failed"
    assert snapshot.shared_state[-1].summary == "Subagent session record is missing."


def test_runner_preserves_blocked_execution_as_blocked(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(cwd=str(tmp_path))
    child = coordinator.launch_subagent(parent, SubagentLaunchSpec(task_summary="write safely"))

    class BlockedRuntime:
        def run_background(self, _request):
            return ExecutionSummary(
                final_message="write requires unavailable approval",
                tool_results=[ToolResult("shell_exec", False, "approval needed", "approval_required")],
                outcome="blocked",
            )

        def persist_run(self, session, _prompt, _summary, *, status, **_kwargs):
            session.status = status
            sessions.save(session)

    result = SubagentRunner(coordinator, lambda _session, _grant: BlockedRuntime()).run_ready(parent)[0]

    assert result.state == "blocked"
    snapshot = coordinator.snapshot(parent)
    assert snapshot.members[1].state == "blocked"
    assert snapshot.shared_state[0].kind == "blocker"
    assert snapshot.shared_state[0].metadata["outcome"] == "blocked"
    assert sessions.load(child.session_id).status == "blocked"


def test_runner_does_not_execute_an_already_claimed_child_twice(tmp_path):
    sessions, clusters, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(cwd=str(tmp_path))
    child = coordinator.launch_subagent(parent, SubagentLaunchSpec(task_summary="once"))
    assert clusters.claim_ready_member(parent.cluster_id, child.session_id) is True
    assert clusters.claim_ready_member(parent.cluster_id, child.session_id) is False

    class NeverRuntime:
        def run_background(self, _request):
            raise AssertionError("claimed child must not execute again")

    runner = SubagentRunner(coordinator, lambda _session, _grant: NeverRuntime())
    assert runner.run_ready(parent) == []


def test_parent_resumes_same_child_with_persisted_context_and_new_attempt(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(cwd=str(tmp_path))
    child = coordinator.launch_subagent(parent, SubagentLaunchSpec(task_summary="implement counter"))
    child.messages.extend(
        [
            {"role": "user", "content": "implement counter"},
            {"role": "assistant", "content": "initial implementation complete"},
        ]
    )
    child.status = "completed"
    sessions.save(child)
    coordinator.update_member_state(child, "completed")

    resumed = coordinator.resume_subagent(parent, child.session_id, "fix click handler")

    assert resumed.session_id == child.session_id
    assert resumed.messages[-1]["content"] == "initial implementation complete"
    member = coordinator.snapshot(parent).members[1]
    assert member.state == "ready"
    assert member.task_summary == "fix click handler"
    assert member.attempt == 2


def test_runner_rejects_irrelevant_completion_and_requests_same_session_resume(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(cwd=str(tmp_path))
    child = coordinator.launch_subagent(
        parent,
        SubagentLaunchSpec(task_summary="修复 click-game/index.html"),
    )

    class Runtime:
        def run_background(self, _request):
            return ExecutionSummary(
                "I cannot use subagent_spawn or subagent_run_ready from this child.",
                [ToolResult("patch", True, "applied")],
            )

        def persist_run(self, session, _prompt, summary, *, status, **_kwargs):
            session.status = status
            sessions.save(session)

    result = SubagentRunner(coordinator, lambda _session, _grant: Runtime()).run_ready(parent)[0]

    assert result.session_id == child.session_id
    assert result.state == "blocked"
    assert "did not address the delegated task" in result.final_message


def test_runner_cancellation_marks_active_child_cancelled_and_late_result_cannot_win(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(cwd=str(tmp_path))
    child = coordinator.launch_subagent(parent, SubagentLaunchSpec(task_summary="inspect runtime"))
    started = Event()
    release = Event()

    class Engine:
        def cancel_current_run(self):
            release.set()

    class Runtime:
        engine = Engine()

        def run_background(self, _request):
            started.set()
            release.wait(2)
            return ExecutionSummary("late completion", [ToolResult("read_file", True, "ok")])

        def persist_run(self, *_args, **_kwargs):
            raise AssertionError("a cancelled late result must not be persisted")

    runner = SubagentRunner(coordinator, lambda _session, _grant: Runtime())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner.run_ready, parent)
        assert started.wait(1)
        runner.cancel_running(parent.cluster_id, [child.session_id])
        result = future.result(timeout=3)[0]

    assert result.state == "cancelled"
    assert coordinator.snapshot(parent).members[1].state == "cancelled"
    assert sessions.load(child.session_id).status == "cancelled"


def test_cancellation_wins_before_attempt_terminal_commit(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(cwd=str(tmp_path))
    child = coordinator.launch_subagent(parent, SubagentLaunchSpec(task_summary="inspect runtime"))
    completion_waiting = Event()
    release_completion = Event()
    original_finish = coordinator.finish_attempt

    def controlled_finish(session, attempt, state, kind, summary, **kwargs):
        if state != "cancelled":
            completion_waiting.set()
            release_completion.wait(2)
        return original_finish(session, attempt, state, kind, summary, **kwargs)

    coordinator.finish_attempt = controlled_finish

    class Runtime:
        def run_background(self, _request):
            return ExecutionSummary("late completion", [ToolResult("read_file", True, "ok")])

        def persist_run(self, *_args, **_kwargs):
            raise AssertionError("superseded completion must not be persisted")

    runner = SubagentRunner(coordinator, lambda _session, _grant: Runtime())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner.run_ready, parent)
        assert completion_waiting.wait(1)
        runner.cancel_running(parent.cluster_id, [child.session_id])
        release_completion.set()
        result = future.result(timeout=3)[0]

    assert result.state == "cancelled"
    assert coordinator.snapshot(parent).members[1].state == "cancelled"
    assert sessions.load(child.session_id).status == "cancelled"


def test_model_visible_tools_complete_spawn_run_and_status_flow(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(cwd=str(tmp_path))

    class Runtime:
        def run_background(self, request):
            return ExecutionSummary(f"result for {request.prompt}", [])

        def persist_run(self, session, prompt, summary, **_kwargs):
            session.messages.extend(
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": summary.final_message},
                ]
            )
            sessions.save(session)

    runner = SubagentRunner(coordinator, lambda _session, _grant: Runtime())
    context = ToolContext(
        cwd=str(tmp_path),
        state={
            "active_session_id": parent.session_id,
            "subagent_coordinator": coordinator,
            "subagent_runner": runner,
        },
    )
    tools = {tool.name: tool for tool in build_subagent_tools()}

    spawned = tools["subagent_spawn"].run(
        ToolAction("subagent_spawn", {"task": "review persistence", "source": "fresh"}),
        context,
    )
    executed = tools["subagent_run_ready"].run(ToolAction("subagent_run_ready", {}), context)
    child_id = json.loads(spawned.output)["session_id"]
    resumed = tools["subagent_resume"].run(
        ToolAction("subagent_resume", {"session_id": child_id, "task": "review persistence again"}),
        context,
    )
    status = tools["subagent_status"].run(ToolAction("subagent_status", {}), context)

    assert spawned.success is True
    assert executed.success is True
    assert resumed.success is True
    assert json.loads(resumed.output)["attempt"] == 2
    assert json.loads(executed.output)[0]["state"] == "completed"
    status_payload = json.loads(status.output)
    assert status_payload["members"][1]["state"] == "ready"
    assert status_payload["public_state"][0]["summary"] == "result for review persistence"


def test_runner_publishes_structured_handoff_evidence(tmp_path):
    sessions, _, _, coordinator = build_coordinator(tmp_path)
    parent = sessions.create(cwd=str(tmp_path))
    coordinator.launch_subagent(parent, SubagentLaunchSpec(task_summary="update report"))

    class Runtime:
        def run_background(self, _request):
            return ExecutionSummary(
                "updated and verified",
                [
                    ToolResult("patch", True, "applied", metadata={"changed_files": ["report.md"]}),
                    ToolResult(
                        "run_tests",
                        True,
                        "passed",
                        metadata={"command": "pytest -q", "duration_seconds": 1.25},
                    ),
                ],
            )

        def persist_run(self, session, _prompt, _summary, *, status, **_kwargs):
            session.status = status
            sessions.save(session)

    result = SubagentRunner(coordinator, lambda _session, _grant: Runtime()).run_ready(parent)[0]
    entry = coordinator.snapshot(parent).shared_state[-1]

    assert result.changed_files == ["report.md"]
    assert result.verifications == [
        {
            "tool": "run_tests",
            "success": True,
            "error_code": "",
            "command": "pytest -q",
            "duration_seconds": 1.25,
        }
    ]
    assert entry.metadata["changed_files"] == ["report.md"]
    assert entry.metadata["verifications"] == result.verifications


def test_application_exposes_subagent_lifecycle_tools_on_demand(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "")

    app = create_app(workspace_root=tmp_path)
    warehouse = app.engine.capability_warehouse
    names = {definition.name for definition in app.engine.tools.definitions()}

    assert not {"subagent_spawn", "subagent_resume", "subagent_run_ready", "subagent_status"} & names
    assert any(entry.id == "local:subagents" for entry in warehouse.catalog_entries())
    manifest = warehouse.open_toolbox("local:subagents")
    warehouse.activate([item.id for item in manifest.items], scope="run", reason="delegate work")
    names = {definition.name for definition in app.engine.tools.definitions()}
    assert {"subagent_spawn", "subagent_resume", "subagent_run_ready", "subagent_status"} <= names
    assert app.subagent_runner is not None


def test_background_mode_alone_does_not_grant_patch_and_hides_recursive_tools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "")

    app = create_app(workspace_root=tmp_path, mode="confirm", background=True)
    names = {definition.name for definition in app.engine.tools.definitions()}

    assert app.engine.guardrails.policy.mode == "confirm"
    assert "patch" in names
    assert "subagent_spawn" not in names
    assert "subagent_resume" not in names
    assert "subagent_run_ready" not in names
    assert not any(entry.id == "local:subagents" for entry in app.engine.capability_warehouse.catalog_entries())


def test_background_subagent_runtime_uses_bounded_model_retry_and_timeout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("TESTCODE_MODEL_TIMEOUT", "60")

    app = create_app(workspace_root=tmp_path, background=True)

    assert app.engine.max_model_retries == 1
    assert app.engine.model.timeout == 30.0


def test_background_subagent_runtime_applies_structured_workspace_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "")
    grant = _issue_subagent_grant(
        cluster_id="cluster-test",
        session_id="child-test",
        parent_session_id="parent-test",
        attempt=2,
        workspace_root=str(tmp_path),
    )
    app = create_app(
        workspace_root=tmp_path,
        mode="confirm",
        background=True,
        subagent_grant=grant,
    )

    class PatchModel:
        def respond(self, _session):
            return ModelReply(
                message="created delegated artifact",
                actions=[
                    ToolAction(
                        "patch",
                        {
                            "diff": (
                                "--- /dev/null\n"
                                "+++ b/delegated.txt\n"
                                "@@ -0,0 +1 @@\n"
                                "+completed by child\n"
                            )
                        },
                    )
                ],
                done=True,
            )

    app.engine.model = PatchModel()
    summary = app.run_background(
        UserRequest(
            prompt="create delegated artifact",
            cwd=str(tmp_path),
            metadata={
                "session_id": "child-test",
                "subagent": {
                    "role": "subagent",
                    "cluster_id": "cluster-test",
                    "parent_session_id": "parent-test",
                    "attempt": 2,
                },
            },
        )
    )

    assert summary.outcome == "completed"
    assert (tmp_path / "delegated.txt").read_text(encoding="utf-8") == "completed by child\n"


def test_delegated_runtime_rejects_request_outside_issued_identity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "")
    grant = _issue_subagent_grant(
        cluster_id="cluster-test",
        session_id="child-test",
        parent_session_id="parent-test",
        attempt=1,
        workspace_root=str(tmp_path),
    )
    app = create_app(workspace_root=tmp_path, background=True, subagent_grant=grant)

    with pytest.raises(RuntimeError, match="does not match"):
        app.run_background(
            UserRequest(
                prompt="inspect",
                cwd=str(tmp_path),
                metadata={
                    "session_id": "different-child",
                    "subagent": {
                        "role": "subagent",
                        "cluster_id": "cluster-test",
                        "parent_session_id": "parent-test",
                        "attempt": 1,
                    },
                },
            )
        )
