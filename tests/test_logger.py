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
