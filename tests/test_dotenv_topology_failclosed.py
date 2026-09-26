"""Task3: fail-closed dotenv topology loading.

When the adjacent `.env` cannot be read cleanly (malformed line, size,
permissions, owner, unreadable) AND the file carries topology keys — the
non-secret RELAY_* names that steer binds, ports, relay URL, workspace —
startup must block with a ConfigError naming the dotenv path. A silent
tolerated read must never let topology fall back to defaults (loopback).

A tokens-only erroneous dotenv preserves the tolerant behavior: the dotenv
read error is swallowed there and the missing credentials fail naturally.
"""

import os
import re
from pathlib import Path

import pytest

from mcp_relay import config

ENV = {
    "RELAY_CLIENT_TOKEN": 'client-test-synthetic-credential-0000000000000000',
    "RELAY_MCP_TOKEN": 'mcp-test-synthetic-credential-0000000000000000',
}


def _write_dotenv(config_path: Path, contents: str, mode: int = 0o600) -> Path:
    dotenv = config_path.parent / ".env"
    dotenv.write_text(contents, encoding="utf-8")
    dotenv.chmod(mode)
    return dotenv


def _assert_error_names_dotenv(excinfo: pytest.ExceptionInfo) -> None:
    message = str(excinfo.value)
    assert ".env" in message
    assert "RELAY_MCP_TOKEN=secret-value" not in message
    assert "ws://127.0.0.1" not in message


@pytest.mark.parametrize(
    "contents",
    [
        # Bare malformed topology line (no '=').
        "RELAY_URL\n",
        # Topology keys plus a bare malformed topology line (no '=') the
        # library parser must not silently drop.
        "RELAY_URL=ws://127.0.0.1:9999/ws\nRELAY_CLIENT_WORKSPACE\n",
    ],
)
def test_malformed_topology_dotenv_blocks_client_startup(
    tmp_path: Path, contents: str
) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "client", env=ENV)
    _write_dotenv(config_path, contents)
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_client_settings(config_path, env=ENV)
    _assert_error_names_dotenv(excinfo)


def test_malformed_topology_dotenv_blocks_server_startup(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    dotenv = _write_dotenv(config_path, "RELAY_SERVER_MCP_PORT\n")
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_server_runtime(config_path, env=ENV)
    _assert_error_names_dotenv(excinfo)
    assert str(dotenv) in str(excinfo.value)


def test_oversized_topology_dotenv_blocks_startup(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "client", env=ENV)
    _write_dotenv(config_path, "RELAY_URL=ws://127.0.0.1:9999/ws\n" + "x" * 4200)
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_client_settings(config_path, env=ENV)
    _assert_error_names_dotenv(excinfo)


def test_bad_permissions_topology_dotenv_blocks_startup(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "client", env=ENV)
    _write_dotenv(
        config_path,
        "RELAY_URL=ws://127.0.0.1:9999/ws\n",
        mode=0o644,
    )
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_client_settings(config_path, env=ENV)
    _assert_error_names_dotenv(excinfo)


def test_foreign_owner_topology_dotenv_blocks_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "client", env=ENV)
    dotenv = _write_dotenv(config_path, "RELAY_CLIENT_WORKSPACE=/tmp/ws\n")
    # Simulate a foreign owner without root: make the guard's own identity
    # check disagree with the file's real (valid) owner.
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_client_settings(config_path, env=ENV)
    _assert_error_names_dotenv(excinfo)
    assert str(dotenv) in str(excinfo.value)


def test_unreadable_topology_dotenv_blocks_startup(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "client", env=ENV)
    dotenv = _write_dotenv(
        config_path, "RELAY_URL=ws://10.0.0.1:9999/ws\n", mode=0o000
    )
    try:
        with pytest.raises(config.ConfigError) as excinfo:
            config.load_client_settings(config_path, env=ENV)
    finally:
        dotenv.chmod(0o600)
    _assert_error_names_dotenv(excinfo)
    assert str(dotenv) in str(excinfo.value)


def test_topology_dotenv_error_never_falls_back_to_loopback(tmp_path: Path) -> None:
    """A broken topology dotenv must not yield default loopback settings."""
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "client", env=ENV)
    config_path.unlink()  # force the env-only path; dotenv had RELAY_URL
    _write_dotenv(
        config_path,
        "RELAY_URL=ws://10.0.0.1:9999/ws\nRELAY_CLIENT_WORKSPACE=/tmp/ws\nbroken\n",
    )
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_client_settings(config_path, env=ENV)
    message = str(excinfo.value)
    assert ".env" in message
    assert re.search(r"10\.0\.0\.1|ws://", message) is None or ".env" in message


@pytest.mark.parametrize(
    "contents",
    [
        "RELAY_MCP_TOKEN\n",
        "RELAY_CLIENT_TOKEN\n",
        "RELAY_MCP_TOKEN=" + "t" * 4200 + "\n",
    ],
)
def test_tokens_only_erroneous_dotenv_preserves_tolerant_behavior(
    tmp_path: Path, contents: str
) -> None:
    """Credentials-only dotenv errors stay tolerated; tokens come from env."""
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "client", env=ENV)
    _write_dotenv(config_path, contents)
    settings = config.load_client_settings(config_path, env=ENV)
    assert settings is not None
    runtime = config.load_server_runtime(config_path, env=ENV)
    assert runtime.settings.mcp_bind_host == "127.0.0.1"
