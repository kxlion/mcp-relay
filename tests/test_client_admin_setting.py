"""Contract for the ``client.admin`` fail-closed setting.

The administration switch is active ONLY when the key is explicitly
``true`` in the YAML. A missing key, ``false`` or an ``unset`` all lock
the admin verbs behind ``permission_denied``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_relay import config


def _client_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(body),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


# ---------------------------------------------------------------------------
# Model: ClientConfig.admin
# ---------------------------------------------------------------------------


def test_admin_defaults_to_locked_when_absent() -> None:
    assert ClientConfig_admin() is False


def ClientConfig_admin() -> bool:
    from mcp_relay.config import ClientConfig

    return ClientConfig.model_validate({}).admin


def test_admin_accepts_explicit_booleans() -> None:
    from mcp_relay.config import ClientConfig

    assert ClientConfig.model_validate({"admin": True}).admin is True
    assert ClientConfig.model_validate({"admin": False}).admin is False


@pytest.mark.parametrize("value", ["true", 1, None, "yes"])
def test_admin_is_a_strict_boolean(value: object) -> None:
    from mcp_relay.config import ClientConfig

    with pytest.raises(ValidationError):
        ClientConfig.model_validate({"admin": value})


def test_admin_appears_in_the_dotted_cli_keys() -> None:
    from mcp_relay.config import ClientConfig, configuration_keys

    assert "admin" in configuration_keys(ClientConfig)


# ---------------------------------------------------------------------------
# Disk reading: fail-closed
# ---------------------------------------------------------------------------


def test_missing_key_reads_locked(tmp_path: Path) -> None:
    path = _client_config(
        tmp_path,
        """
        relay_url: ws://127.0.0.1:8000/ws
        """,
    )
    assert config.load_client_admin_setting(path) is False


def test_explicit_true_reads_unlocked(tmp_path: Path) -> None:
    path = _client_config(
        tmp_path,
        """
        admin: true
        """,
    )
    assert config.load_client_admin_setting(path) is True


def test_explicit_false_reads_locked(tmp_path: Path) -> None:
    path = _client_config(
        tmp_path,
        """
        admin: false
        """,
    )
    assert config.load_client_admin_setting(path) is False


def test_missing_file_reads_locked(tmp_path: Path) -> None:
    assert config.load_client_admin_setting(tmp_path / "absent.yaml") is False


def test_non_boolean_admin_is_rejected_by_the_strict_reader(
    tmp_path: Path,
) -> None:
    path = _client_config(
        tmp_path,
        """
        admin: "true"
        """,
    )
    with pytest.raises(config.ConfigError):
        config._client_admin_setting(path)


# ---------------------------------------------------------------------------
# CLI: init leaves administration locked, unset locks
# ---------------------------------------------------------------------------


def test_generated_client_yaml_leaves_administration_locked(
    tmp_path: Path,
) -> None:
    import yaml

    path = tmp_path / "config.yaml"
    config.init_config(path, "client", env={"RELAY_CLIENT_TOKEN": 'client-synthetic-credential-0000000000000000'})

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["admin"] is False
    assert config.load_client_admin_setting(path) is False


def test_unset_locks_the_admin_setting(tmp_path: Path) -> None:
    import yaml

    path = tmp_path / "config.yaml"
    config.init_config(path, "client", env={"RELAY_CLIENT_TOKEN": 'client-synthetic-credential-0000000000000000'})

    config.unset_value(path, "client", "admin")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert "admin" not in document
    assert config.load_client_admin_setting(path) is False
