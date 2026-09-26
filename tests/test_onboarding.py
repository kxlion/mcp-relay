from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
import yaml

from mcp_relay import cli, config

SERVER_TOPOLOGY_KEYS = (
    "RELAY_SERVER_MCP_HOST",
    "RELAY_SERVER_MCP_PORT",
    "RELAY_SERVER_CLIENT_HOST",
    "RELAY_SERVER_CLIENT_PORT",
)


def _env_token(
    monkeypatch: pytest.MonkeyPatch,
    value: str = "remote-client-secret-synthetic-credential-0000000000000000",
) -> None:
    monkeypatch.setenv("RELAY_CLIENT_TOKEN", value)


def _mcp_env_token(
    monkeypatch: pytest.MonkeyPatch,
    value: str = "remote-mcp-secret-synthetic-credential-0000000000000000",
) -> None:
    monkeypatch.setenv("RELAY_MCP_TOKEN", value)


def _dotenv(config_path: Path) -> Path:
    return config_path.parent / ".env"


def _dotenv_values(config_path: Path) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in _dotenv(config_path).read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }


def _operator_dotenv(config_path: Path, extra: str = "") -> Path:
    """Simulate the operator providing credentials before onboarding."""
    dotenv_path = _dotenv(config_path)
    dotenv_path.parent.mkdir(parents=True, exist_ok=True)
    dotenv_path.write_text(
        "RELAY_MCP_TOKEN=operator-mcp-secret-synthetic-credential-0000000000000000\n"
        "RELAY_CLIENT_TOKEN=operator-client-secret-synthetic-credential-0000000000000000\n"
        + extra,
        encoding="utf-8",
    )
    if os.name != "nt":
        dotenv_path.chmod(0o600)
    return dotenv_path


# --------------------------------------------------------------------------
# Topology-to-dotenv merge
# --------------------------------------------------------------------------


def test_server_onboarding_writes_topology_to_dotenv_without_creating_yaml(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.yaml"
    _operator_dotenv(config_path)

    assert (
        cli.run_onboarding(
            config_path,
            cli.OnboardingOptions(role="server", non_interactive=True, topology="lan"),
        )
        == 0
    )

    assert not config_path.exists()
    dotenv_path = _dotenv(config_path)
    assert dotenv_path.is_file()
    assert (dotenv_path.stat().st_mode & 0o777) == 0o600
    values = _dotenv_values(config_path)
    assert values["RELAY_SERVER_MCP_HOST"] == "0.0.0.0"
    assert values["RELAY_SERVER_MCP_PORT"] == "8000"
    assert values["RELAY_SERVER_CLIENT_HOST"] == "0.0.0.0"
    assert values["RELAY_SERVER_CLIENT_PORT"] == "8001"
    # Tokens are never touched by the topology merge.
    assert values["RELAY_MCP_TOKEN"] == 'operator-mcp-secret-synthetic-credential-0000000000000000'
    assert values["RELAY_CLIENT_TOKEN"] == 'operator-client-secret-synthetic-credential-0000000000000000'
    output = capsys.readouterr()
    assert "result=valid" in output.out
    assert 'operator-mcp-secret-synthetic-credential-0000000000000000' not in output.out
    assert 'operator-client-secret-synthetic-credential-0000000000000000' not in output.out


def test_server_topology_merge_preserves_manual_topology_and_unrelated_entries(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    _operator_dotenv(
        config_path,
        extra="RELAY_SERVER_MCP_PORT=9100\nUNRELATED_OPERATOR_KEY=keep\n",
    )

    assert (
        cli.run_onboarding(
            config_path,
            cli.OnboardingOptions(role="server", non_interactive=True, topology="lan"),
        )
        == 0
    )

    values = _dotenv_values(config_path)
    # Manually supplied topology survives a merge without forced overwrite.
    assert values["RELAY_SERVER_MCP_PORT"] == "9100"
    assert values["RELAY_SERVER_MCP_HOST"] == "0.0.0.0"
    assert values["UNRELATED_OPERATOR_KEY"] == "keep"
    assert values["RELAY_CLIENT_TOKEN"] == 'operator-client-secret-synthetic-credential-0000000000000000'


def test_server_onboarding_force_overwrites_topology_but_never_tokens(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    _operator_dotenv(
        config_path,
        extra="RELAY_SERVER_MCP_PORT=9100\nUNRELATED_OPERATOR_KEY=keep\n",
    )
    options = cli.OnboardingOptions(role="server", non_interactive=True, topology="lan")
    assert cli.run_onboarding(config_path, options) == 0

    assert (
        cli.run_onboarding(
            config_path,
            cli.OnboardingOptions(
                role="server", non_interactive=True, topology="lan", force=True
            ),
        )
        == 0
    )

    values = _dotenv_values(config_path)
    assert values["RELAY_SERVER_MCP_PORT"] == "8000"
    assert values["RELAY_SERVER_MCP_HOST"] == "0.0.0.0"
    # Forced overwrite is bounded to the onboarding topology keys.
    assert values["UNRELATED_OPERATOR_KEY"] == "keep"
    assert values["RELAY_MCP_TOKEN"] == 'operator-mcp-secret-synthetic-credential-0000000000000000'
    assert values["RELAY_CLIENT_TOKEN"] == 'operator-client-secret-synthetic-credential-0000000000000000'


def test_merge_helper_refuses_credential_keys(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    dotenv_path = _operator_dotenv(config_path)
    with pytest.raises(config.ConfigError):
        config.merge_dotenv_values(config_path, {"RELAY_CLIENT_TOKEN": "x"})
    assert 'RELAY_CLIENT_TOKEN=operator-client-secret-synthetic-credential-0000000000000000' in dotenv_path.read_text(
        encoding="utf-8"
    )


def test_local_onboarding_flattens_yaml_to_client_only_and_merges_dotenv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    _operator_dotenv(config_path)

    assert (
        cli.run_onboarding(
            config_path,
            cli.OnboardingOptions(role="local", non_interactive=True, topology="local"),
        )
        == 0
    )

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    # Flat client YAML: no server section, no client: prefix, no topology key.
    assert "server" not in document
    assert "topology" not in document
    assert set(document) <= set(config.ClientConfig.model_fields)
    assert document["relay_url"] == "ws://127.0.0.1:8001/ws"
    values = _dotenv_values(config_path)
    for key in SERVER_TOPOLOGY_KEYS:
        assert key in values
    assert values["RELAY_CLIENT_TOKEN"] == 'operator-client-secret-synthetic-credential-0000000000000000'
    assert values["RELAY_MCP_TOKEN"] == 'operator-mcp-secret-synthetic-credential-0000000000000000'
    output = capsys.readouterr()
    client_secret = values["RELAY_CLIENT_TOKEN"]
    mcp_secret = values["RELAY_MCP_TOKEN"]
    assert client_secret not in output.out
    assert mcp_secret not in output.out
    assert "result=valid" in output.out


# --------------------------------------------------------------------------
# Effective client port -> relay_url derivation (env > .env > default 8001)
# --------------------------------------------------------------------------


def test_local_onboarding_derives_the_client_url_from_the_effective_client_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_token(monkeypatch)
    _mcp_env_token(monkeypatch)
    config_path = tmp_path / "config.yaml"

    assert (
        cli.run_onboarding(
            config_path,
            cli.OnboardingOptions(
                role="local",
                non_interactive=True,
                topology="local",
                mcp_port="9100",
                client_port="9200",
            ),
        )
        == 0
    )

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    values = _dotenv_values(config_path)
    assert values["RELAY_SERVER_MCP_PORT"] == "9100"
    assert values["RELAY_SERVER_CLIENT_PORT"] == "9200"
    # The MCP port never leaks into the Client URL.
    assert document["relay_url"] == "ws://127.0.0.1:9200/ws"


def test_local_onboarding_effective_client_port_prefers_environment_over_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_token(monkeypatch)
    _mcp_env_token(monkeypatch)
    monkeypatch.setenv("RELAY_SERVER_CLIENT_PORT", "9300")
    config_path = tmp_path / "config.yaml"

    assert (
        cli.run_onboarding(
            config_path,
            cli.OnboardingOptions(
                role="local", non_interactive=True, topology="local", client_port="9200"
            ),
        )
        == 0
    )

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    # Environment wins over the .env value just written.
    assert document["relay_url"] == "ws://127.0.0.1:9300/ws"


def test_client_onboarding_local_topology_defaults_to_the_effective_client_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_token(monkeypatch)
    config_path = tmp_path / "config.yaml"
    workspace = tmp_path / "client-workspace"
    _operator_dotenv(config_path, extra="RELAY_SERVER_CLIENT_PORT=9400\n")

    assert (
        cli.run_onboarding(
            config_path,
            cli.OnboardingOptions(
                role="client",
                non_interactive=True,
                topology="local",
                workspace=str(workspace),
                check=False,
            ),
        )
        == 0
    )

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["relay_url"] == "ws://127.0.0.1:9400/ws"


def test_ipv6_client_host_is_bracketed_in_the_derived_relay_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_token(monkeypatch)
    _mcp_env_token(monkeypatch)
    config_path = tmp_path / "config.yaml"

    assert (
        cli.run_onboarding(
            config_path,
            cli.OnboardingOptions(
                role="local",
                non_interactive=True,
                topology="local",
                client_host="::1",
                client_port="9200",
            ),
        )
        == 0
    )

    values = _dotenv_values(config_path)
    assert values["RELAY_SERVER_CLIENT_HOST"] == "::1"
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["relay_url"] == "ws://[::1]:9200/ws"


def test_onboarding_rejects_identical_mcp_and_client_ports(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    _operator_dotenv(config_path)

    with pytest.raises(config.ConfigError, match="must be distinct"):
        cli.run_onboarding(
            config_path,
            cli.OnboardingOptions(
                role="local",
                non_interactive=True,
                topology="local",
                mcp_port="8000",
                client_port="8000",
            ),
        )


# --------------------------------------------------------------------------
# Client role contract
# --------------------------------------------------------------------------


def test_remote_client_onboarding_masks_env_secret_and_requires_wss(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    _env_token(monkeypatch)
    workspace = tmp_path / "client-workspace"

    assert (
        cli.run_onboarding(
            config_path,
            cli.OnboardingOptions(
                role="client",
                non_interactive=True,
                topology="remote",
                relay_url="wss://relay.example.test/ws?ignored=secret",
                workspace=str(workspace),
                check=False,
            ),
        )
        == 0
    )

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["relay_url"] == "wss://relay.example.test/ws?ignored=secret"
    assert document["workspace"] == str(workspace)
    assert "topology" not in document
    assert 'remote-client-secret-synthetic-credential-0000000000000000' not in capsys.readouterr().out
    # The token came from the environment: it stays there, never copied to disk.
    dotenv_file = _dotenv(config_path)
    assert not dotenv_file.exists() or (
        "RELAY_CLIENT_TOKEN" not in dotenv_file.read_text(encoding="utf-8")
    )


def test_remote_client_onboarding_rejects_plaintext_remote_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_token(monkeypatch)
    with pytest.raises(
        config.ConfigError, match="remote topology requires a wss:// relay URL"
    ):
        cli.run_onboarding(
            tmp_path / "config.yaml",
            cli.OnboardingOptions(
                role="client",
                non_interactive=True,
                topology="remote",
                relay_url="ws://relay.example.test/ws",
            ),
        )


def test_explicit_lan_relay_url_is_never_rewritten_to_the_effective_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_token(monkeypatch)
    _mcp_env_token(monkeypatch)
    config_path = tmp_path / "config.yaml"
    workspace = tmp_path / "client-workspace"

    assert (
        cli.run_onboarding(
            config_path,
            cli.OnboardingOptions(
                role="client",
                non_interactive=True,
                topology="lan",
                relay_url="ws://192.168.1.20:8000/ws",
                workspace=str(workspace),
                check=False,
            ),
        )
        == 0
    )

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["relay_url"] == "ws://192.168.1.20:8000/ws"


def test_client_only_default_topology_is_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    _env_token(monkeypatch)
    assert (
        cli.run_onboarding(
            config_path,
            cli.OnboardingOptions(
                role="client",
                non_interactive=True,
                topology="remote",
                relay_url="wss://relay.example.test/ws",
            ),
        )
        == 0
    )
    settings = config.load_client_settings(config_path)
    assert settings.server_url == "wss://relay.example.test/ws"


# --------------------------------------------------------------------------
# Role/topology guards and interaction
# --------------------------------------------------------------------------


def test_local_role_rejects_a_non_local_topology(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    with pytest.raises(config.ConfigError, match="local role requires local topology"):
        cli.run_onboarding(
            config_path,
            cli.OnboardingOptions(role="local", non_interactive=True, topology="lan"),
        )
    assert not config_path.exists()


def test_local_role_fails_fast_on_explicit_non_local_topology_without_prompting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    config_path = tmp_path / "config.yaml"

    with pytest.raises(config.ConfigError, match="local role requires local topology"):
        cli.run_onboarding(
            config_path, cli.OnboardingOptions(role="local", topology="lan")
        )
    output = capsys.readouterr()
    assert "Choose a topology" not in output.out
    assert "Choose a setup" not in output.out
    assert not config_path.exists()


def test_interactive_role_selection_uses_safe_server_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin = io.StringIO("2\n\n\n\n\n")
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr("sys.stdin", stdin)
    config_path = tmp_path / "config.yaml"
    _operator_dotenv(config_path)

    monkeypatch.setattr(cli.config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["onboard"]) == 0

    values = _dotenv_values(config_path)
    assert values["RELAY_SERVER_MCP_HOST"] == "127.0.0.1"
    assert values["RELAY_SERVER_CLIENT_PORT"] == "8001"
    assert "Server only" in capsys.readouterr().out


def test_interactive_local_onboarding_never_prompts_for_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin = io.StringIO("1\n\n\n\n\n")
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr("sys.stdin", stdin)
    config_path = tmp_path / "config.yaml"
    _operator_dotenv(config_path)

    monkeypatch.setattr(cli.config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["onboard"]) == 0

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["relay_url"] == "ws://127.0.0.1:8001/ws"
    output = capsys.readouterr()
    assert "Choose a topology" not in output.out
    assert "Deployment topology: local" in output.out


def test_onboarding_cancellation_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin = io.StringIO("")
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr("sys.stdin", stdin)

    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(cli.config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["onboard"]) == 1
    assert "onboarding cancelled" in capsys.readouterr().err


def test_noninteractive_onboarding_requires_a_role(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="non-interactive onboarding requires"):
        cli.run_onboarding(
            tmp_path / "config.yaml", cli.OnboardingOptions(non_interactive=True)
        )


def test_server_onboarding_dotenv_startup_reload_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The written dotenv must reload through the runtime loader."""
    monkeypatch.setenv("RELAY_MCP_TOKEN", 'env-mcp-secret-synthetic-credential-0000000000000000')
    monkeypatch.setenv("RELAY_CLIENT_TOKEN", 'env-client-secret-synthetic-credential-0000000000000000')
    config_path = tmp_path / "config.yaml"

    assert (
        cli.run_onboarding(
            config_path,
            cli.OnboardingOptions(
                role="server", non_interactive=True, topology="lan", client_port="9200"
            ),
        )
        == 0
    )

    runtime = config.load_server_runtime(config_path, env=dict(os.environ))
    assert runtime.client_port == 9200
    assert runtime.mcp_bind_host == "0.0.0.0"


def test_client_requires_operator_token_before_yaml_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    monkeypatch.delenv("RELAY_CLIENT_TOKEN", raising=False)
    stdin = io.StringIO("3\n3\nwss://relay.example.test/ws\n\n")
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr("sys.stdin", stdin)
    with pytest.raises(config.ConfigError, match=r"RELAY_CLIENT_TOKEN|\.env"):
        cli.run_onboarding(path)
    assert not path.exists()
    assert not _dotenv(path).exists()


def test_client_existing_dotenv_token_is_not_replaced_or_claimed_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.yaml"
    _operator_dotenv(path)
    monkeypatch.delenv("RELAY_CLIENT_TOKEN", raising=False)
    stdin = io.StringIO("3\n3\nwss://relay.example.test/ws\n\n\nn\n")
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr("sys.stdin", stdin)
    assert cli.run_onboarding(path) == 0
    assert (
        config.read_dotenv_values(path)["RELAY_CLIENT_TOKEN"]
        == "operator-client-secret-synthetic-credential-0000000000000000"
    )
    assert "credential stored" not in capsys.readouterr().out


def test_local_missing_token_leaves_no_yaml_or_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    monkeypatch.delenv("RELAY_CLIENT_TOKEN", raising=False)
    with pytest.raises(config.ConfigError, match="RELAY_CLIENT_TOKEN"):
        cli.run_onboarding(
            path, cli.OnboardingOptions(role="local", non_interactive=True)
        )
    assert not path.exists()
    assert not _dotenv(path).exists()


def test_empty_environment_token_never_falls_back_to_dotenv_or_writes_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    _operator_dotenv(path)
    monkeypatch.setenv("RELAY_CLIENT_TOKEN", "")
    with pytest.raises(config.ConfigError, match="token is empty"):
        cli.run_onboarding(
            path,
            cli.OnboardingOptions(
                role="client",
                non_interactive=True,
                topology="remote",
                relay_url="wss://relay.example.test/ws",
            ),
        )
    assert not path.exists()
    assert (
        config.read_dotenv_values(path)["RELAY_CLIENT_TOKEN"]
        == "operator-client-secret-synthetic-credential-0000000000000000"
    )


def test_onboarding_without_tty_fails_with_manual_setup_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("1\n"))
    with pytest.raises(config.ConfigError, match="interactive terminal|manual"):
        cli.run_onboarding(tmp_path / "config.yaml")
