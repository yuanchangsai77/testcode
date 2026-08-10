from testcode.model.prompt import ModelPromptBuilder
from testcode.observability.logger import InMemoryLogger
from testcode.orchestration.session import SessionContext
from testcode.tools.base import SimpleTool, ToolContext
from testcode.tools.registry import ToolRegistry
from testcode.types import ToolAction, ToolResult, UserRequest


def test_tool_result_contract_for_model_context():
    session = SessionContext(request=UserRequest(prompt="inspect", cwd="/repo"))
    session.add_tool_result(
        ToolResult(
            name="demo_tool",
            success=True,
            output="model-visible output",
            metadata={
                "internal_value": "metadata-hidden-from-model",
                "action_arguments": {"path": "visible-argument.txt"},
            },
        )
    )

    messages = ModelPromptBuilder().build_messages(session)
    prompt = "\n".join(str(message["content"]) for message in messages)

    assert "model-visible output" in prompt
    assert "visible-argument.txt" in prompt
    assert "metadata-hidden-from-model" not in prompt


def test_tool_summarizer_is_display_only():
    tool = SimpleTool(
        name="demo_tool",
        description="Demo.",
        arguments={},
        handler=lambda _action, _context: ToolResult(
            name="demo_tool",
            success=True,
            output="model-visible output",
            metadata={"internal_value": "runtime-only"},
        ),
        summarizer=lambda _result: "user-visible summary",
    )

    result = tool.run(ToolAction(name="demo_tool"), context=ToolContext(cwd="/repo"))

    assert result.output == "model-visible output"
    assert result.metadata == {"internal_value": "runtime-only"}
    assert tool.summarize(result) == "user-visible summary"
    assert "summary" not in result.metadata


def test_registry_packages_every_tool_result_before_history():
    registry = ToolRegistry(InMemoryLogger(), max_output_bytes=24)
    registry.register(
        SimpleTool(
            name="verbose",
            description="Verbose.",
            arguments={},
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda _action, _context: ToolResult("verbose", True, "x" * 200),
        )
    )

    result = registry.execute(ToolAction("verbose"), cwd="/repo")

    assert len(result.output.encode("utf-8")) <= 24
    assert result.metadata["truncated"] is True
    assert result.metadata["output_original_bytes"] == 200
    assert result.metadata["output_visible_bytes"] <= 24
    assert len(result.metadata["output_sha256"]) == 64
