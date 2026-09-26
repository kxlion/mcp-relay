from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from mcp_relay.config import (
    RESERVED_MCP_ALIASES,
    ClientConfig,
    configuration_keys,
)


def test_client_model_rejects_a_tools_key_in_any_shape() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClientConfig.model_validate({"tools": {"allowlist": ["relay_server_status"]}})


def test_admin_defaults_to_locked_and_accepts_explicit_booleans() -> None:
    assert ClientConfig.model_validate({}).admin is False
    assert ClientConfig.model_validate({"admin": False}).admin is False
    assert ClientConfig.model_validate({"admin": True}).admin is True


@pytest.mark.parametrize(
    "value",
    [None, "true", "false", 1, 0, [], {}],
)
def test_admin_is_a_strict_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        ClientConfig.model_validate({"admin": value})


def test_admin_setting_appears_in_the_dotted_cli_keys() -> None:
    assert "admin" in configuration_keys(ClientConfig)
    assert "mcp_admin_enabled" not in configuration_keys(ClientConfig)


def test_client_model_rejects_generic_permission_objects() -> None:
    """No mcp_permissions object exists; the closed model rejects it."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClientConfig.model_validate({"mcp_permissions": {"use": True}})


def test_reserved_mcp_aliases_are_the_fixed_dispatcher_words() -> None:
    assert RESERVED_MCP_ALIASES == frozenset({"client", "mcp", "server"})


@pytest.mark.parametrize("alias", ["client", "mcp", "server"])
def test_reserved_aliases_are_rejected_as_mcp_server_aliases(alias: str) -> None:
    with pytest.raises(ValidationError, match="reserved"):
        ClientConfig.model_validate(
            {"mcp_servers": {alias: {"command": ["echo", "hi"]}}}
        )


def test_client_config_owns_environment_coercion_constraints_and_runtime_flattening(
    tmp_path,
) -> None:
    identity = str(uuid.uuid4())
    model = ClientConfig.from_sources(
        {"identity": {"id": identity}, "workspace": "workspace"},
        {
            "RELAY_CLIENT_TOOLS": "relay_sample_ping, relay_sample_exec",
        },
    )

    assert "tools" not in type(model).model_fields
    # Purged runtime knobs must be rejected outright (extra=forbid): the
    # client settings are deliberately minimal and timings are constants.
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClientConfig.model_validate(
            {
                "identity": {"id": identity},
                "runtime": {"reconnect_min_seconds": 1.5},
            }
        )
    credential = "test-token"
    runtime = model.runtime_settings(token=credential, config_path=tmp_path / "config.yaml")
    assert runtime["client_id"] == identity
    assert runtime["workspace"] == tmp_path / "workspace"
    assert "tools_allowlist" not in runtime


def test_client_environment_id_override_keeps_legacy_runtime_ids() -> None:
    runtime_id = "linux-terminal-e2e"

    model = ClientConfig.from_sources({}, {"RELAY_CLIENT_ID": runtime_id})

    assert model.identity.id == runtime_id
    with pytest.raises(ValidationError, match="UUID"):
        ClientConfig.model_validate({"identity": {"id": runtime_id}})
