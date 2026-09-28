from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from mcp_relay import cli, config
from mcp_relay.server import RelaySettings


def _write_operator_dotenv(config_path: Path, lines: list[str]) -> Path:
    """Play the operator: provide credentials the way they would."""
    dotenv = config_path.parent / ".env"
    dotenv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        dotenv.chmod(0o600)
    return dotenv


def test_server_settings_default_to_loopback_without_a_transport_policy() -> None:
    settings = RelaySettings.from_environment(
        {"RELAY_MCP_TOKEN": "t-mcp-synthetic-credential-0000000000000000", "RELAY_CLIENT_TOKEN": "t-client-synthetic-credential-0000000000000000"}
    )
    field_name = "allow_" + "insecure_ws"
    assert settings.mcp_bind_host == "127.0.0.1"
    assert settings.client_bind_host == "127.0.0.1"
    assert field_name not in RelaySettings.model_fields


def _write_private_server_env(config_path: Path, lines: list[str]) -> Path:
    """Server settings are environment-only: play the operator directly."""
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)
    return _write_operator_dotenv(config_path, lines)


def test_init_rejects_symlinked_dotenv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config-parent"
    outside = tmp_path / "outside"
    config_dir.mkdir()
    outside.mkdir()
    try:
        os.symlink(outside / ".env", config_dir / ".env")
    except OSError:
        pytest.skip("symbolic links are unavailable")
    monkeypatch.setenv("RELAY_CLIENT_TOKEN", "client-secret-synthetic-credential-0000000000000000")

    config_path = config_dir / "config.yaml"
    with pytest.raises(config.ConfigError, match="symlink"):
        config.init_config(config_path, "client")
    assert not (outside / ".env").exists()


def test_empty_canonical_token_environment_override_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    with pytest.raises(config.ConfigError):
        config.load_server_runtime(
            config_path, env={"RELAY_MCP_TOKEN": "", "RELAY_CLIENT_TOKEN": "t-client-synthetic-credential-0000000000000000"}
        )


def test_load_server_runtime_rejects_shared_process_tokens(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    with pytest.raises(config.ConfigError, match="invalid relay server configuration"):
        config.load_server_runtime(
            config_path,
            env={
                "RELAY_MCP_TOKEN": "shared-synthetic-credential-0000000000000000000",
                "RELAY_CLIENT_TOKEN": "shared-synthetic-credential-0000000000000000000",
            },
        )


def test_dotenv_values_are_used_without_mutating_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    _write_private_server_env(
        config_path,
        ["RELAY_MCP_TOKEN=dotenv-mcp-synthetic-credential-000000000000000", "RELAY_CLIENT_TOKEN=t-client-synthetic-credential-0000000000000000"],
    )
    monkeypatch.delenv("RELAY_MCP_TOKEN", raising=False)
    runtime = config.load_server_runtime(config_path, env={})
    assert runtime.settings.mcp_token == "dotenv-mcp-synthetic-credential-000000000000000"
    assert "RELAY_MCP_TOKEN" not in os.environ


def test_dotenv_log_level_is_exported_without_overwriting_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    dotenv = _write_private_server_env(
        config_path,
        [
            "RELAY_MCP_TOKEN=t-mcp-synthetic-credential-0000000000000000",
            "RELAY_CLIENT_TOKEN=t-client-synthetic-credential-0000000000000000",
            "LOG_LEVEL=DEBUG",
        ],
    )
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    runtime = config.load_server_runtime(config_path)
    assert runtime.settings.mcp_token
    assert os.environ["LOG_LEVEL"] == "DEBUG"

    # An explicit process variable wins over the .env fallback.
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    config.load_server_runtime(config_path)
    assert os.environ["LOG_LEVEL"] == "WARNING"

    # Read-only: the relay left the operator's file untouched.
    assert "LOG_LEVEL=DEBUG" in dotenv.read_text(encoding="utf-8")


def test_dotenv_export_requires_the_real_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An injected environment view never mutates the global process env."""
    config_path = tmp_path / "config.yaml"
    _write_private_server_env(
        config_path,
        [
            "RELAY_MCP_TOKEN=t-mcp-synthetic-credential-0000000000000000",
            "RELAY_CLIENT_TOKEN=t-client-synthetic-credential-0000000000000000",
            "LOG_LEVEL=DEBUG",
        ],
    )
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    config.load_server_runtime(config_path, env={})
    assert "LOG_LEVEL" not in os.environ


def test_dotenv_any_key_is_exported_except_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modèle 2: every non-secret entry is an environment override."""
    config_path = tmp_path / "config.yaml"
    _write_private_server_env(
        config_path,
        [
            "RELAY_MCP_TOKEN=t-mcp-synthetic-credential-0000000000000000",
            "RELAY_CLIENT_TOKEN=t-client-synthetic-credential-0000000000000000",
            "RELAY_MAX_TOOL_RESULT_BYTES=1048576",
            "SOME_FUTURE_FLAG=1",
        ],
    )
    monkeypatch.delenv("RELAY_MAX_TOOL_RESULT_BYTES", raising=False)
    monkeypatch.delenv("SOME_FUTURE_FLAG", raising=False)
    config.load_server_runtime(config_path)
    assert os.environ["RELAY_MAX_TOOL_RESULT_BYTES"] == "1048576"
    assert os.environ["SOME_FUTURE_FLAG"] == "1"
    # Credentials are never exported into the process environment.
    assert "RELAY_MCP_TOKEN" not in os.environ
    assert "RELAY_CLIENT_TOKEN" not in os.environ


@pytest.mark.parametrize(
    "contents, expected",
    [
        ("RELAY_MCP_TOKEN\n", "invalid line"),
        ("x" * 4097, "too large"),
    ],
)
def test_dotenv_rejects_unsupported_syntax(
    tmp_path: Path, contents: str, expected: str
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)
    dotenv = config_path.parent / ".env"
    dotenv.write_text(contents, encoding="utf-8")
    if os.name != "nt":
        dotenv.chmod(0o600)
    with pytest.raises(config.ConfigError, match=expected):
        config.load_server_runtime(config_path, env={})


def test_server_runtime_reports_sanitized_validation_errors(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.yaml"
    secret = "SHARED_TOKEN_SENTINEL-synthetic-credential-0000000000000000"

    with pytest.raises(config.ConfigError) as error:
        config.load_server_runtime(
            config_path,
            env={"RELAY_MCP_TOKEN": secret, "RELAY_CLIENT_TOKEN": secret},
        )

    message = str(error.value)
    assert message == "invalid relay server configuration"
    assert secret not in message


def test_yaml_boolean_and_environment_boolean_are_parsed_strictly(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "client", env={"RELAY_CLIENT_TOKEN": "test-client-synthetic-credential-000000000"})
    config.set_value(config_path, "client", "admin", "true")
    settings = config.load_client_settings(config_path, env={"RELAY_CLIENT_TOKEN": "test-client-synthetic-credential-000000000"})
    assert settings is not None
    # Strict numeric parse is exercised through the server runtime environment.
    server_runtime = config.load_server_runtime(
        config_path,
        env={
            "RELAY_MCP_TOKEN": "t-mcp-synthetic-credential-0000000000000000",
            "RELAY_CLIENT_TOKEN": "test-client-synthetic-credential-000000000",
            "RELAY_MAX_TIMEOUT_SECONDS": "45",
        },
    )
    assert server_runtime.settings.max_timeout_seconds == 45.0


def test_init_client_starts_without_static_tool_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / ".mcp-relay" / "config.yaml"

    # The operator provides the token through the process environment.
    monkeypatch.setenv("RELAY_CLIENT_TOKEN", "client-secret-synthetic-credential-0000000000000000")
    config.init_config(config_path, "client")

    from yaml import safe_load

    document = safe_load(config_path.read_text(encoding="utf-8"))
    assert "tools" not in document
    assert document["identity"]["id"]
    # Read-only contract: the relay never created or wrote a .env.
    assert not (config_path.parent / ".env").exists()




@pytest.mark.parametrize("bad_shape", ["duplicate", "scalar"])
def test_reinit_rejects_an_unknown_tools_key_in_any_shape(
    tmp_path: Path, bad_shape: str
) -> None:
    config_path = tmp_path / f"{bad_shape}.yaml"
    config.init_config(config_path, "client", env={"RELAY_CLIENT_TOKEN": "client-secret-synthetic-credential-0000000000000000"})
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if bad_shape == "duplicate":
        document["tools"] = {"allowlist": [
            "relay_sample_ping",
            "relay_sample_ping",
        ]}
    else:
        document["tools"] = "not-a-mapping"
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(document, handle, sort_keys=False)

    with pytest.raises(config.ConfigError, match="unknown root key"):
        config.init_config(config_path, "client", env={})




def test_init_client_from_server_uses_the_effective_dotenv_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)
    dotenv = _write_operator_dotenv(
        config_path,
        [
            "RELAY_MCP_TOKEN=t-mcp-synthetic-credential-0000000000000000",
            "RELAY_CLIENT_TOKEN=custom-server-synthetic-credential-00000000",
        ],
    )
    monkeypatch.delenv("RELAY_CLIENT_TOKEN", raising=False)

    config.init_config(config_path, "client", token=config.read_server_client_token(config_path))

    # Read-only: the operator's .env is untouched by the client init.
    client_token = dotenv.read_text(encoding="utf-8")
    assert "RELAY_CLIENT_TOKEN=custom-server-synthetic-credential-00000000\n" in client_token
    output = capsys.readouterr()
    assert "custom-server-synthetic-credential-00000000" not in output.out
    assert "custom-server-synthetic-credential-00000000" not in output.err


def test_server_client_token_source_honors_environment_override(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)

    assert config.read_server_client_token(
        config_path,
        env={"RELAY_CLIENT_TOKEN": "environment-server-token-synthetic-credential-0000000000000000"},
    ) == "environment-server-token-synthetic-credential-0000000000000000"










def test_unset_token_is_refused_with_guidance(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("RELAY_CLIENT_TOKEN", "client-secret-synthetic-credential-0000000000000000")
    assert config.init_config(config_path, "client") == config_path
    capsys.readouterr()
    with pytest.raises(config.ConfigError, match="tokens are not managed"):
        config.unset_value(config_path, "client", "client_token")








def test_transport_validation_reports_scheme_without_sensitive_url_data(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "client", token="client-secret-synthetic-credential-0000000000000000", env={})

    config.set_value(
        config_path,
        "client",
        "relay_url",
        "ws://192.168.1.20:8000/ws?token=must-not-be-rendered",
    )
    ws_report = cli._render_validation(
        config.validate_document(config_path, "client", env={})
    )
    assert "transport=ws://" in ws_report
    assert "unencrypted" in ws_report
    # The validation summary prints the scheme only, never the URL.
    assert "token=must-not-be-rendered" not in ws_report

    config.set_value(
        config_path,
        "client",
        "relay_url",
        "wss://relay.example.test/ws?token=must-not-be-rendered",
    )
    wss_report = cli._render_validation(
        config.validate_document(config_path, "client", env={})
    )
    assert "transport=wss://" in wss_report
    assert "TLS expected" in wss_report
    assert "token=must-not-be-rendered" not in wss_report




def test_canonical_environment_overrides_defaults_for_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("RELAY_CLIENT_TOKEN", "client-secret-synthetic-credential-0000000000000000")
    assert config.init_config(config_path, "client") == config_path
    capsys.readouterr()
    monkeypatch.setenv("RELAY_URL", "ws://127.0.0.1:9100/ws")
    assert config.validate_document(config_path, "client").valid
    # The environment override wins over the file's default relay_url.
    settings = config.load_client_settings(config_path, env=dict(os.environ))
    assert settings is not None
    assert settings.server_url == "ws://127.0.0.1:9100/ws"






def test_relay_url_is_not_locked_to_a_specific_websocket_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("RELAY_CLIENT_TOKEN", "client-secret-synthetic-credential-0000000000000000")
    assert config.init_config(config_path, "client") == config_path
    config.set_value(config_path, "client", "relay_url", "ws://relay.example.test/future-endpoint")
    assert config.validate_document(config_path, "client").valid

    config.set_value(config_path, "client", "relay_url", "ws://relay.example.test:not-a-port/future")
    assert not config.validate_document(config_path, "client").valid


def test_repeated_client_init_preserves_identity_settings_and_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("RELAY_CLIENT_TOKEN", "client-secret-synthetic-credential-0000000000000000")
    config.init_config(config_path, "client")
    first = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    monkeypatch.setattr("getpass.getpass", lambda *_: pytest.fail("must not prompt"))
    assert config.init_config(config_path, "client") == config_path
    second = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert second["identity"]["id"] == first["identity"]["id"]
    assert second == first
    # Read-only contract: no .env was created or written by the init.
    assert not (config_path.parent / ".env").exists()


def test_repeated_client_init_prompts_when_no_token_source_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.delenv("RELAY_CLIENT_TOKEN", raising=False)
    monkeypatch.setattr("getpass.getpass", lambda *_: "replacement-client-synthetic-credential-00000000000")
    # An existing .env without the token key still allows the interactive
    # prompt; the entered value is validated but never persisted by the relay.
    dotenv = config_path.parent / ".env"
    dotenv.write_text("", encoding="utf-8")
    if os.name != "nt":
        dotenv.chmod(0o600)

    assert config.init_config(config_path, "client", token="replacement-client-synthetic-credential-00000000000") == config_path

    assert dotenv.read_text(encoding="utf-8") == ""


def test_force_reinitializes_mutable_settings_but_preserves_client_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.delenv("RELAY_CLIENT_TOKEN", raising=False)
    assert config.init_config(config_path, "client", token="client-secret-synthetic-credential-0000000000000000", env={}) == config_path
    capsys.readouterr()
    config.set_value(config_path, "client", "relay_url", "ws://127.0.0.1:9000/ws")
    before = yaml.safe_load(config_path.read_text(encoding="utf-8"))["identity"]["id"]
    assert config.init_config(config_path, "client", token="client-secret-synthetic-credential-0000000000000000", force=True) == config_path
    after = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert after["identity"]["id"] == before
    # Mutable settings are reinitialized to defaults by --force.
    assert after["relay_url"] == "ws://127.0.0.1:8001/ws"


def _write_private_config(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _client_admin_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "client", env={"RELAY_CLIENT_TOKEN": "t-client-synthetic-credential-000000000000000"})
    return config_path


def test_admin_setting_set_show_and_unset_round_trip(tmp_path: Path) -> None:
    config_path = _client_admin_config(tmp_path)

    config.set_value(config_path, "client", "admin", "false")
    shown = config.show_document(config_path, env={})
    assert shown["admin"]["value"] is False
    assert shown["admin"]["source"] == "file"

    config.unset_value(config_path, "client", "admin")
    shown = config.show_document(config_path, env={})
    # Fail-closed: an unset key reads as locked, not as unlocked.
    assert shown["admin"]["value"] is False


@pytest.mark.parametrize("value", ["null", "1", '"true"'])
def test_non_boolean_admin_setting_is_reported_invalid(
    tmp_path: Path, value: str
) -> None:
    config_path = _client_admin_config(tmp_path)

    config.set_value(config_path, "client", "admin", value)

    report = config.validate_document(config_path, "client", env={})
    assert not report.valid


def test_config_show_lists_the_client_model_with_values_and_sources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 0

    shown = yaml.safe_load(capsys.readouterr().out)
    assert shown["config_file"] == str(config_path)
    for dotted in config.configuration_keys(config.ClientConfig):
        if dotted == config.MCP_SERVERS_PREFIX:
            # The alias mapping renders per configured alias; with no
            # aliases configured there is nothing to list for it.
            continue
        node = shown
        for part in dotted.split("."):
            node = node[part]
        assert node["source"] == "default"
        assert "value" in node
    assert not config_path.exists()
    assert not (tmp_path / ".env").exists()


def test_config_show_reports_file_sources_from_a_custom_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_private_config(
        tmp_path / "custom" / "config.yaml",
        "relay_url: ws://127.0.0.1:9000/ws\nworkspace: ./custom-workspace\n",
    )

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 0

    shown = yaml.safe_load(capsys.readouterr().out)
    assert shown["config_file"] == str(config_path)
    assert shown["relay_url"]["value"] == "ws://127.0.0.1:9000/ws"
    assert shown["relay_url"]["source"] == "file"
    assert shown["workspace"]["source"] == "file"


def test_config_show_reports_environment_sources_without_leaking_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RELAY_URL", "ws://127.0.0.1:9200/ws")
    monkeypatch.setenv("RELAY_CLIENT_WORKSPACE", "from-env")
    config_path = tmp_path / "config.yaml"

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 0

    shown = yaml.safe_load(capsys.readouterr().out)
    assert shown["relay_url"]["value"] == "ws://127.0.0.1:9200/ws"
    assert shown["relay_url"]["source"] == "environment"
    assert shown["workspace"]["value"] == "from-env"
    assert shown["workspace"]["source"] == "environment"


def test_config_show_redacts_sensitive_url_query_parameters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_private_config(
        tmp_path / "config.yaml",
        "relay_url: ws://relay.example.com/ws?token=super-secret&plan=pro\n",
    )

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 0

    # AGENTS.md: config show keeps masking credential-like URL values.
    out = capsys.readouterr().out
    assert "super-secret" not in out
    shown = yaml.safe_load(out)
    relay_url = shown["relay_url"]["value"]
    assert "token=[REDACTED]" in relay_url
    assert "plan=pro" in relay_url


def test_config_show_never_prints_dotenv_tokens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)
    dotenv = config_path.parent / ".env"
    dotenv.write_text(
        "RELAY_MCP_TOKEN=dotenv-mcp-synthetic-credential-00000000000000\nRELAY_CLIENT_TOKEN=dotenv-client-synthetic-credential-00000000\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        dotenv.chmod(0o600)

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 0

    out = capsys.readouterr().out
    assert "dotenv-mcp-synthetic-credential-00000000000000" not in out
    assert "dotenv-client-synthetic-credential-00000000" not in out
    assert "RELAY_MCP_TOKEN" not in out
    assert "RELAY_CLIENT_TOKEN" not in out


def test_config_show_fails_actionably_on_invalid_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_private_config(
        tmp_path / "config.yaml",
        "relay_url: not-a-url\n",
    )

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 1

    err = capsys.readouterr().err
    assert "client configuration is invalid" in err
    assert "relay_url" in err
    assert "not-a-url" not in err


def test_config_show_rejects_unknown_root_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_private_config(
        tmp_path / "config.yaml",
        "unknown_scope:\n  key: value\n",
    )

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 1

    assert "unknown root key" in capsys.readouterr().err


def test_config_show_fails_on_unreadable_config(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("symbolic links are unavailable")
    real = _write_private_config(tmp_path / "real.yaml", "server: {}\n")
    link = tmp_path / "link.yaml"
    os.symlink(real, link)

    with pytest.raises(config.ConfigError, match="symlink"):
        config.show_document(link)




# --------------------------------------------------------------------------
# Per-listener topology (env-only server settings)
# --------------------------------------------------------------------------


def test_doctor_reports_the_listener_binds(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_private_server_env(
        config_path, ["RELAY_MCP_TOKEN=t-mcp-synthetic-credential-0000000000000000", "RELAY_CLIENT_TOKEN=t-client-synthetic-credential-0000000000000000"]
    )

    runtime = config.load_server_runtime(config_path, env={})
    assert runtime.mcp_bind_host == "127.0.0.1"
    assert runtime.client_bind_host == "127.0.0.1"


def test_port_env_overrides_stay_independent_across_runtime_loading(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    _write_private_server_env(
        config_path, ["RELAY_MCP_TOKEN=t-mcp-synthetic-credential-0000000000000000", "RELAY_CLIENT_TOKEN=t-client-synthetic-credential-0000000000000000"]
    )

    runtime = config.load_server_runtime(
        config_path,
        env={
            "RELAY_SERVER_MCP_PORT": "9100",
            "RELAY_SERVER_CLIENT_PORT": "9200",
        },
    )
    assert runtime.mcp_port == 9100
    assert runtime.client_port == 9200
    assert runtime.settings.client_port == 9200

    with pytest.raises(config.ConfigError, match="invalid relay server configuration"):
        config.load_server_runtime(
            config_path,
            env={
                "RELAY_SERVER_MCP_PORT": "9100",
                "RELAY_SERVER_CLIENT_PORT": "9100",
            },
        )


def test_env_only_server_configuration_honors_the_client_port_override(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.yaml"

    runtime = config.load_server_runtime(
        missing,
        env={
            "RELAY_MCP_TOKEN": "t-mcp-synthetic-credential-0000000000000000",
            "RELAY_CLIENT_TOKEN": "t-client-synthetic-credential-0000000000000000",
            "RELAY_SERVER_CLIENT_PORT": "9200",
        },
    )

    assert runtime.mcp_port == 8000
    assert runtime.client_port == 9200
    assert runtime.settings.client_port == 9200


# --------------------------------------------------------------------------
# Task6: honest config show (per-field provenance, dotenv, LAN classification)
# --------------------------------------------------------------------------


def test_config_show_reports_dotenv_provenance_for_client_overrides(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)
    _write_operator_dotenv(
        config_path,
        [
            "RELAY_MCP_TOKEN=t-mcp-synthetic-credential-0000000000000000",
            "RELAY_CLIENT_TOKEN=t-client-synthetic-credential-0000000000000000",
            "RELAY_URL=ws://127.0.0.1:9300/ws",
        ],
    )

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    try:
        assert cli.main(["config", "show"]) == 0
    finally:
        # The loader exported the dotenv-only RELAY_URL into the real
        # process environment; pop it so later real-env loads stay hermetic.
        os.environ.pop("RELAY_URL", None)

    shown = yaml.safe_load(capsys.readouterr().out)
    assert shown["relay_url"]["value"] == "ws://127.0.0.1:9300/ws"
    assert shown["relay_url"]["source"] == ".env"


def test_config_show_environment_beats_dotenv_provenance(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)
    _write_operator_dotenv(
        config_path,
        [
            "RELAY_MCP_TOKEN=t-mcp-synthetic-credential-0000000000000000",
            "RELAY_CLIENT_TOKEN=t-client-synthetic-credential-0000000000000000",
            "RELAY_URL=ws://127.0.0.1:9300/ws",
        ],
    )
    monkeypatch.setenv("RELAY_URL", "ws://127.0.0.1:9400/ws")

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 0

    shown = yaml.safe_load(capsys.readouterr().out)
    assert shown["relay_url"]["value"] == "ws://127.0.0.1:9400/ws"
    assert shown["relay_url"]["source"] == "environment"


def test_config_show_reports_server_binds_with_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)
    _write_operator_dotenv(
        config_path,
        [
            "RELAY_MCP_TOKEN=t-mcp-synthetic-credential-0000000000000000",
            "RELAY_CLIENT_TOKEN=t-client-synthetic-credential-0000000000000000",
            "RELAY_SERVER_MCP_PORT=9100",
        ],
    )

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 0

    shown = yaml.safe_load(capsys.readouterr().out)
    assert shown["server"]["mcp"]["port"] == {"value": 9100, "source": ".env"}
    assert shown["server"]["mcp"]["bind_host"] == {
        "value": "127.0.0.1",
        "source": "default",
    }
    assert shown["server"]["client"]["port"]["value"] == 8001


def test_config_show_environment_port_provenance_beats_dotenv(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)
    _write_operator_dotenv(
        config_path,
        [
            "RELAY_MCP_TOKEN=t-mcp-synthetic-credential-0000000000000000",
            "RELAY_CLIENT_TOKEN=t-client-synthetic-credential-0000000000000000",
            "RELAY_SERVER_MCP_PORT=9100",
        ],
    )
    monkeypatch.setenv("RELAY_SERVER_MCP_PORT", "9500")

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 0

    shown = yaml.safe_load(capsys.readouterr().out)
    assert shown["server"]["mcp"]["port"] == {
        "value": 9500,
        "source": "environment",
    }


@pytest.mark.parametrize(
    "host, expected_kind",
    [("0.0.0.0", "wildcard"), ("192.168.1.10", "specific")],
)
def test_config_show_flags_nonloopback_binds_as_lan_exposed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    expected_kind: str,
) -> None:
    monkeypatch.setenv("RELAY_SERVER_CLIENT_HOST", host)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)
    _write_operator_dotenv(
        config_path, ["RELAY_MCP_TOKEN=t-mcp-synthetic-credential-0000000000000000", "RELAY_CLIENT_TOKEN=t-client-synthetic-credential-0000000000000000"]
    )

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 0

    shown = yaml.safe_load(capsys.readouterr().out)
    node = shown["server"]["client"]["bind_host"]
    assert node["value"] == host
    assert node["source"] == "environment"
    assert f"LAN-exposed ({expected_kind})" in node["warning"]


def test_config_show_loopback_binds_carry_no_lan_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)
    _write_operator_dotenv(
        config_path, ["RELAY_MCP_TOKEN=t-mcp-synthetic-credential-0000000000000000", "RELAY_CLIENT_TOKEN=t-client-synthetic-credential-0000000000000000"]
    )

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 0

    shown = yaml.safe_load(capsys.readouterr().out)
    assert "warning" not in shown["server"]["mcp"]["bind_host"]
    assert "warning" not in shown["server"]["client"]["bind_host"]


def test_config_show_labels_tokens_with_fixed_masks_and_sources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)
    _write_operator_dotenv(
        config_path,
        [
            "RELAY_MCP_TOKEN=dotenv-mcp-synthetic-credential-00000000000000",
            "RELAY_CLIENT_TOKEN=dotenv-client-synthetic-credential-00000000",
        ],
    )

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 0

    out = capsys.readouterr().out
    assert "dotenv-mcp-synthetic-credential-00000000000000" not in out
    assert "dotenv-client-synthetic-credential-00000000" not in out
    assert "RELAY_MCP_TOKEN" not in out
    assert "RELAY_CLIENT_TOKEN" not in out
    shown = yaml.safe_load(out)
    mcp_label = shown["secrets"]["mcp_token"]
    client_label = shown["secrets"]["client_token"]
    assert mcp_label["present"] is True
    assert mcp_label["source"] == ".env"
    assert client_label["present"] is True
    assert client_label["source"] == ".env"
    # Shared client token label: the same credential the client presents.
    assert client_label["role"] == "shared server-client credential"
    # Fixed-size masks only: never the value and never a length hint.
    assert mcp_label["value"] == "[REDACTED]"
    assert client_label["value"] == "[REDACTED]"
    assert not any("length" in str(node) for node in (mcp_label, client_label))


def test_config_show_reports_absent_tokens_without_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 0

    shown = yaml.safe_load(capsys.readouterr().out)
    assert shown["secrets"]["mcp_token"]["present"] is False
    assert shown["secrets"]["client_token"]["present"] is False


def test_config_show_does_not_pollute_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)
    _write_operator_dotenv(
        config_path,
        [
            "RELAY_MCP_TOKEN=t-mcp-synthetic-credential-0000000000000000",
            "RELAY_CLIENT_TOKEN=t-client-synthetic-credential-0000000000000000",
            "LOG_LEVEL=DEBUG",
        ],
    )
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "show"]) == 0

    assert "LOG_LEVEL" not in os.environ


# Public CLI uses only the default path; explicit paths remain an internal API.
def _public_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", path)
    for name in tuple(os.environ):
        if name.startswith("RELAY_"):
            monkeypatch.delenv(name)
    return path


def test_public_get_one_effective_client_key_and_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _public_path(monkeypatch, tmp_path)
    assert cli.main(["config", "get", "relay_url"]) == 0
    assert yaml.safe_load(capsys.readouterr().out) == "ws://127.0.0.1:8001/ws"
    config.init_config(path, "client", env={"RELAY_CLIENT_TOKEN": "secret-synthetic-credential-0000000000000000"})
    monkeypatch.setenv("RELAY_URL", "wss://example.test/ws?token=SECRET_MARKER&mode=live")
    assert cli.main(["config", "get", "relay_url"]) == 0
    output = capsys.readouterr().out
    assert "SECRET_MARKER" not in output and "[REDACTED]" in output
    assert "workspace" not in output
    shown = config.show_document(path)["relay_url"]["value"]
    assert yaml.safe_load(output) == shown
    assert cli.main(["config", "get", "identity.id"]) == 0
    assert yaml.safe_load(capsys.readouterr().out) == config.show_document(path)["identity"]["id"]["value"]
    assert cli.main(["config", "get", "mcp_servers"]) == 0
    assert yaml.safe_load(capsys.readouterr().out) == {}


@pytest.mark.parametrize("key", ["client_token", "mcp_token", "secrets", "secrets.client_token", "server.mcp.port", "RELAY_URL", "identity", "mcp_servers.unknown.command", "unknown"])
def test_public_get_rejects_unapproved_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], key: str
) -> None:
    _public_path(monkeypatch, tmp_path)
    assert cli.main(["config", "get", key]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "unknown client configuration key" in captured.err


def test_public_set_unset_and_token_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _public_path(monkeypatch, tmp_path)
    config.init_config(path, "client", env={"RELAY_CLIENT_TOKEN": "secret-synthetic-credential-0000000000000000"})
    assert cli.main(["config", "set", "admin", "false"]) == 0
    assert cli.main(["config", "get", "admin"]) == 0
    assert yaml.safe_load(capsys.readouterr().out.splitlines()[-1]) is False
    assert cli.main(["config", "unset", "admin"]) == 0
    assert config.show_document(path)["admin"]["value"] is False
    assert cli.main(["config", "set", "client_token", "NEVER_PRINT_MARKER"]) == 1
    assert "NEVER_PRINT_MARKER" not in capsys.readouterr().err


@pytest.mark.parametrize("scenario,expected,status", [
    ("neither", ("skipped", "skipped"), 1),
    ("server", ("valid", "skipped"), 0),
    ("client", ("skipped", "valid"), 0),
    ("both", ("valid", "valid"), 0),
    ("partial_server", ("invalid", "skipped"), 1),
    ("invalid_server_env", ("invalid", "skipped"), 1),
    ("invalid_dotenv", ("invalid", "invalid"), 1),
    ("malformed_yaml", ("skipped", "invalid"), 1),
    ("env_only_client", ("skipped", "valid"), 0),
    ("partial_client", ("skipped", "invalid"), 1),
])
def test_public_validate_detects_configured_roles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    scenario: str, expected: tuple[str, str], status: int,
) -> None:
    path = _public_path(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if scenario in {"server", "both"}:
        monkeypatch.setenv("RELAY_MCP_TOKEN", "mcp-synthetic-credential-00000000000000000000")
        monkeypatch.setenv("RELAY_CLIENT_TOKEN", "client-secret-synthetic-credential-0000000000000000")
    if scenario == "partial_server":
        monkeypatch.setenv("RELAY_SERVER_MCP_PORT", "8000")
    if scenario == "invalid_server_env":
        monkeypatch.setenv("RELAY_MCP_TOKEN", "mcp-synthetic-credential-00000000000000000000")
        monkeypatch.setenv("RELAY_CLIENT_TOKEN", "client-secret-synthetic-credential-0000000000000000")
        monkeypatch.setenv("RELAY_SERVER_MCP_PORT", "not-a-port")
    if scenario in {"client", "both"}:
        config.init_config(path, "client", workspace=workspace, env={"RELAY_CLIENT_TOKEN": "client-secret-synthetic-credential-0000000000000000"})
        monkeypatch.setenv("RELAY_CLIENT_TOKEN", "client-secret-synthetic-credential-0000000000000000")
    if scenario == "malformed_yaml":
        path.write_text("client: [invalid\n", encoding="utf-8")
        path.chmod(0o600)
    if scenario == "invalid_dotenv":
        _write_operator_dotenv(path, ["RELAY_SERVER_MCP_PORT=not-a-port"])
    if scenario in {"env_only_client", "partial_client"}:
        monkeypatch.setenv("RELAY_URL", "wss://example.test/ws")
        monkeypatch.setenv("RELAY_CLIENT_TOKEN", "client-secret-synthetic-credential-0000000000000000")
        if scenario == "env_only_client":
            monkeypatch.setenv("RELAY_CLIENT_WORKSPACE", str(workspace))
    try:
        assert cli.main(["config", "validate"]) == status
    finally:
        # The loader exports .env entries straight into the real process
        # environment (intended behavior); pop them so later tests that
        # load against os.environ stay hermetic. NOT monkeypatch.delenv:
        # the key is absent before the call, so an undo here would be a
        # no-op while the export survives fixture finalization.
        if scenario == "invalid_dotenv":
            os.environ.pop("RELAY_SERVER_MCP_PORT", None)
    output = capsys.readouterr().out
    assert "Server" in output and "Client" in output
    for result in expected:
        assert f"result={result}" in output
    assert "mcp-secret" not in output and "client-secret-synthetic-credential-0000000000000000" not in output
