import json

from testcode.observability.logger import InMemoryLogger
from testcode.types import ExecutionSummary, RuntimeBlocker, TaskCheckpoint, ToolResult, UserRequest


def test_logger_finalize_starts_run_and_writes_details_without_model_turns(tmp_path):
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    request = UserRequest(prompt="summarize", cwd=str(tmp_path), metadata={"source": "test"})
    summary = ExecutionSummary(
        final_message="done",
        tool_results=[ToolResult(name="read_file", success=True, output="content")],
    )

    logger.finalize(request, summary)

    assert logger.run_dir is None
    assert logger.last_run_id is not None
    run_dir = tmp_path / "runs" / logger.last_run_id
    assert (run_dir / "events.jsonl").exists()
    details = (run_dir / "details.log").read_text(encoding="utf-8")
    assert "- prompt: summarize" in details
    assert "- turns: 0" in details
    assert "- message: done" in details
    assert "read_file: ok" in details


def test_logger_details_falls_back_to_repr_for_unserializable_payload(tmp_path):
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    request = UserRequest(prompt="inspect", cwd=str(tmp_path))
    logger.start_run(request)
    logger.record("model.request", {"bad": object()})

    logger.finalize(request, ExecutionSummary(final_message="done", tool_results=[]))

    assert logger.last_run_id is not None
    details = (tmp_path / "runs" / logger.last_run_id / "details.log").read_text(encoding="utf-8")
    assert "'bad':" in details


def test_logger_counts_model_retries_as_one_semantic_turn(tmp_path):
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    request = UserRequest(prompt="inspect", cwd=str(tmp_path))
    logger.start_run(request)
    logger.record("model.request", {"attempt": 1})
    logger.record("model.retry", {"retry": 1})
    logger.record("model.request", {"attempt": 2})
    logger.record("model.response", {"content": "done"})
    logger.record("model.reply", {"turn": 1, "message": "done", "done": True, "actions": []})

    logger.finalize(request, ExecutionSummary(final_message="done", tool_results=[]))

    assert logger.last_run_summary is not None
    assert logger.last_run_summary.turn_count == 1


def test_logger_externalizes_large_event_values_to_run_artifact(tmp_path):
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    request = UserRequest(prompt="write report", cwd=str(tmp_path))
    logger.start_run(request)
    large_diff = "x" * 20_000

    logger.record("tool.execute", {"name": "patch", "arguments": {"diff": large_diff}})

    event = logger.events[-1]
    reference = event.payload["arguments"]["diff"]
    assert reference["$type"] == "artifact_ref"
    assert reference["schema_version"] == 1
    assert reference["chars"] == 20_000
    assert reference["sha256"]
    assert large_diff not in json.dumps(event.payload)
    assert __import__("pathlib").Path(reference["artifact_ref"]).exists()


def test_logger_reuses_content_addressed_artifact_for_repeated_large_values(tmp_path):
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    logger.start_run(UserRequest(prompt="write report", cwd=str(tmp_path)))
    large_value = "x" * 20_000

    logger.record("tool.execute", {"value": large_value})
    first = logger.events[-1].payload["value"]
    logger.record("tool.result", {"value": large_value})
    second = logger.events[-1].payload["value"]

    assert first["artifact_ref"] == second["artifact_ref"]
    assert len(list((logger.run_dir / "artifacts").iterdir())) == 1


def test_logger_does_not_copy_session_trace_into_run_events(tmp_path):
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    request = UserRequest(
        prompt="continue",
        cwd=str(tmp_path),
        metadata={"source": "test", "session_trace": ["historical-trace-sentinel"]},
    )

    logger.finalize(request, ExecutionSummary(final_message="done", tool_results=[]))

    events_path = tmp_path / "runs" / logger.last_run_id / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    start = next(event for event in events if event["name"] == "run.start")
    assert start["payload"]["metadata"] == {"source": "test"}
    assert "historical-trace-sentinel" not in events_path.read_text(encoding="utf-8")


def test_logger_redacts_sensitive_keys_and_token_like_values(tmp_path):
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    request = UserRequest(
        prompt="use token=sk-test123456789abcdef",
        cwd=str(tmp_path),
        metadata={"api_key": "plain-secret"},
    )
    summary = ExecutionSummary(
        final_message="done with ghp_1234567890abcdef",
        tool_results=[
            ToolResult(
                name="shell_exec",
                success=True,
                output="PASSWORD=hunter2\nstdout sk-test123456789abcdef",
                metadata={"token": "plain-token"},
            )
        ],
    )

    logger.finalize(request, summary)

    run_dir = tmp_path / "runs" / logger.last_run_id
    events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    details = (run_dir / "details.log").read_text(encoding="utf-8")
    combined = events + details
    assert "plain-secret" not in combined
    assert "plain-token" not in combined
    assert "hunter2" not in combined
    assert "sk-test123456789abcdef" not in combined
    assert "ghp_1234567890abcdef" not in combined
    assert "[REDACTED]" in combined


def test_logger_redacts_service_key_names(tmp_path):
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    request = UserRequest(
        prompt="configure service",
        cwd=str(tmp_path),
        metadata={
            "AMAP_WEB_SERVICE_KEY": "1234567890abcdef1234567890abcdef",
        },
    )
    logger.finalize(
        request,
        ExecutionSummary(
            final_message=(
                "AMAP_WEB_SERVICE_KEY="
                "1234567890abcdef1234567890abcdef"
            ),
            tool_results=[],
        ),
    )

    run_dir = tmp_path / "runs" / logger.last_run_id
    combined = (
        (run_dir / "events.jsonl").read_text(encoding="utf-8")
        + (run_dir / "details.log").read_text(encoding="utf-8")
    )
    assert "1234567890abcdef" not in combined
    assert "[REDACTED]" in combined


def test_logger_redacts_checkpoint_and_blocker_before_session_trace(tmp_path):
    secret = "sk-test123456789abcdef"
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    request = UserRequest(prompt="continue", cwd=str(tmp_path))
    summary = ExecutionSummary(
        final_message="blocked",
        tool_results=[],
        outcome="runtime_error",
        blockers=[RuntimeBlocker("runtime_error", f"token={secret}")],
        checkpoint=TaskCheckpoint(
            objective=f"use token={secret}",
            blockers=[RuntimeBlocker("runtime_error", f"token={secret}")],
        ),
    )

    logger.finalize(request, summary)

    trace = logger.last_run_summary
    assert trace is not None
    assert secret not in trace.checkpoint.objective
    assert secret not in trace.blockers[0].summary
    assert "[REDACTED]" in trace.checkpoint.objective
