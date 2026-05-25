from pathlib import Path

from testcode.safety.redaction import REDACTED, is_sensitive_path, redact, redact_text


def test_redact_handles_nested_values_and_sensitive_keys():
    value = {
        "safe": ["hello", {"token": "plain-secret"}],
        "headers": {"Authorization": "Bearer abcdef1234567890"},
        "text": "api_key: sk-test123456789abcdef password=hunter2",
    }

    redacted = redact(value)

    assert redacted["safe"] == ["hello", {"token": REDACTED}]
    assert redacted["headers"]["Authorization"] == REDACTED
    assert redacted["text"] == f"api_key:{REDACTED} password={REDACTED}"


def test_redact_text_handles_assignments_bearer_and_known_token_prefixes():
    text = "TOKEN=sk-test123456789abcdef Authorization: Bearer abcdef1234567890 ghp_1234567890abcdef"

    redacted = redact_text(text)

    assert "sk-test123456789abcdef" not in redacted
    assert "abcdef1234567890" not in redacted
    assert "ghp_1234567890abcdef" not in redacted
    assert redacted.count(REDACTED) == 3


def test_sensitive_path_matching_is_case_insensitive():
    assert is_sensitive_path(Path("Secrets/config.txt")) is True
    assert is_sensitive_path(Path(".ENV")) is True
    assert is_sensitive_path(Path("cert.PEM")) is True
    assert is_sensitive_path(Path(".env.example")) is False
