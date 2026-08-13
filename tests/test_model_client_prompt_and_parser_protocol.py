import http.client
import io
import urllib.error
import urllib.request

import pytest

from testcode.app import create_model_client
from testcode.model.client import OpenAICompatibleModelClient, StubModelClient
from testcode.model.parser import ModelReplyParser
from testcode.model.prompt import ModelPromptBuilder
from testcode.context.packager import ContextPackager, ContextSegment
from testcode.model.types import (
    ModelClientConfig,
    ModelConnectionError,
    ModelServiceError,
    ModelTimeoutError,
)
from testcode.observability.logger import InMemoryLogger
from testcode.orchestration.session import SessionContext
from testcode.types import SessionResumeState, SessionRunTrace, SessionTurnTrace, ToolDefinition, UserRequest


def test_post_json_wraps_timeout_as_runtime_error(monkeypatch):
    logger = InMemoryLogger()
    client = OpenAICompatibleModelClient(
        base_url="http://127.0.0.1:3000",
        timeout=1.5,
        logger=logger,
    )

    def fail_with_timeout(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(client, "_open_url", fail_with_timeout)

    with pytest.raises(ModelTimeoutError, match="timed out after 1.5 seconds"):
        client._post_json("http://127.0.0.1:3000/v1/chat/completions", {"messages": []})

    assert logger.events[-1].name == "model.timeout"
    assert logger.events[-1].payload["timeout"] == 1.5


def test_post_json_recognizes_timeout_wrapped_by_url_error(monkeypatch):
    client = OpenAICompatibleModelClient(
        base_url="http://127.0.0.1:3000",
        timeout=1.5,
    )

    monkeypatch.setattr(
        client,
        "_open_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError(TimeoutError("timed out"))),
    )

    with pytest.raises(ModelTimeoutError, match="timed out after 1.5 seconds"):
        client._post_json("http://127.0.0.1:3000/v1/chat/completions", {"messages": []})


def test_post_json_wraps_remote_disconnect_as_runtime_error(monkeypatch):
    logger = InMemoryLogger()
    client = OpenAICompatibleModelClient(
        base_url="http://127.0.0.1:3000",
        timeout=1.5,
        logger=logger,
    )

    def fail_with_remote_disconnect(*_args, **_kwargs):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr(client, "_open_url", fail_with_remote_disconnect)

    with pytest.raises(ModelConnectionError, match="Remote end closed connection without response"):
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

    monkeypatch.setattr(client, "_open_url", fail_with_http_error)

    with pytest.raises(ModelServiceError, match="HTTP 500"):
        client._post_json("http://127.0.0.1:3000/v1/chat/completions", {"messages": []})
    assert logger.events[-1].name == "model.http_error"

    def fail_with_url_error(*_args, **_kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(client, "_open_url", fail_with_url_error)

    with pytest.raises(ModelConnectionError, match="connection refused"):
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

    monkeypatch.setattr(client, "_open_url", lambda *_args, **_kwargs: FakeResponse(b"not-json"))
    with pytest.raises(RuntimeError, match="not valid JSON"):
        client._post_json("http://127.0.0.1:3000/v1/chat/completions", {"messages": []})
    assert logger.events[-1].name == "model.invalid_json"

    monkeypatch.setattr(client, "_open_url", lambda *_args, **_kwargs: FakeResponse(b'{"id":"x"}'))
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


def test_loopback_model_requests_use_proxy_free_opener(monkeypatch):
    client = OpenAICompatibleModelClient(base_url="http://127.0.0.1:3000")
    marker = object()
    direct_calls = []

    monkeypatch.setattr(
        client._direct_opener,
        "open",
        lambda request, timeout: direct_calls.append((request.full_url, timeout)) or marker,
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("loopback used proxy-aware urlopen")),
    )

    request = urllib.request.Request("http://127.0.0.1:3000/v1/chat/completions")
    assert client._open_url(request, timeout=4) is marker
    assert direct_calls == [(request.full_url, 4)]


def test_response_read_enforces_total_deadline(monkeypatch):
    client = OpenAICompatibleModelClient(base_url="http://127.0.0.1:3000", timeout=1)

    class SlowResponse:
        def read1(self, _size):
            return b"x"

    times = iter([0.0, 0.6, 1.1])
    monkeypatch.setattr("testcode.model.client.time.monotonic", lambda: next(times))

    with pytest.raises(TimeoutError, match="deadline exceeded"):
        client._read_response(SlowResponse(), deadline=1.0)


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
    assert "Recent current-run history:" in user
    assert "Available tools:" not in user
    assert "- read_file: Read a workspace file." not in user


def test_context_packager_preserves_current_request_and_latest_conversation_within_budget():
    packager = ContextPackager(max_chars=4_000)
    conversation = [
        {"role": "user", "content": f"old-{index}-" + ("x" * 700)}
        for index in range(8)
    ]

    messages = packager.package(
        "system rules\n" + ("s" * 1_000),
        conversation,
        "Current working directory: /repo\nUser request: create report\n" + ("h" * 2_000),
    )

    total = sum(len(str(item["content"])) for item in messages)
    assert total <= 4_000
    assert "User request: create report" in messages[-1]["content"]
    assert any("old-7-" in str(item["content"]) for item in messages)
    assert packager.last_stats.omitted_messages > 0


def test_context_packager_prioritizes_required_atomic_sections_over_large_optional_context():
    packager = ContextPackager(max_chars=4_000)

    messages = packager.package_segments(
        [
            ContextSegment("MANDATORY PROTOCOL", "protocol", priority=100, required=True),
            ContextSegment("MANDATORY SECURITY", "security", priority=100, required=True),
            ContextSegment("optional\n" * 2_000, "optional instructions", priority=10),
        ],
        [],
        [ContextSegment("User request: repair runtime", "current request", priority=100, required=True)],
    )

    system = str(messages[0]["content"])
    assert "MANDATORY PROTOCOL" in system
    assert "MANDATORY SECURITY" in system
    assert "User request: repair runtime" in str(messages[-1]["content"])
    assert sum(len(str(item["content"])) for item in messages) <= 4_000


def test_context_packager_reserves_space_for_each_required_segment_when_they_overflow():
    packager = ContextPackager(max_chars=4_000)

    messages = packager.package_segments(
        [ContextSegment("PROTOCOL\n" + ("p\n" * 2_000), "protocol", required=True)],
        [],
        [
            ContextSegment("REQUEST\n" + ("r\n" * 2_000), "request", required=True),
            ContextSegment("CHECKPOINT\n" + ("c\n" * 2_000), "checkpoint", required=True),
        ],
    )

    assert "PROTOCOL" in str(messages[0]["content"])
    assert "REQUEST" in str(messages[-1]["content"])
    assert "CHECKPOINT" in str(messages[-1]["content"])
    assert sum(len(str(item["content"])) for item in messages) <= 4_000


def test_model_client_projects_capability_profile_and_context_stats(monkeypatch):
    logger = InMemoryLogger()
    client = OpenAICompatibleModelClient(
        base_url="http://127.0.0.1:3000",
        logger=logger,
        context_budget_chars=4_000,
    )
    monkeypatch.setattr(
        client,
        "_post_json",
        lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": '{"message":"done","done":true,"actions":[]}'}}]
        },
    )
    session = SessionContext(request=UserRequest(prompt="hello", cwd="/repo"))

    client.respond(session)

    profile = session.request.metadata["model_capability_profile"]
    request_event = next(event for event in logger.events if event.name == "model.request")
    assert profile["structured_output_mode"] == "prompt_json"
    assert profile["context_budget_chars"] == 4_000
    assert request_event.payload["context_package"]["budget_chars"] == 4_000


def test_build_messages_marks_delegated_subagent_and_prioritizes_current_task():
    session = SessionContext(
        request=UserRequest(
            prompt="implement the delegated page",
            cwd="/repo",
            metadata={
                "conversation": [{"role": "user", "content": "spawn another agent"}],
                "subagent": {"role": "subagent", "parent_session_id": "parent"},
            },
        )
    )

    system = str(ModelPromptBuilder().build_messages(session)[0]["content"])

    assert "You are already a subagent" in system
    assert "current delegated request overrides inherited conversational intent" in system
    assert "Do not create or run another subagent" in system


def test_build_messages_explains_staged_warehouse_without_exposing_contents():
    session = SessionContext(request=UserRequest(prompt="使用 MCP 查询路线", cwd="/repo"))

    system = str(ModelPromptBuilder().build_messages(session)[0]["content"])

    assert "warehouse_list" in system
    assert "The warehouse contents are not shown by default" in system
    assert "mcp:amap" not in system
    assert "Do not inspect project source, config files, or environment variables" in system
    assert "Never infer that credentials, transports, or integrations are missing" in system


def test_build_messages_includes_non_overridable_security_baseline():
    session = SessionContext(request=UserRequest(prompt="create an app", cwd="/repo"))

    system = str(ModelPromptBuilder().build_messages(session)[0]["content"])

    assert "[SEC-CREDENTIAL-001]" in system
    assert "environment variables or a protected secret store" in system
    assert "[SEC-CREDENTIAL-002]" in system
    assert ".env.example" in system
    assert "[SEC-CLIENT-001]" in system
    assert "browser-delivered" in system
    assert "[PY-PACKAGE-001]" in system
    assert "distribution project name may contain hyphens" in system
    assert "mandatory baseline" in system
    assert "Do not encode, split, rename" in system


def test_build_messages_includes_recent_session_trace_summary():
    builder = ModelPromptBuilder()
    session = SessionContext(
        request=UserRequest(
            prompt="continue",
            cwd="/repo",
            metadata={
                "session_trace": [
                    SessionRunTrace(
                        run_id="run-1",
                        started_at="2026-07-10T02:00:00Z",
                        completed_at="2026-07-10T02:00:05Z",
                        prompt="add amap config",
                        final_message="config written successfully",
                        outcome="completed",
                        event_count=12,
                        turn_count=2,
                        tool_names=["read_file", "patch"],
                        turns=[
                            SessionTurnTrace(
                                turn=1,
                                message="inspecting config",
                                actions=["read_file"],
                                tool_results=["read_file:ok"],
                            )
                        ],
                    )
                ]
            },
        ),
        available_tools=[],
    )

    messages = builder.build_messages(session)
    user = str(messages[-1]["content"])

    assert "Session trace summary:" in user
    assert "run run-1 | outcome=completed | prompt=add amap config" in user
    assert "- tools: read_file, patch" in user
    assert "- final: config written successfully" in user


def test_build_messages_includes_resume_state():
    builder = ModelPromptBuilder()
    session = SessionContext(
        request=UserRequest(
            prompt="continue",
            cwd="/repo",
            metadata={
                "resume_state": SessionResumeState(
                    last_run_id="run-2",
                    last_outcome="runtime_error",
                    last_tool_names=["read_file", "run_tests"],
                    open_issue="Model API is unavailable right now. timed out.",
                    recovery_hint="Reuse earlier successful results and avoid repeating identical reads.",
                )
            },
        ),
        available_tools=[],
    )

    messages = builder.build_messages(session)
    user = str(messages[-1]["content"])

    assert "Resume state:" in user
    assert "- last_run_id: run-2" in user
    assert "- last_outcome: runtime_error" in user
    assert "- last_tools: read_file, run_tests" in user
    assert "- open_issue: Model API is unavailable right now. timed out." in user


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


def test_parse_reply_ignores_false_positive_json_in_text():
    parser = ModelReplyParser()
    content = "Here is some text containing a JSON object: {\"path\": \"/some/path.txt\"} but this is not the reply schema."
    reply = parser.parse_reply(content)
    assert reply.message == content
    assert reply.done is True


def test_parse_reply_ignores_strict_json_without_schema_keys():
    parser = ModelReplyParser()
    content = '{"random_key": "random_value"}'
    reply = parser.parse_reply(content)
    assert reply.message == content
    assert reply.done is True


def test_parse_reply_allows_tool_only_json_response():
    parser = ModelReplyParser()
    content = '{"actions": [{"name": "read_file", "arguments": {"path": "foo.txt"}}]}'
    reply = parser.parse_reply(content, allowed_tool_names={"read_file"})
    assert reply.message == "Model requested tool calls."
    assert reply.done is False
    assert len(reply.actions) == 1
    assert reply.actions[0].name == "read_file"
