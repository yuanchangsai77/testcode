import json

from testcode.observability.logger import InMemoryLogger
from testcode.types import ExecutionSummary, ToolResult, UserRequest


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
