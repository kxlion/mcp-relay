"""Shared strict bounds for JSON-shaped provider metadata and payloads."""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Annotated, Mapping, TypeAlias
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypeAliasType

MAX_JSON_BYTES = 64 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4096
MAX_JSON_COLLECTION_ITEMS = 256
MAX_JSON_URI_LENGTH = 2048

#: Collection-level bounds. A catalog of up to MAX_PROVIDER_TOOLS descriptors,
#: each individually unit-bounded, aggregates far beyond the unit bounds; the
#: catalog validates against these dedicated collection bounds (finite, but
#: sized for a full inventory of unit-bounded descriptors).
MAX_CATALOG_JSON_NODES = 16384
MAX_CATALOG_JSON_BYTES = 256 * 1024

# ---------------------------------------------------------------------------
# Result / transport bounds.
#
# Chain invariant (static test: tests/test_bounds_chain.py):
#     MAX_TOOL_RESULT_BYTES + MAX_CLIENT_RESULT_ENVELOPE_BYTES
#         <= MAX_WS_MESSAGE_BYTES
#
# - A single content block and a whole result share the same bound: both are
#   "one MCP result payload" at different nesting levels, calibrated on the
#   same real consumer. Splitting them would only pay off if they were
#   expected to diverge — deliberate MVP single knob.
# - A result travels inside a WS frame that also carries the protocol
#   envelope. The invariant reserves the exact largest compact envelope,
#   including a maximum-length request id; the larger default gap remains
#   operational headroom rather than the correctness check itself.
#
# Calibrated on the heaviest known real consumer (cua-driver: a 1080p PNG
# screenshot is ~0.3-0.8 MiB base64, a full UIA inventory tree ~0.5-3 MiB).
# These are protocol constants: change them only via PR with calibrated
# tests. Each may be overridden for operator debugging via the RELAY_*
# environment variables declared in `_SIZE_OVERRIDES` (clamped, logged, and
# still rejected by the per-stage size checks — an override only moves the
# thresholds, it never disables detection).
#
# Resolution happens at RUNTIME, inside the config loaders (single override
# mechanism; the removed package-import hook applied overrides before the
# .env was exported, silently dead — Windows bench 2026-09-09). Consumers
# must therefore read these constants through the module attribute at call
# time, never via ``from .json_bounds import`` copies.
MAX_TOOL_RESULT_BYTES = 2 * 1024 * 1024  # 2 MiB
MAX_RESULT_NODES = 65536  # full-desktop UIA dumps exceed the old 16384 (2026-09-09 bench)
MAX_WS_MESSAGE_BYTES = 4 * 1024 * 1024  # 4 MiB

#: Request ids are restricted to ASCII protocol-safe characters, so their
#: maximum character count is also their maximum compact-JSON byte count.
MAX_REQUEST_ID_LENGTH = 128

#: Bytes added around a compact provider-result JSON value by the largest
#: valid ClientResult frame. Computed from the wire shape so punctuation and
#: field names cannot be hand-counted incorrectly; tests compare this value
#: against an actual ClientResult model serialization.
_MAX_CLIENT_RESULT_FRAME = {
    "version": 2,
    "type": "result",
    "request_id": "r" * MAX_REQUEST_ID_LENGTH,
    "result": {},
}
MAX_CLIENT_RESULT_ENVELOPE_BYTES = len(
    json.dumps(
        _MAX_CLIENT_RESULT_FRAME,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
) - len(b"{}")

#: Calibrated defaults; tests restore them after mutating the globals.
_DEFAULT_BOUNDS: dict[str, int] = {
    "MAX_TOOL_RESULT_BYTES": MAX_TOOL_RESULT_BYTES,
    "MAX_RESULT_NODES": MAX_RESULT_NODES,
    "MAX_WS_MESSAGE_BYTES": MAX_WS_MESSAGE_BYTES,
}

#: Environment overrides for operator debugging. Value is clamped to the
#: declared range; an effective override is logged at startup; a combination
#: breaking the chain invariant aborts startup.
_SIZE_OVERRIDES: dict[str, tuple[int, int]] = {
    # name: (minimum, maximum)
    "RELAY_MAX_TOOL_RESULT_BYTES": (64 * 1024, 16 * 1024 * 1024),
    "RELAY_MAX_RESULT_NODES": (1024, 262144),
    "RELAY_MAX_WS_MESSAGE_BYTES": (64 * 1024, 32 * 1024 * 1024),
}

class SizeOverrideSettings(BaseModel):
    """Declared shape of the operator-facing RELAY_MAX_* size overrides."""

    model_config = ConfigDict(extra="forbid")

    max_tool_result_bytes: Annotated[int, Field(ge=64 * 1024, le=16 * 1024 * 1024)] = Field(
        default=MAX_TOOL_RESULT_BYTES, json_schema_extra={"env": "RELAY_MAX_TOOL_RESULT_BYTES"}
    )
    max_result_nodes: Annotated[int, Field(ge=1024, le=262144)] = Field(
        default=MAX_RESULT_NODES, json_schema_extra={"env": "RELAY_MAX_RESULT_NODES"}
    )
    max_ws_message_bytes: Annotated[int, Field(ge=64 * 1024, le=32 * 1024 * 1024)] = Field(
        default=MAX_WS_MESSAGE_BYTES, json_schema_extra={"env": "RELAY_MAX_WS_MESSAGE_BYTES"}
    )


#: Populated by :func:`resolve_size_overrides`; maps the constant name to the
#: effective override for diagnostics.
APPLIED_SIZE_OVERRIDES: dict[str, int] = {}


def resolve_size_overrides(environ: Mapping[str, str]) -> None:
    """Resolve and publish one complete startup snapshot of RELAY_MAX_* bounds.

    This is a startup-only mutation point: callers finish resolution before
    constructing the application or starting concurrent runtime tasks. Values
    are parsed, clamped, and validated in locals first; only a valid complete
    snapshot is published. Missing overrides therefore restore calibrated
    defaults, while a failed resolution leaves the previous snapshot intact.
    """
    logger = logging.getLogger(__name__)
    declared = SizeOverrideSettings.model_validate(
        {key.removeprefix("RELAY_").lower(): value for key, value in environ.items() if key in _SIZE_OVERRIDES}
    )
    effective: dict[str, int] = {
        "MAX_TOOL_RESULT_BYTES": declared.max_tool_result_bytes,
        "MAX_RESULT_NODES": declared.max_result_nodes,
        "MAX_WS_MESSAGE_BYTES": declared.max_ws_message_bytes,
    }
    applied: dict[str, int] = {}
    resolved: list[tuple[str, str, int, int]] = []
    for name, (minimum, maximum) in _SIZE_OVERRIDES.items():
        raw = environ.get(name)
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
        clamped = min(max(value, minimum), maximum)
        const_name = name.removeprefix("RELAY_")
        effective[const_name] = clamped
        applied[const_name] = clamped
        resolved.append((name, raw, value, clamped))

    result_bytes = effective["MAX_TOOL_RESULT_BYTES"]
    frame_bytes = effective["MAX_WS_MESSAGE_BYTES"]
    required_frame_bytes = result_bytes + MAX_CLIENT_RESULT_ENVELOPE_BYTES
    if required_frame_bytes > frame_bytes:
        raise ValueError(
            "size override combination breaks the protocol envelope invariant "
            f"MAX_TOOL_RESULT_BYTES ({result_bytes}) + "
            "MAX_CLIENT_RESULT_ENVELOPE_BYTES "
            f"({MAX_CLIENT_RESULT_ENVELOPE_BYTES}) <= MAX_WS_MESSAGE_BYTES "
            f"({frame_bytes}); fix RELAY_MAX_TOOL_RESULT_BYTES / "
            "RELAY_MAX_WS_MESSAGE_BYTES"
        )

    # Runtime readers use these module attributes. All actual call sites are
    # synchronous startup paths, so publishing after complete validation keeps
    # readers from observing a failed or stale resolution.
    for const_name, value in effective.items():
        globals()[const_name] = value
    APPLIED_SIZE_OVERRIDES.clear()
    APPLIED_SIZE_OVERRIDES.update(applied)

    for name, raw, value, clamped in resolved:
        if clamped != value:
            logger.warning("%s=%s clamped to %d", name, raw, clamped)
        logger.warning(
            "%s overridden to %d via %s",
            name.removeprefix("RELAY_"),
            clamped,
            name,
        )

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue = TypeAliasType(
    "JsonValue",
    JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"],
)
JsonObject = TypeAliasType("JsonObject", dict[str, JsonValue])


class JsonBoundsError(ValueError):
    """Raised when a value is not safe for a bounded JSON boundary."""


_UNSAFE_METADATA_WORDS = {
    "handler",
    "module",
    "executable",
    "exec",
    "code",
    "script",
    "callback",
    "callable",
    "command",
    "function",
    "entrypoint",
    "endpoint",
    "execute",
    "shell",
}
_SENSITIVE_QUERY_KEY = {
    "accesstoken",
    "apikey",
    "auth",
    "authtoken",
    "authorization",
    "bearer",
    "clientsecret",
    "credential",
    "jwt",
    "oauthtoken",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "sig",
    "signature",
    "token",
}


def validate_resource_uri(value: object) -> str:
    """Validate a conservative, bounded URI suitable for provider resources."""
    if not isinstance(value, str):
        raise JsonBoundsError("resource URI must be a string")
    if not value or len(value) > MAX_JSON_URI_LENGTH:
        raise JsonBoundsError("resource URI is empty or too long")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise JsonBoundsError("resource URI contains a control character")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise JsonBoundsError("resource URI is malformed") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise JsonBoundsError("resource URI scheme is not allowed")
    if parsed.username is not None or parsed.password is not None:
        raise JsonBoundsError("resource URI must not contain credentials")
    try:
        if parsed.port is not None and not 0 < parsed.port <= 65535:
            raise JsonBoundsError("resource URI port is invalid")
    except ValueError as exc:
        raise JsonBoundsError("resource URI port is invalid") from exc
    try:
        hostname = parsed.hostname
    except ValueError as exc:
        raise JsonBoundsError("resource URI host is malformed") from exc
    if not hostname:
        raise JsonBoundsError("resource URI host is missing")
    if any(
        _normalize_query_key(key) in _SENSITIVE_QUERY_KEY
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise JsonBoundsError("resource URI contains credential-like query data")
    return value


def _normalize_query_key(key: str) -> str:
    """Normalize query-key spelling before applying the credential policy."""
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def is_sensitive_query_key(key: str) -> bool:
    """Report whether a query key denotes credential-like data."""
    return _normalize_query_key(key) in _SENSITIVE_QUERY_KEY


def _normalize_metadata_key(key: str) -> str:
    """Normalize separators and camel-case for executable-key checks."""
    camel_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return re.sub(r"[^a-z0-9]+", "_", camel_case.casefold()).strip("_")


def _reject_unsafe_metadata_key(key: str) -> None:
    normalized = _normalize_metadata_key(key)
    if key == "command_id":
        return
    if any(part in _UNSAFE_METADATA_WORDS for part in normalized.split("_")):
        raise JsonBoundsError(f"JSON metadata key is not allowed: {key!r}")


def _format_bound(
    *,
    env_name: str,
    bound: int,
    measured: int,
    unit: str,
    measurement_is_lower_bound: bool = False,
) -> str:
    """Format an exact measurement or a safe bounded-traversal lower bound."""
    qualifier = "at least " if measurement_is_lower_bound else ""
    return f"{env_name}: {bound} < payload: {qualifier}{measured} {unit}"


def validate_json_bounds(
    value: object,
    *,
    require_object: bool = False,
    label: str = "value",
    reject_unsafe_metadata: bool = False,
    max_nodes: int = MAX_JSON_NODES,
    max_bytes: int = MAX_JSON_BYTES,
    max_nodes_env: str | None = None,
    max_bytes_env: str | None = None,
) -> object:
    """Validate and return a finite, bounded JSON value without transforming it.

    The traversal is shared by descriptors, provider invocations, structured
    content, and protocol results.  It deliberately accepts only the JSON
    types themselves; tuples, sets, model instances, bytes, and other Python
    objects are rejected instead of being stringified or otherwise adapted.
    ``max_nodes``/``max_bytes`` let collection-level callers (the provider
    tool catalog) validate an aggregate of unit-bounded items against wider,
    still-finite bounds. Result-bound callers pass ``max_nodes_env`` /
    ``max_bytes_env`` (the RELAY_* variable names) so a refusal names the
    exact bound and its value vs the measured payload. Byte totals are exact;
    node totals are reported as a lower bound because traversal stops safely
    at the first node over the configured limit.
    """
    if require_object and not isinstance(value, dict):
        raise JsonBoundsError(f"{label} must be a JSON object")

    nodes = 0
    stack: list[tuple[object, int, str | None]] = [(value, 1, None)]
    while stack:
        node, depth, parent_key = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            if max_nodes_env is not None:
                raise JsonBoundsError(
                    _format_bound(
                        env_name=max_nodes_env,
                        bound=max_nodes,
                        measured=nodes,
                        unit="nodes",
                        measurement_is_lower_bound=True,
                    )
                )
            raise JsonBoundsError(f"{label} has too many nodes")
        if depth > MAX_JSON_DEPTH:
            raise JsonBoundsError(f"{label} is too deeply nested")

        if isinstance(node, dict):
            if len(node) > MAX_JSON_COLLECTION_ITEMS:
                raise JsonBoundsError(f"{label} has too many object members")
            for key, child in node.items():
                if not isinstance(key, str):
                    raise JsonBoundsError(f"{label} object keys must be strings")
                if reject_unsafe_metadata:
                    _reject_unsafe_metadata_key(key)
                stack.append((child, depth + 1, key))
        elif isinstance(node, list):
            if len(node) > MAX_JSON_COLLECTION_ITEMS:
                raise JsonBoundsError(f"{label} has too many array items")
            stack.extend((child, depth + 1, parent_key) for child in node)
        elif isinstance(node, str):
            continue
        elif isinstance(node, bool) or node is None:
            continue
        elif isinstance(node, int) and not isinstance(node, bool):
            continue
        elif isinstance(node, float) and math.isfinite(node):
            continue
        else:
            raise JsonBoundsError(f"{label} must contain only JSON values")

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise JsonBoundsError(f"{label} must contain only JSON values") from exc
    if len(encoded) > max_bytes:
        if max_bytes_env is not None:
            raise JsonBoundsError(
                _format_bound(
                    env_name=max_bytes_env,
                    bound=max_bytes,
                    measured=len(encoded),
                    unit="bytes",
                )
            )
        raise JsonBoundsError(f"{label} JSON exceeds maximum size")
    return value


__all__ = [
    "JsonBoundsError",
    "JsonObject",
    "JsonPrimitive",
    "JsonValue",
    "MAX_CATALOG_JSON_BYTES",
    "MAX_CATALOG_JSON_NODES",
    "MAX_CLIENT_RESULT_ENVELOPE_BYTES",
    "MAX_JSON_BYTES",
    "MAX_JSON_COLLECTION_ITEMS",
    "MAX_JSON_DEPTH",
    "MAX_JSON_NODES",
    "MAX_JSON_URI_LENGTH",
    "MAX_REQUEST_ID_LENGTH",
    "validate_json_bounds",
    "validate_resource_uri",
]
