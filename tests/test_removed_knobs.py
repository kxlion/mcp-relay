"""Removed config knobs are refused cleanly, never silently ignored."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_relay.config import ClientConfig, set_value


def test_removed_runtime_yaml_field_is_rejected(tmp_path) -> None:
    # runtime.* client knobs are gone; YAML carrying one must fail loudly.
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClientConfig.model_validate(
            {
                "identity": {"id": "00000000-0000-0000-0000-000000000001"},
                "runtime": {"command_timeout_seconds": 45},
            }
        )


def test_removed_runtime_cli_key_is_refused(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    # No client section yet: set_value must fail for a different reason, so
    # initialize first.
    from mcp_relay.config import init_config

    init_config(config_path, "client", token="client-synthetic-credential-0000000000000000", env={})
    with pytest.raises(Exception, match="unknown client configuration key"):
        set_value(config_path, "client", "runtime.stdout_limit", "48000")
