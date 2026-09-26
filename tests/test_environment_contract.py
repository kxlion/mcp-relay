"""Startup rejects misspelled Relay names without echoing values."""

import pytest

from mcp_relay import client, config, json_bounds
from mcp_relay.server import RelaySettings


@pytest.fixture
def sources(tmp_path, monkeypatch):
    monkeypatch.setattr(config.os, "environ", {})
    path = tmp_path / "config.yaml"
    config.init_config(path, "client", token='client-test-synthetic-credential-0000000000000000', env={})
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    env = {
        "RELAY_CLIENT_TOKEN": 'client-test-synthetic-credential-0000000000000000',
        "RELAY_MCP_TOKEN": 'mcp-test-synthetic-credential-0000000000000000',
        "RELAY_URL": "ws://127.0.0.1:8001/ws",
        "RELAY_CLIENT_WORKSPACE": str(workspace),
        "RELAY_CLIENT_ID": "test-client",
    }
    yield path, env
    json_bounds.resolve_size_overrides({})


@pytest.mark.parametrize("loader", [config.load_client_settings, config.load_server_runtime])
@pytest.mark.parametrize("source", ["process", "dotenv"])
@pytest.mark.parametrize("value", ["", "synthetic-secret-do-not-print"])
def test_unknown_relay_name_blocks_startup(sources, loader, source, value):
    path, env = sources
    key = "RELAY_SERVER_MCP_PORRT"
    if source == "process":
        env[key] = value
    else:
        dotenv = config.dotenv_path(path)
        dotenv.write_text(f"{key}={value}\n")
        dotenv.chmod(0o600)
    with pytest.raises(ValueError, match=key) as caught:
        loader(path, env=env)
    assert "synthetic-secret-do-not-print" not in str(caught.value)
    assert key not in config.os.environ


@pytest.mark.parametrize("loader", [RelaySettings.from_environment, client.ClientSettings.from_environment])
def test_direct_environment_loaders_reject_unknown_names(sources, loader):
    _, env = sources
    with pytest.raises(ValueError, match="RELAY_CLIENT_WORKSPCE") as caught:
        loader({**env, "RELAY_CLIENT_WORKSPCE": "synthetic-secret-do-not-print"})
    assert "synthetic-secret-do-not-print" not in str(caught.value)


@pytest.mark.parametrize("with_config", [True, False])
def test_actual_client_startup_rejects_unknown_environment(sources, monkeypatch, capsys, with_config):
    path, env = sources
    monkeypatch.setattr(config.os, "environ", {**env, "RELAY_TYPPO": "synthetic-secret-do-not-print"})
    monkeypatch.setattr(client, "_set_log_file", lambda *_: None)
    monkeypatch.setattr(client, "_run_client", lambda *a, **k: pytest.fail("runtime started"))
    with pytest.raises(SystemExit) as caught:
        client.main(["--config", str(path)] if with_config else [])
    assert caught.value.code == 2
    error = capsys.readouterr().err
    assert "RELAY_TYPPO" in error
    assert "synthetic-secret-do-not-print" not in error


@pytest.mark.parametrize("loader", [config.load_client_settings, config.load_server_runtime])
def test_shared_declared_names_and_unrelated_environment_are_allowed(sources, loader):
    path, env = sources
    env.update({
        "RELAY_SERVER_MCP_PORT": "9100",
        "RELAY_SERVER_CLIENT_PORT": "9101",
        "RELAY_MAX_TIMEOUT_SECONDS": "10",
        "RELAY_CANCEL_SEND_TIMEOUT_SECONDS": "0.5",
        "RELAY_MAX_RESULT_NODES": "8192",
        "RELAY_MAX_TOOL_RESULT_BYTES": "1048576",
        "RELAY_MAX_WS_MESSAGE_BYTES": "2097152",
        "PATH": "/synthetic/bin",
        "PROVIDER_API_KEY": "synthetic-secret",
    })
    assert loader(path, env=env)
    assert json_bounds.MAX_RESULT_NODES == 8192
