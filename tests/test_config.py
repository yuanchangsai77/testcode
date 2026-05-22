from testcode.config import load_dotenv, load_runtime_config


def test_load_dotenv_reads_values_without_overwriting_existing_env(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        """
# ignored
TESTCODE_MODEL_BASE_URL=http://127.0.0.1:3000
TESTCODE_MODEL_NAME="quoted-model"
TESTCODE_MODE=auto
EXISTING=from-file
MALFORMED
        """.strip(),
        encoding="utf-8",
    )
    monkeypatch.delenv("TESTCODE_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("TESTCODE_MODEL_NAME", raising=False)
    monkeypatch.delenv("TESTCODE_MODE", raising=False)
    monkeypatch.setenv("EXISTING", "from-env")

    load_dotenv(env_path)

    assert load_runtime_config().model_base_url == "http://127.0.0.1:3000"
    assert load_runtime_config().model_name == "quoted-model"
    assert load_runtime_config().mode == "auto"
    assert load_runtime_config().model_timeout == 60.0
    assert load_runtime_config(mode="readonly").mode == "readonly"
    assert load_runtime_config().model_timeout == 60.0
    assert __import__("os").environ["EXISTING"] == "from-env"


def test_load_runtime_config_defaults_and_timeout_fallbacks(monkeypatch):
    monkeypatch.delenv("TESTCODE_MODEL_BASE_URL", raising=False)
    monkeypatch.setenv("TESTCODE_MODEL_NAME", " ")
    monkeypatch.setenv("TESTCODE_MODEL_TIMEOUT", "not-a-number")
    monkeypatch.setenv("TESTCODE_MODE", "")

    config = load_runtime_config()

    assert config.model_base_url == ""
    assert config.model_name == "gpt-5.4"
    assert config.model_timeout == 60.0
    assert config.mode == "confirm"

    monkeypatch.setenv("TESTCODE_MODEL_TIMEOUT", "-1")
    assert load_runtime_config().model_timeout == 60.0

    monkeypatch.setenv("TESTCODE_MODEL_TIMEOUT", "2.5")
    assert load_runtime_config().model_timeout == 2.5
