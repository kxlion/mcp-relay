"""Shared Relay environment contract, derived from runtime model declarations."""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel


class UnknownRelayEnvironmentError(ValueError):
    """A safe key-only startup diagnostic (never an environment value)."""


def environment_fields(model: type[BaseModel]) -> dict[str, str]:
    """Map declared environment names to their owning model fields."""
    fields: dict[str, str] = {}
    for name, field in model.model_fields.items():
        extra: Any = field.json_schema_extra
        if isinstance(extra, dict) and isinstance(extra.get("env"), str):
            fields[extra["env"]] = name
    return fields


def validate_relay_environment(environ: Mapping[str, object]) -> None:
    """Reject any undeclared RELAY_* name in the effective startup environment.

    The allowed names are derived from the declared environment fields of the
    runtime models (ClientSettings, RelaySettings, SizeOverrideSettings), not
    from a separate whitelist. Diagnostics name the key, never the value.
    """
    # Lazy imports keep model definitions independent of startup validation.
    from .client import ClientSettings
    from .json_bounds import SizeOverrideSettings
    from .server import RelaySettings

    allowed = set().union(
        *(
            environment_fields(model)
            for model in (ClientSettings, RelaySettings, SizeOverrideSettings)
        )
    )
    for key in environ:
        if key.startswith("RELAY_") and key not in allowed:
            # Escape control characters and cap operator-controlled key lengths.
            raise UnknownRelayEnvironmentError(
                f"unknown Relay environment variable: {ascii(key[:128])}"
            )
