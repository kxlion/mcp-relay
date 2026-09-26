import json

import pytest
import yaml

from mcp_relay import config
from mcp_relay.config import ClientConfig
from mcp_relay.server import RelaySettings

TOKENS = {"client_token": 'client-test-synthetic-credential-0000000000000000', "mcp_token": 'mcp-test-synthetic-credential-0000000000000000'}
ENV = {"RELAY_CLIENT_TOKEN": 'client-test-synthetic-credential-0000000000000000', "RELAY_MCP_TOKEN": 'mcp-test-synthetic-credential-0000000000000000'}


def test_flat_client_schema():
    assert set(ClientConfig.model_fields) == {
        "identity", "relay_url", "workspace", "admin", "mcp_servers"
    }


def test_server_environment_defaults_and_independent_listeners():
    settings = RelaySettings.from_environment(ENV)
    assert (settings.mcp_bind_host, settings.mcp_port) == ("127.0.0.1", 8000)
    assert (settings.client_bind_host, settings.client_port) == ("127.0.0.1", 8001)
    settings = RelaySettings.from_environment({**ENV, "RELAY_SERVER_MCP_HOST": "::1", "RELAY_SERVER_MCP_PORT": "9100"})
    assert (settings.mcp_bind_host, settings.mcp_port) == ("::1", 9100)
    assert (settings.client_bind_host, settings.client_port) == ("127.0.0.1", 8001)
    settings = RelaySettings.from_environment({**ENV, "RELAY_SERVER_CLIENT_HOST": "0.0.0.0", "RELAY_SERVER_CLIENT_PORT": "9200"})
    assert (settings.client_bind_host, settings.client_port) == ("0.0.0.0", 9200)
    assert (settings.mcp_bind_host, settings.mcp_port) == ("127.0.0.1", 8000)


@pytest.mark.parametrize("field", ["mcp_bind_host", "client_bind_host"])
@pytest.mark.parametrize("value", ["localhost", "relay.example.test", "", "127.0.0.999", "[::1]"])
@pytest.mark.parametrize("method", ["constructor", "model_validate", "model_validate_json", "model_validate_strings", "environment"])
def test_all_server_validation_paths_require_literal_ips(field, value, method):
    data = {**TOKENS, field: value}
    with pytest.raises(ValueError, match="invalid relay server configuration"):
        if method == "constructor":
            RelaySettings(**data)
        elif method == "model_validate_json":
            RelaySettings.model_validate_json(json.dumps(data))
        elif method == "environment":
            key = "RELAY_SERVER_MCP_HOST" if field == "mcp_bind_host" else "RELAY_SERVER_CLIENT_HOST"
            RelaySettings.from_environment({**ENV, key: value})
        else:
            getattr(RelaySettings, method)(data)


@pytest.mark.parametrize("hosts", [("127.0.0.1", "127.0.0.1"), ("::1", "0:0:0:0:0:0:0:1")])
def test_same_normalized_ip_requires_distinct_ports(hosts):
    with pytest.raises(ValueError):
        RelaySettings(**TOKENS, mcp_bind_host=hosts[0], client_bind_host=hosts[1], mcp_port=9000, client_port=9000)


def test_distinct_ips_can_share_a_port_and_ipv6_is_normalized():
    settings = RelaySettings(**TOKENS, mcp_bind_host="0:0:0:0:0:0:0:1", client_bind_host="127.0.0.1", mcp_port=9000, client_port=9000)
    assert settings.mcp_bind_host == "::1"


@pytest.mark.parametrize("key", ["RELAY_MAX_TIMEOUT_SECONDS", "RELAY_CANCEL_SEND_TIMEOUT_SECONDS"])
def test_live_timeout_environment_settings(key):
    field = key.removeprefix("RELAY_").lower()
    settings = RelaySettings.from_environment({**ENV, key: "0.5"})
    assert getattr(settings, field) == 0.5
    for invalid in ("0", "nan", "inf", "bad"):
        with pytest.raises(ValueError):
            RelaySettings.from_environment({**ENV, key: invalid})
    if key == "RELAY_MAX_TIMEOUT_SECONDS":
        assert RelaySettings.from_environment({**ENV, key: "0.00001"}).max_timeout_seconds == 0.00001
    else:
        with pytest.raises(ValueError):
            RelaySettings.from_environment({**ENV, key: "0.00001"})


@pytest.mark.parametrize("field", ["mcp_port", "client_port"])
@pytest.mark.parametrize("value", [0, 65536, True, "8000x", None])
def test_server_port_bounds(field, value):
    with pytest.raises(ValueError):
        RelaySettings(**TOKENS, **{field: value})


def test_server_runtime_never_reads_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr(config.os, "environ", {})
    path = tmp_path / "config.yaml"
    path.write_text("not: [valid YAML")
    (tmp_path / ".env").write_text('RELAY_CLIENT_TOKEN=client-test-synthetic-credential-0000000000000000\nRELAY_MCP_TOKEN=mcp-test-synthetic-credential-0000000000000000\nRELAY_SERVER_MCP_PORT=9100\nRELAY_SERVER_CLIENT_HOST=::1\n')
    (tmp_path / ".env").chmod(0o600)
    monkeypatch.setattr(config, "_load_yaml", lambda *a, **kw: pytest.fail("server read YAML"))
    runtime = config.load_server_runtime(path, env={"RELAY_SERVER_MCP_PORT": "9200"})
    assert runtime.settings.mcp_port == 9200
    assert runtime.settings.client_bind_host == "::1"
    assert config.validate_document(path, "server", env=ENV).valid
    assert config.read_server_client_token(path, env={}) == 'client-test-synthetic-credential-0000000000000000'


def test_flat_config_helpers_roundtrip(tmp_path):
    path = tmp_path / "config.yaml"
    config.init_config(path, "client", env=ENV)
    document = yaml.safe_load(path.read_text())
    assert set(document) == set(ClientConfig.model_fields)
    config.set_value(path, "client", "admin", "false")
    assert config.get_section(path, "client")["admin"] is False
    config.unset_value(path, "client", "admin")
    assert config.load_client_admin_setting(path) is False
    config.set_value(path, "client", "admin", "true")
    assert config.load_client_admin_setting(path) is True
    assert config.load_client_settings(path, env=ENV).server_url == ClientConfig().relay_url
    assert config.show_document(path, env={})["admin"]["value"] is True


@pytest.mark.parametrize("key", ["unexpected-secret-key", 123])
def test_unknown_root_errors_are_generic(tmp_path, key):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({key: "secret-value"}))
    path.chmod(0o600)
    with pytest.raises(config.ConfigError, match="unknown root key") as caught:
        config.show_document(path, env={})
    assert str(key) not in str(caught.value)
    assert "secret-value" not in str(caught.value)
