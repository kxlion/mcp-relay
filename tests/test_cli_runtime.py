from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_relay import cli, config

_HOST = "127.0.0.1"


def test_server_module_entrypoint_exposes_help_and_rejects_equal_ports(
    tmp_path: Path,
) -> None:
    help_result = subprocess.run(
        [sys.executable, "-m", "mcp_relay.server", "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert help_result.returncode == 0
    assert "--config" in help_result.stdout

    environment = os.environ.copy()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind((_HOST, 0))
        blocker.listen()
        port = blocker.getsockname()[1]
    environment.update(
        {
            "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
            "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
            "RELAY_SERVER_MCP_HOST": _HOST,
            "RELAY_SERVER_MCP_PORT": str(port),
            "RELAY_SERVER_CLIENT_HOST": _HOST,
            "RELAY_SERVER_CLIENT_PORT": str(port),
        }
    )
    equal_port_result = subprocess.run(
        [sys.executable, "-m", "mcp_relay.server"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert equal_port_result.returncode != 0
    assert "invalid relay server configuration" in equal_port_result.stderr
    assert "listener" not in equal_port_result.stderr.lower()


def test_server_and_client_are_the_only_runtime_dispatch_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    received_server: list[list[str] | None] = []
    received_client: list[list[str] | None] = []
    monkeypatch.setattr(
        cli.server,
        "main",
        lambda argv=None: received_server.append(argv),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        cli.client,
        "main",
        lambda argv=None, **_kwargs: received_client.append(argv),
    )
    monkeypatch.setattr(
        cli.config,
        "load_server_runtime",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        cli.config,
        "load_client_settings",
        lambda *_args, **_kwargs: object(),
    )

    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", path)
    assert cli.main(["server"]) == 0
    assert cli.main(["client"]) == 0
    assert received_server == [["--config", str(path)]]
    assert received_client == [["--config", str(path)]]


def test_client_can_use_runtime_environment_without_default_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    received_client: list[list[str] | None] = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(cli.config, "DEFAULT_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(
        cli.client,
        "main",
        lambda argv=None, **_kwargs: received_client.append(argv),
    )
    monkeypatch.setenv("RELAY_URL", "ws://127.0.0.1:8000/ws")
    monkeypatch.setenv("RELAY_CLIENT_WORKSPACE", str(workspace))
    monkeypatch.setenv("RELAY_CLIENT_TOKEN", 'client-token-synthetic-credential-0000000000000000')

    assert cli.main(["client"]) == 0
    assert received_client == [[]]


def test_start_commands_return_configuration_error_status_for_missing_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "missing.yaml"
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["server"]) == 1
    assert cli.main(["client"]) == 1
    assert "error" in capsys.readouterr().err.lower()


def test_server_start_prints_sanitized_validation_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = 'SHARED_TOKEN_SENTINEL-synthetic-credential-0000000000000000'
    monkeypatch.setenv("RELAY_MCP_TOKEN", secret)
    monkeypatch.setenv("RELAY_CLIENT_TOKEN", secret)

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", tmp_path / "missing.yaml")
    assert cli.main(["server"]) == 1

    stderr = capsys.readouterr().err
    assert "invalid relay server configuration" in stderr
    assert secret not in stderr


def test_client_runtime_can_start_with_zero_selected_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("client-secret-synthetic-credential-0000000000000000\n"),
    )
    config.init_config(config_path, "client", token="client-secret-synthetic-credential-0000000000000000", env={})

    from mcp_relay.client import RelayClient
    from mcp_relay.config import load_client_settings

    settings = load_client_settings(config_path, env={"RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000'})
    relay_client = RelayClient(settings)
    assert relay_client._capabilities == {}


# --------------------------------------------------------------------------
# Per-listener runtime plumbing (env-only server topology)
# --------------------------------------------------------------------------


def test_server_main_honors_the_client_port_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
        "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
        "RELAY_SERVER_CLIENT_PORT": "9200",
    }
    monkeypatch.setattr("mcp_relay.server.os.environ", environment)
    observed: dict[str, object] = {}

    def fake_run(settings: object) -> None:
        observed.update(settings=settings)

    monkeypatch.setattr("mcp_relay.server._run_relay", fake_run)
    from mcp_relay.server import main as server_main

    server_main([])

    assert getattr(observed["settings"], "client_port") == 9200


def test_server_main_rejects_identical_mcp_and_client_ports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = {
        "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
        "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
        "RELAY_SERVER_MCP_PORT": "8000",
        "RELAY_SERVER_CLIENT_PORT": "8000",
    }
    monkeypatch.setattr("mcp_relay.server.os.environ", environment)
    from mcp_relay.server import main as server_main

    with pytest.raises(SystemExit):
        server_main([])
    assert "invalid relay server configuration" in capsys.readouterr().err


def test_server_module_entrypoint_exposes_per_listener_bind_flags(
    tmp_path: Path,
) -> None:
    help_result = subprocess.run(
        [sys.executable, "-m", "mcp_relay.server", "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert help_result.returncode == 0
    for flag in ("--mcp-host", "--mcp-port", "--client-host", "--client-port"):
        assert flag in help_result.stdout
    for legacy in ("--host", "--port", "--ws-port"):
        assert legacy not in help_result.stdout


def test_server_main_applies_explicit_bind_flags_over_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
        "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
        "RELAY_SERVER_MCP_PORT": "9000",
    }
    monkeypatch.setattr("mcp_relay.server.os.environ", environment)
    observed: dict[str, object] = {}

    def fake_run(settings: object) -> None:
        observed.update(settings=settings)

    monkeypatch.setattr("mcp_relay.server._run_relay", fake_run)
    from mcp_relay.server import main as server_main

    server_main(
        [
            "--mcp-host",
            "0.0.0.0",
            "--mcp-port",
            "8080",
            "--client-host",
            "127.0.0.1",
            "--client-port",
            "8081",
        ]
    )

    settings = observed["settings"]
    assert getattr(settings, "mcp_bind_host") == "0.0.0.0"
    assert getattr(settings, "mcp_port") == 8080
    assert getattr(settings, "client_bind_host") == "127.0.0.1"
    assert getattr(settings, "client_port") == 8081


def test_server_main_rejects_bind_flag_combined_with_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_relay.server import main as server_main

    with pytest.raises(SystemExit) as excinfo:
        server_main(["--config", "relay.yaml", "--mcp-port", "8000"])

    assert excinfo.value.code == 2
    stderr = capsys.readouterr().err
    assert "--mcp-port" in stderr
    assert "--config" in stderr


def test_server_main_rejects_bind_flag_equal_to_default_with_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_relay.server import main as server_main

    with pytest.raises(SystemExit) as excinfo:
        server_main(
            ["--config", "relay.yaml", "--mcp-host", "127.0.0.1", "--client-port", "8001"]
        )

    assert excinfo.value.code == 2
    stderr = capsys.readouterr().err
    assert "--mcp-host" in stderr
    assert "--client-port" in stderr
