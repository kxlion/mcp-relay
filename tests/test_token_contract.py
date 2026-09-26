"""Credential bounds and pre-write validation at each configuration boundary."""

import pytest

from mcp_relay import config
from mcp_relay.client import ClientSettings, ConfigurationError
from mcp_relay.server import RelaySettings

CLIENT = "c" * 32
MCP = "m" * 32


@pytest.mark.parametrize("field", ["client_token", "mcp_token"])
@pytest.mark.parametrize("value,valid", [
    ("", False), ("x" * 31, False), ("x" * 32, True),
    ("x" * 256, True), ("x" * 257, False), ("é" * 32, False),
    ("x" * 31 + "é", False),
])
def test_server_credential_bounds(field, value, valid):
    credentials = {"client_token": CLIENT, "mcp_token": MCP, field: value}
    if valid:
        assert getattr(RelaySettings(**credentials), field) == value
    else:
        with pytest.raises(ValueError) as error:
            RelaySettings(**credentials)
        assert value not in str(error.value) or not value


@pytest.mark.parametrize("value,valid", [
    ("", False), ("x" * 31, False), ("x" * 32, True),
    ("x" * 256, True), ("x" * 257, False), ("é" * 32, False),
])
def test_client_credential_bounds(tmp_path, value, valid):
    data = {"server_url": "ws://localhost:8001", "client_id": "client", "client_token": value, "workspace": tmp_path}
    if valid:
        assert ClientSettings(**data).client_token.get_secret_value() == value
    else:
        with pytest.raises(ConfigurationError) as error:
            ClientSettings(**data)
        assert value not in str(error.value) or not value


def test_equal_conforming_credentials_still_rejected():
    with pytest.raises(ValueError):
        RelaySettings(client_token=CLIENT, mcp_token=CLIENT)


@pytest.mark.parametrize("source", ["env", "dotenv"])
@pytest.mark.parametrize("scope", ["server", "client"])
def test_short_credential_rejected_by_config_validation(tmp_path, source, scope):
    path = tmp_path / "relay.yaml"
    env = {"RELAY_CLIENT_TOKEN": CLIENT, "RELAY_MCP_TOKEN": MCP}
    if scope == "client":
        config.init_config(path, "client", env=env)
    if source == "dotenv":
        (tmp_path / ".env").write_text(f"RELAY_CLIENT_TOKEN={CLIENT}\nRELAY_MCP_TOKEN={MCP}\n")
        (tmp_path / ".env").chmod(0o600)
        env = {}
    key = "RELAY_MCP_TOKEN" if scope == "server" else "RELAY_CLIENT_TOKEN"
    short = "s" * 31
    if source == "dotenv":
        dotenv = tmp_path / ".env"
        dotenv.write_text(dotenv.read_text().replace(f"{key}={env.get(key, MCP if scope == 'server' else CLIENT)}", f"{key}={short}"))
    else:
        env[key] = short
    report = config.validate_document(path, scope, env=env)
    assert not report.valid
    assert short not in str(report)


@pytest.mark.parametrize("source", ["missing", "stdin", "env", "dotenv"])
def test_init_rejects_invalid_token_before_creating_files(tmp_path, source):
    path = tmp_path / "relay.yaml"
    workspace = tmp_path / "workspace"
    env = {}
    token = None
    if source == "stdin":
        token = "s" * 31
    elif source == "env":
        env["RELAY_CLIENT_TOKEN"] = "s" * 31
    elif source == "dotenv":
        dotenv = tmp_path / ".env"
        dotenv.write_text("RELAY_CLIENT_TOKEN=" + "s" * 31 + "\n")
        dotenv.chmod(0o600)
    with pytest.raises(config.ConfigError):
        config.init_config(path, "client", token=token, env=env, workspace=workspace)
    assert not path.exists()
    assert not workspace.exists()
    assert (tmp_path / ".env").exists() == (source == "dotenv")


@pytest.mark.parametrize("source", ["env", "dotenv"])
@pytest.mark.parametrize("scope", ["server", "client"])
def test_invalid_token_error_names_the_offending_key(tmp_path, source, scope):
    path = tmp_path / "relay.yaml"
    env = {"RELAY_CLIENT_TOKEN": CLIENT, "RELAY_MCP_TOKEN": MCP}
    if scope == "client":
        config.init_config(path, "client", env=env)
    if source == "dotenv":
        (tmp_path / ".env").write_text(
            f"RELAY_CLIENT_TOKEN={CLIENT}\nRELAY_MCP_TOKEN={MCP}\n"
        )
        (tmp_path / ".env").chmod(0o600)
        env = {}
    key = "RELAY_MCP_TOKEN" if scope == "server" else "RELAY_CLIENT_TOKEN"
    env[key] = "x" * 31
    report = config.validate_document(path, scope, env=env)
    assert not report.valid
    assert key in str(report)


def test_invalid_token_error_from_load_server_runtime_names_the_key(tmp_path):
    path = tmp_path / "relay.yaml"
    env = {"RELAY_CLIENT_TOKEN": CLIENT, "RELAY_MCP_TOKEN": "x" * 31}
    with pytest.raises(config.ConfigError) as error:
        config.load_server_runtime(path, env=env)
    assert "RELAY_MCP_TOKEN" in str(error.value)
    assert "RELAY_CLIENT_TOKEN" not in str(error.value)
