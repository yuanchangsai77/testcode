import http.client
import io
import urllib.error

import pytest

from testcode.app import create_model_client
from testcode.model.client import OpenAICompatibleModelClient, StubModelClient
from testcode.model.parser import ModelReplyParser
from testcode.model.prompt import ModelPromptBuilder
from testcode.model.types import ModelClientConfig
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


def test_post_json_wraps_remote_disconnect_as_runtime_error(monkeypatch):
    logger = InMemoryLogger()
    client = OpenAICompatibleModelClient(
        base_url="http://127.0.0.1:3000",
        timeout=1.5,
        logger=logger,
    )

    def fail_with_remote_disconnect(*_args, **_kwargs):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr("urllib.request.urlopen", fail_with_remote_disconnect)

    with pytest.raises(RuntimeError, match="Remote end closed connection without response"):
        client._post_json("http://127.0.0.1:3000/v1/chat/completions", {"messages": []})

    assert logger.events[-1].name == "model.network_error"
    assert logger.events[-1].payload["reason"] == "Remote end closed connection without response"


def test_post_json_wraps_http_and_url_errors(monkeypatch):
    logger = InMemoryLogger()
    client = OpenAICompatibleModelClient(base_url="http://127.0.0.1:3000", logger=logger)

    def fail_with_http_error(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            url="http://127.0.0.1:3000/v1/chat/completions",
            code=500,
            msg="server error",
            hdrs=None,
            fp=io.BytesIO(b"boom"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fail_with_http_error)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        client._post_json("http://127.0.0.1:3000/v1/chat/completions", {"messages": []})
    assert logger.events[-1].name == "model.http_error"

    def fail_with_url_error(*_args, **_kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fail_with_url_error)

    with pytest.raises(RuntimeError, match="connection refused"):
        client._post_json("http://127.0.0.1:3000/v1/chat/completions", {"messages": []})
    assert logger.events[-1].name == "model.network_error"


def test_post_json_rejects_invalid_json_and_missing_choices(monkeypatch):
    logger = InMemoryLogger()
    client = OpenAICompatibleModelClient(base_url="http://127.0.0.1:3000", logger=logger)

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.body

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse(b"not-json"))
    with pytest.raises(RuntimeError, match="not valid JSON"):
        client._post_json("http://127.0.0.1:3000/v1/chat/completions", {"messages": []})
    assert logger.events[-1].name == "model.invalid_json"

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse(b'{"id":"x"}'))
    with pytest.raises(RuntimeError, match="missing choices"):
        client._post_json("http://127.0.0.1:3000/v1/chat/completions", {"messages": []})
    assert logger.events[-1].name == "model.invalid_shape"


def test_create_model_client_reads_timeout_from_env(monkeypatch):
    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("TESTCODE_MODEL_TIMEOUT", "2.25")

    client = create_model_client(logger=None)

    assert isinstance(client, OpenAICompatibleModelClient)
    assert client.timeout == 2.25


def test_openai_client_accepts_config_object():
    config = ModelClientConfig(base_url="http://127.0.0.1:3000", model="custom-model", timeout=3.5)

    client = OpenAICompatibleModelClient(config=config)

    assert client.base_url == "http://127.0.0.1:3000"
    assert client.model == "custom-model"
    assert client.timeout == 3.5


def test_create_model_client_uses_stub_without_base_url(monkeypatch):
    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "")

    client = create_model_client(logger=None)

    assert isinstance(client, StubModelClient)


def test_build_messages_keeps_tool_definitions_in_stable_system_prefix():
    builder = ModelPromptBuilder()
    session = SessionContext(
        request=UserRequest(
            prompt="inspect tools",
            cwd="/repo",
            metadata={
                "conversation": [
                    {"role": "user", "content": "previous turn"},
                    {"role": "assistant", "content": "previous answer"},
                ]
            },
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

    messages = builder.build_messages(session)
    system = str(messages[0]["content"])
    first_history = messages[1]
    second_history = messages[2]
    user = str(messages[3]["content"])

    assert "Available tools:" in system
    assert "- patch: Apply a unified diff." in system
    assert "- read_file: Read a workspace file." in system
    assert "argument path: File path." in system
    assert first_history == {"role": "user", "content": "previous turn"}
    assert second_history == {"role": "assistant", "content": "previous answer"}
    assert "User request: inspect tools" in user
    assert "Session history:" in user
    assert "Available tools:" not in user
    assert "- read_file: Read a workspace file." not in user


def test_respond_sends_native_tool_schemas_and_parses_tool_calls(monkeypatch):
    client = OpenAICompatibleModelClient(base_url="http://127.0.0.1:3000")
    session = SessionContext(
        request=UserRequest(prompt="read README", cwd="/repo"),
        available_tools=[
            ToolDefinition(
                name="read_file",
                description="Read a workspace file.",
                arguments={"path": "File path."},
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                risk_level="read",
            )
        ],
    )
    captured = {}

    def fake_post_json(_url, payload):
        captured.update(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": "Reading the file.",
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "README.md"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }

    monkeypatch.setattr(client, "_post_json", fake_post_json)

    reply = client.respond(session)

    assert captured["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a workspace file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    assert reply.done is False
    assert reply.message == "Reading the file."
    assert reply.actions[0].name == "read_file"
    assert reply.actions[0].arguments == {"path": "README.md"}


def test_parse_response_rejects_unknown_native_tool_call():
    parser = ModelReplyParser()
    data = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": "missing_tool", "arguments": "{}"},
                        }
                    ],
                }
            }
        ]
    }

    with pytest.raises(RuntimeError, match="unknown tool: missing_tool"):
        parser.parse_response(data, allowed_tool_names={"read_file"})


def test_parse_response_rejects_invalid_native_tool_arguments():
    parser = ModelReplyParser()
    data = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "[]"},
                        }
                    ],
                }
            }
        ]
    }

    with pytest.raises(RuntimeError, match="arguments must decode to an object"):
        parser.parse_response(data, allowed_tool_names={"read_file"})


def test_parse_response_rejects_empty_content_without_tool_calls():
    parser = ModelReplyParser()
    data = {"choices": [{"message": {"content": ""}}]}

    with pytest.raises(RuntimeError, match="content was empty"):
        parser.parse_response(data, allowed_tool_names=set())


def test_parse_reply_converts_xmlish_content_tool_call():
    parser = ModelReplyParser()
    content = """<think>Need to search.</think>
- tool="shell_exec">
<parameter name="command">grep -r "sqlite" src tests --include="*.py"</parameter>
</invoke>
</minimax:tool_call>"""

    reply = parser.parse_reply(content, allowed_tool_names={"shell_exec"})

    assert reply.done is False
    assert reply.message == "Model requested tool calls."
    assert reply.metadata == {"thinking": "Need to search.", "cleaned_protocol_tags": True}
    assert len(reply.actions) == 1
    assert reply.actions[0].name == "shell_exec"
    assert reply.actions[0].arguments == {
        "command": 'grep -r "sqlite" src tests --include="*.py"'
    }


def test_parse_reply_cleans_thinking_tags_from_final_message():
    parser = ModelReplyParser()
    content = "<think>Need to inspect files first.</think>\n项目可以先保留 JSON。"

    reply = parser.parse_reply(content)

    assert reply.done is True
    assert reply.actions == []
    assert reply.message == "项目可以先保留 JSON。"
    assert reply.metadata == {
        "thinking": "Need to inspect files first.",
        "cleaned_protocol_tags": True,
    }


def test_parse_response_cleans_thinking_from_native_tool_call_message():
    parser = ModelReplyParser()
    data = {
        "choices": [
            {
                "message": {
                    "content": "<think>Need README.</think>Reading file.",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path": "README.md"}'},
                        }
                    ],
                }
            }
        ]
    }

    reply = parser.parse_response(data, allowed_tool_names={"read_file"})

    assert reply.message == "Reading file."
    assert reply.metadata == {"thinking": "Need README.", "cleaned_protocol_tags": True}
    assert reply.actions[0].name == "read_file"


def test_parse_reply_cleans_thinking_from_json_message():
    parser = ModelReplyParser()
    content = (
        '{"message":"<think>Need to inspect.</think>我会先看 README。",'
        '"done":true,"actions":[]}'
    )

    reply = parser.parse_reply(content)

    assert reply.done is True
    assert reply.actions == []
    assert reply.message == "我会先看 README。"
    assert reply.metadata == {"thinking": "Need to inspect.", "cleaned_protocol_tags": True}


def test_parse_reply_treats_invalid_embedded_json_as_final_message():
    logger = InMemoryLogger()
    parser = ModelReplyParser(logger=logger)

    reply = parser.parse_reply("工具说明如下：{name: read_file, arguments: {path: '.'}}")

    assert reply.done is True
    assert reply.actions == []
    assert "工具说明如下" in reply.message
    assert logger.events[-1].name == "model.invalid_reply_json"


def test_parse_reply_retries_invalid_json_shaped_response():
    logger = InMemoryLogger()
    parser = ModelReplyParser(logger=logger)
    content = (
        '{"message":"starting","done":false,'
        '"actions":[{"name":"find_files","parameter name="pattern">src/**/*.py"}]}'
    )

    reply = parser.parse_reply(content)

    assert reply.done is False
    assert reply.actions == []
    assert reply.message == (
        "Model response was invalid JSON. Return strict JSON with "
        "message, done, and actions fields only."
    )
    assert reply.metadata == {"invalid_reply_json": True}
    assert logger.events[-1].name == "model.invalid_reply_json"


def test_parse_reply_preserves_newlines_in_message():
    parser = ModelReplyParser()
    content = "Line 1.\n\nLine 2.\nLine 3."
    reply = parser.parse_reply(content)
    assert reply.message == "Line 1.\n\nLine 2.\nLine 3."
