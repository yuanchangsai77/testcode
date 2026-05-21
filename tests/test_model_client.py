import pytest

from testcode.app import create_model_client
from testcode.model.client import OpenAICompatibleModelClient, StubModelClient
from testcode.observability.logger import InMemoryLogger


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
