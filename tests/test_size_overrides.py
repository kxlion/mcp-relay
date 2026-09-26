"""Runtime RELAY_MAX_* overrides through the single dotenv pipeline.

Overrides live in one mechanism: ``config._apply_dotenv_environment``
exports the .env (shell wins) into ``os.environ``, then resolves the
RELAY_MAX_* bounds into ``json_bounds`` module globals. The resolution
runs inside ``load_client_settings`` / ``load_server_runtime`` — there is
no package-import hook (the removed import-time application made .env
overrides dead, observed on the Windows bench 2026-09-09).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import mcp_relay.json_bounds as jb
from mcp_relay import config


def _server_config(tmp_path: Path) -> tuple[Path, Path]:
    # Server settings are environment-only: there is no server YAML surface,
    # so the test plays the operator by creating the dotenv directly.
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    dotenv = config_path.parent / ".env"
    dotenv.write_text(
        'RELAY_MCP_TOKEN=test-mcp-token-synthetic-credential-0000000000000000\nRELAY_CLIENT_TOKEN=test-client-token-synthetic-credential-0000000000000000\n',
        encoding="utf-8",
    )
    if os.name != "nt":
        dotenv.chmod(0o600)
    return config_path, dotenv


# ---------------------------------------------------------------------------
# Runtime resolution through the config loaders
# ---------------------------------------------------------------------------


def test_env_file_override_applied_at_load(tmp_path: Path) -> None:
    config_path, dotenv = _server_config(tmp_path)
    dotenv.write_text(
        dotenv.read_text(encoding="utf-8")
        + "RELAY_MAX_TOOL_RESULT_BYTES=1048576\n"
        + "RELAY_MAX_WS_MESSAGE_BYTES=2097152\n",
        encoding="utf-8",
    )
    config.load_server_runtime(config_path, env={})
    assert jb.MAX_TOOL_RESULT_BYTES == 1048576
    assert jb.MAX_WS_MESSAGE_BYTES == 2097152
    assert jb.APPLIED_SIZE_OVERRIDES == {
        "MAX_TOOL_RESULT_BYTES": 1048576,
        "MAX_WS_MESSAGE_BYTES": 2097152,
    }


def test_shell_env_wins_over_dotenv_override_at_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, dotenv = _server_config(tmp_path)
    dotenv.write_text(
        dotenv.read_text(encoding="utf-8")
        + "RELAY_MAX_TOOL_RESULT_BYTES=1048576\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RELAY_MAX_TOOL_RESULT_BYTES", "1572864")

    config.load_server_runtime(config_path)

    assert jb.MAX_TOOL_RESULT_BYTES == 1572864


def test_injected_environment_drives_override_resolution(tmp_path: Path) -> None:
    config_path, _ = _server_config(tmp_path)

    config.load_server_runtime(
        config_path,
        env={
            "RELAY_MCP_TOKEN": 'injected-mcp-token-synthetic-credential-0000000000000000',
            "RELAY_CLIENT_TOKEN": 'injected-client-token-synthetic-credential-0000000000000000',
            "RELAY_MAX_TOOL_RESULT_BYTES": "1048576",
            "RELAY_MAX_WS_MESSAGE_BYTES": "2097152",
        },
    )

    assert jb.MAX_TOOL_RESULT_BYTES == 1048576
    assert jb.MAX_WS_MESSAGE_BYTES == 2097152


def test_invalid_dotenv_does_not_skip_valid_shell_override_resolution(
    tmp_path: Path,
) -> None:
    config_path, dotenv = _server_config(tmp_path)
    dotenv.write_text("NOT_A_VALID_LINE\n", encoding="utf-8")

    config.load_server_runtime(
        config_path,
        env={
            "RELAY_MCP_TOKEN": 'shell-mcp-token-synthetic-credential-0000000000000000',
            "RELAY_CLIENT_TOKEN": 'shell-client-token-synthetic-credential-0000000000000000',
            "RELAY_MAX_TOOL_RESULT_BYTES": "1048576",
            "RELAY_MAX_WS_MESSAGE_BYTES": "2097152",
        },
    )

    assert jb.MAX_TOOL_RESULT_BYTES == 1048576
    assert jb.MAX_WS_MESSAGE_BYTES == 2097152


def test_client_loader_applies_overrides_too(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    dotenv = config_path.parent / ".env"
    # The .env must exist before init: init_config verifies the client token
    # is resolvable from the environment or the .env next to the config.
    dotenv.write_text(
        'RELAY_CLIENT_TOKEN=test-client-token-synthetic-credential-0000000000000000\n'
        "RELAY_MAX_TOOL_RESULT_BYTES=1048576\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        dotenv.chmod(0o600)
    config.init_config(config_path, "client", env={})
    config.load_client_settings(config_path, env={})
    assert jb.MAX_TOOL_RESULT_BYTES == 1048576


def test_no_override_leaves_calibrated_defaults(tmp_path: Path) -> None:
    config_path, _ = _server_config(tmp_path)
    config.load_server_runtime(config_path, env={})
    assert jb.MAX_TOOL_RESULT_BYTES == jb._DEFAULT_BOUNDS["MAX_TOOL_RESULT_BYTES"]
    assert jb.MAX_RESULT_NODES == jb._DEFAULT_BOUNDS["MAX_RESULT_NODES"]
    assert jb.MAX_WS_MESSAGE_BYTES == jb._DEFAULT_BOUNDS["MAX_WS_MESSAGE_BYTES"]
    assert jb.APPLIED_SIZE_OVERRIDES == {}


def test_node_override_applied_at_load(tmp_path: Path) -> None:
    config_path, dotenv = _server_config(tmp_path)
    dotenv.write_text(
        dotenv.read_text(encoding="utf-8") + "RELAY_MAX_RESULT_NODES=65536\n",
        encoding="utf-8",
    )
    config.load_server_runtime(config_path, env={})
    assert jb.MAX_RESULT_NODES == 65536


# ---------------------------------------------------------------------------
# Clamping and logging (unchanged contract, new entry point)
# ---------------------------------------------------------------------------


def test_result_override_above_ceiling_fails_startup(
    tmp_path: Path,
) -> None:
    # The declared bound schema rejects out-of-range overrides: 1 GiB is
    # above the 16 MiB ceiling, so startup refuses instead of clamping.
    config_path, dotenv = _server_config(tmp_path)
    dotenv.write_text(
        dotenv.read_text(encoding="utf-8")
        + f"RELAY_MAX_TOOL_RESULT_BYTES={1024 * 1024 * 1024}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="less than or equal to"):
        config.load_server_runtime(config_path, env={})


def test_incoherent_override_chain_aborts_startup(tmp_path: Path) -> None:
    # A result at or above the frame size breaks the chain invariant:
    # startup refuses, naming the conflicting variables.
    config_path, dotenv = _server_config(tmp_path)
    dotenv.write_text(
        dotenv.read_text(encoding="utf-8")
        + f"RELAY_MAX_TOOL_RESULT_BYTES={4 * 1024 * 1024}\n"
        + f"RELAY_MAX_WS_MESSAGE_BYTES={2 * 1024 * 1024}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="RELAY_MAX_TOOL_RESULT_BYTES"):
        config.load_server_runtime(config_path, env={})


def test_override_chain_reserves_the_complete_client_result_envelope() -> None:
    with pytest.raises(ValueError, match="envelope"):
        jb.resolve_size_overrides(
            {
                "RELAY_MAX_TOOL_RESULT_BYTES": "65536",
                "RELAY_MAX_WS_MESSAGE_BYTES": "65537",
            }
        )


def test_non_integer_override_fails_startup(tmp_path: Path) -> None:
    config_path, dotenv = _server_config(tmp_path)
    dotenv.write_text(
        dotenv.read_text(encoding="utf-8") + "RELAY_MAX_RESULT_NODES=big\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_result_nodes"):
        config.load_server_runtime(config_path, env={})


def test_successive_resolutions_replace_the_complete_bound_snapshot() -> None:
    jb.resolve_size_overrides(
        {
            "RELAY_MAX_TOOL_RESULT_BYTES": "1048576",
            "RELAY_MAX_WS_MESSAGE_BYTES": "2097152",
        }
    )
    assert jb.APPLIED_SIZE_OVERRIDES == {
        "MAX_TOOL_RESULT_BYTES": 1048576,
        "MAX_WS_MESSAGE_BYTES": 2097152,
    }

    jb.resolve_size_overrides({"RELAY_MAX_RESULT_NODES": "32768"})
    assert jb.MAX_TOOL_RESULT_BYTES == jb._DEFAULT_BOUNDS["MAX_TOOL_RESULT_BYTES"]
    assert jb.MAX_RESULT_NODES == 32768
    assert jb.MAX_WS_MESSAGE_BYTES == jb._DEFAULT_BOUNDS["MAX_WS_MESSAGE_BYTES"]
    assert jb.APPLIED_SIZE_OVERRIDES == {"MAX_RESULT_NODES": 32768}

    jb.resolve_size_overrides({})
    assert {
        name: getattr(jb, name) for name in jb._DEFAULT_BOUNDS
    } == jb._DEFAULT_BOUNDS
    assert jb.APPLIED_SIZE_OVERRIDES == {}


def test_failed_resolution_does_not_mutate_the_current_bound_snapshot() -> None:
    jb.resolve_size_overrides(
        {
            "RELAY_MAX_TOOL_RESULT_BYTES": "1048576",
            "RELAY_MAX_RESULT_NODES": "32768",
            "RELAY_MAX_WS_MESSAGE_BYTES": "2097152",
        }
    )
    before_bounds = {name: getattr(jb, name) for name in jb._DEFAULT_BOUNDS}
    before_applied = dict(jb.APPLIED_SIZE_OVERRIDES)

    with pytest.raises(ValueError, match="RELAY_MAX_TOOL_RESULT_BYTES"):
        jb.resolve_size_overrides(
            {
                "RELAY_MAX_TOOL_RESULT_BYTES": "4194304",
                "RELAY_MAX_WS_MESSAGE_BYTES": "2097152",
            }
        )

    assert {name: getattr(jb, name) for name in jb._DEFAULT_BOUNDS} == before_bounds
    assert jb.APPLIED_SIZE_OVERRIDES == before_applied


# ---------------------------------------------------------------------------
# Honest result_too_large diagnostics (measured vs bound)
# ---------------------------------------------------------------------------


def test_oversized_result_names_bound_and_payload_size() -> None:
    """Refusal diagnostics carry measured payload size and the binding bound."""
    from mcp_relay.providers.base import ProviderResultTooLargeError, bounded_result

    saved = jb.MAX_TOOL_RESULT_BYTES
    try:
        jb.MAX_TOOL_RESULT_BYTES = 64
        payload = {"content": [{"type": "text", "text": "x" * 200}]}
        with pytest.raises(ProviderResultTooLargeError) as too_large:
            bounded_result(payload)
        detail = str(too_large.value)
        assert "RELAY_MAX_TOOL_RESULT_BYTES: 64" in detail
        assert "payload: " in detail
        assert "bytes" in detail
    finally:
        jb.MAX_TOOL_RESULT_BYTES = saved


def test_oversized_nodes_report_a_traversal_lower_bound() -> None:
    payload = list(range(22))  # 23 nodes including the root list

    with pytest.raises(jb.JsonBoundsError) as too_large:
        jb.validate_json_bounds(
            payload,
            max_nodes=5,
            max_bytes=4096,
            max_nodes_env="RELAY_MAX_RESULT_NODES",
        )

    assert str(too_large.value) == (
        "RELAY_MAX_RESULT_NODES: 5 < payload: at least 6 nodes"
    )


def test_oversized_nodes_names_node_bound(tmp_path: Path) -> None:
    """A node refusal names its bound and the safe traversal lower bound."""
    from mcp_relay.providers.base import ProviderResultTooLargeError, bounded_result

    payload: dict[str, object] = {"value": 1}
    for _ in range(20):  # beyond any sane node bound
        payload = {"nested": payload}

    saved = jb.MAX_RESULT_NODES
    try:
        jb.MAX_RESULT_NODES = 16
        with pytest.raises(ProviderResultTooLargeError) as too_large:
            bounded_result({"content": [], **payload})
        detail = str(too_large.value)
        assert detail == (
            "RELAY_MAX_RESULT_NODES: 16 < payload: at least 17 nodes"
        )
    finally:
        jb.MAX_RESULT_NODES = saved
