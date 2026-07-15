import pytest

from testcode.config import load_dotenv, load_runtime_config
from testcode.mcp.adapter import build_stable_tool_name, map_mcp_tool_risk
from testcode.mcp.config import MCPServerConfig
from testcode.mcp.types import MCPToolDescriptor


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


def test_load_runtime_config_parses_project_mcp_servers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    config_dir = tmp_path / ".testcode"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[[mcp.servers]]
name = "github"
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
tool_name_prefix = "gh"
timeout = 45

[mcp.servers.env]
GITHUB_TOKEN = "${GITHUB_TOKEN}"

[mcp.servers.risk_overrides]
create_issue = "write"
        """.strip(),
        encoding="utf-8",
    )

    config = load_runtime_config()

    assert len(config.mcp_servers) == 1
    server = config.mcp_servers[0]
    assert server.name == "github"
    assert server.transport == "stdio"
    assert server.command == "npx"
    assert server.args == ("-y", "@modelcontextprotocol/server-github")
    assert server.env["GITHUB_TOKEN"] == "secret-token"
    assert server.risk_overrides["create_issue"] == "write"
    assert server.stable_prefix == "gh"


def test_load_runtime_config_rejects_duplicate_mcp_server_names(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".testcode"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[[mcp.servers]]
name = "duplicate"
transport = "stdio"
command = "first"

[[mcp.servers]]
name = "duplicate"
transport = "stdio"
command = "second"
        """.strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate MCP server name 'duplicate'"):
        load_runtime_config()


def test_mcp_adapter_helpers_apply_stable_names_and_risk_overrides():
    server = MCPServerConfig(
        name="github",
        transport="stdio",
        tool_name_prefix="gh",
        risk_overrides={"create_issue": "destructive"},
    )
    create_descriptor = MCPToolDescriptor(
        server_name="github",
        tool_name="create_issue",
        description="Create a remote issue",
    )
    search_descriptor = MCPToolDescriptor(
        server_name="github",
        tool_name="search_repositories",
        description="Search remote repositories via API",
    )

    assert build_stable_tool_name(server, "search_repositories") == "gh__search_repositories"
    assert map_mcp_tool_risk(server, create_descriptor) == "destructive"
    assert map_mcp_tool_risk(server, search_descriptor) == "network"


def test_mcp_environment_overrides_project_server_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".testcode"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[[mcp.servers]]
name = "remote-api"
transport = "streamable_http"
url = "https://project.example/mcp"
timeout = 10
        """.strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TESTCODE_MCP_REMOTE_API_URL", "https://env.example/mcp")
    monkeypatch.setenv("TESTCODE_MCP_REMOTE_API_TIMEOUT", "25")
    monkeypatch.setenv("TESTCODE_MCP_REMOTE_API_ENABLED", "false")

    server = load_runtime_config().mcp_servers[0]

    assert server.url == "https://env.example/mcp"
    assert server.timeout == 25
    assert server.enabled is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("enabled", "false", "field 'enabled' must be a boolean"),
        ("args", "--flag", "field 'args' must be an array of strings"),
        ("headers", ["Authorization"], "field 'headers' must be a string table"),
    ],
)
def test_mcp_config_rejects_invalid_field_types(field, value, message):
    from testcode.mcp.config import _build_server_config

    raw = {"name": "strict", "transport": "stdio", "command": "server", field: value}

    with pytest.raises(ValueError, match=message):
        _build_server_config(raw)
