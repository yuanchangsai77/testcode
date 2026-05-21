import pytest

from testcode.app import create_model_client
from testcode.model.client import OpenAICompatibleModelClient, StubModelClient
from testcode.observability.logger import InMemoryLogger
from testcode.orchestration.session import SessionContext
from testcode.types import ToolDefinition, UserRequest


def test_post_json_wraps_timeout_as_runtime_error(monkeypatch):
    logger = InMemoryLogger()
    client = OpenAICompatibleModelClient(
        base_url="http://127.0.0.1:3000",
        timeout=1.5,
        logger=logger,
    )

    def fail_with_timeout(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", fail_with_timeout)

    with pytest.raises(RuntimeError, match="timed out after 1.5 seconds"):
        client._post_json("http://127.0.0.1:3000/v1/chat/completions", {"messages": []})

    assert logger.events[-1].name == "model.timeout"
    assert logger.events[-1].payload["timeout"] == 1.5


def test_create_model_client_reads_timeout_from_env(monkeypatch):
    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("TESTCODE_MODEL_TIMEOUT", "2.25")

    client = create_model_client(logger=None)

    assert isinstance(client, OpenAICompatibleModelClient)
    assert client.timeout == 2.25


def test_create_model_client_uses_stub_without_base_url(monkeypatch):
    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "")

    client = create_model_client(logger=None)

    assert isinstance(client, StubModelClient)


def test_build_messages_keeps_tool_definitions_in_stable_system_prefix():
    client = OpenAICompatibleModelClient(base_url="http://127.0.0.1:3000")
    session = SessionContext(
        request=UserRequest(
            prompt="inspect tools",
            cwd="/repo",
            metadata={"conversation": [{"role": "user", "content": "previous turn"}]},
        ),
        available_tools=[
            ToolDefinition(
                name="read_file",
                description="Read a workspace file.",
                arguments={"path": "File path."},
                risk_level="read",
            ),
            ToolDefinition(
                name="patch",
                description="Apply a unified diff.",
                arguments={"diff": "Unified diff text."},
                risk_level="write",
            ),
        ],
        history=["tool:read_file:ok: content"],
    )

    messages = client._build_messages(session)
    system = str(messages[0]["content"])
    user = str(messages[1]["content"])

    assert "Available tools:" in system
    assert "- patch: Apply a unified diff." in system
    assert "- read_file: Read a workspace file." in system
    assert "argument path: File path." in system
    assert "User request: inspect tools" in user
    assert "Conversation history:" in user
    assert "Session history:" in user
    assert "Available tools:" not in user
    assert "- read_file: Read a workspace file." not in user


def test_parse_reply_treats_invalid_embedded_json_as_final_message():
    logger = InMemoryLogger()
    client = OpenAICompatibleModelClient(base_url="http://127.0.0.1:3000", logger=logger)

    reply = client._parse_reply("工具说明如下：{name: read_file, arguments: {path: '.'}}")

    assert reply.done is True
    assert reply.actions == []
    assert "工具说明如下" in reply.message
    assert logger.events[-1].name == "model.invalid_reply_json"
