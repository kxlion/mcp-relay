"""Client-side third-party catalog: snapshots, revision, pagination, cursors.

The catalog is an immutable snapshot layer between the hub (which owns the
transports) and the fixed facade's ``relay_mcp_list`` / ``relay_mcp_command``
operations. The hub remains the sole owner of transport lifecycles: the
catalog never opens or closes a process; it stores per-alias inventory state
and route references handed to it by the client wiring.

Key identity is the exact ``(alias, tool)`` tuple. Nothing here derives a
``relay_<alias>_<tool>`` public name or normalizes an upstream tool name:
``tool`` is the exact MCP name, matching the bounded ``ProviderToolName``.

The catalog revision ``<runtime_uuid_hex>:<generation>`` is authored solely by
the Client runtime. It moves only on effective change of the executable
snapshot (descriptors, availability, route, alias set). Display-only changes
(entry text, runtime state, hub errors) keep the revision; the hub state stays
visible through the servers view.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .json_bounds import MAX_JSON_BYTES
from .provider_tools import ProviderToolDescriptor

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_CURSOR_LENGTH",
    "MAX_PAGE_LIMIT",
    "MAX_REVISION_LENGTH",
    "AliasCatalog",
    "CatalogError",
    "CatalogSnapshot",
    "ClientCatalog",
    "CursorCodec",
    "RouteReservation",
    "Selector",
    "resolve_selector",
    "validate_limit",
    "validate_selector",
]

#: ``<runtime_uuid_hex>:<generation>`` (32 + 1 + digits, bounded generously).
MAX_REVISION_LENGTH = 128
DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100
MAX_CURSOR_LENGTH = 1024

_ERROR_CODES = frozenset(
    {
        "invalid_arguments",
        "invalid_cursor",
        "catalog_stale",
        "alias_unknown",
        "alias_unavailable",
        "tool_unknown",
        "result_too_large",
    }
)


class CatalogError(Exception):
    """A closed-code catalog failure rendered as a safe Relay error later."""

    def __init__(self, code: str, message: str) -> None:
        if code not in _ERROR_CODES:  # pragma: no cover - developer guard
            raise AssertionError(f"unknown catalog error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Selector:
    """The resolved three-level listing selector."""

    level: str
    alias: str | None
    tool: str | None
    limit: int


def resolve_selector(
    *,
    alias: str | None,
    tool: str | None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Selector:
    """Validate the closed ``relay_mcp_list`` arguments and resolve the level."""
    if tool is not None and alias is None:
        raise CatalogError("invalid_arguments", "tool requires an alias")
    if tool is not None and (limit is not None or cursor is not None):
        raise CatalogError("invalid_arguments", "detail level takes no pagination")
    if tool is not None:
        level = "tool"
    elif alias is not None:
        level = "tools"
    else:
        level = "servers"
    return Selector(
        level=level, alias=alias, tool=tool, limit=validate_limit(limit)
    )


def validate_selector(
    *,
    alias: str | None,
    tool: str | None,
    limit: int | None = None,
    cursor: str | None = None,
) -> None:
    """Validate the closed ``relay_mcp_list`` argument combinations."""
    if tool is not None and alias is None:
        raise CatalogError("invalid_arguments", "tool requires an alias")
    if tool is not None and (limit is not None or cursor is not None):
        raise CatalogError("invalid_arguments", "detail level takes no pagination")


def validate_limit(limit: int | None) -> int:
    """``None`` means the 20-item default; otherwise a strict 1..100 int."""
    if limit is None:
        return DEFAULT_PAGE_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise CatalogError("invalid_arguments", "limit must be an integer")
    if not 1 <= limit <= MAX_PAGE_LIMIT:
        raise CatalogError("invalid_arguments", "limit must be between 1 and 100")
    return limit


@dataclass(frozen=True)
class AliasCatalog:
    """One alias's executable snapshot state plus its display metadata."""

    alias: str
    enabled: bool
    runtime_state: str
    transport: str
    entry: Mapping[str, Any]
    last_error: Mapping[str, str] | None
    catalog_available: bool
    discovery_error: Mapping[str, str] | None
    descriptors: tuple[ProviderToolDescriptor, ...] = ()
    provider: object | None = None
    #: Credential key NAMES from the alias private .env, never values.
    env_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.catalog_available and self.provider is None:
            raise ValueError("an available alias needs a route provider")
        if not self.catalog_available and self.discovery_error is None:
            raise ValueError("an unavailable alias needs a discovery error")


@dataclass(frozen=True)
class RouteReservation:
    """A route reference bound to one validated generation.

    Dispatch must acquire this under the same synchronization as the snapshot
    it validated against. A catalog invalidation before the MCP call cancels
    the reservation; once sent, the request belongs to the old instance.
    """

    alias: str
    tool: str
    revision: str
    descriptor: ProviderToolDescriptor
    provider: object
    _generation: int

    def still_valid(self, catalog: "ClientCatalog") -> bool:
        return (
            catalog.snapshot.generation == self._generation
            and catalog.revision == self.revision
        )


@dataclass(frozen=True)
class CatalogSnapshot:
    """Immutable view over the alias records of one generation."""

    generation: int
    _records: Mapping[str, AliasCatalog] = field(default_factory=dict)

    def servers_view(self) -> list[dict[str, Any]]:
        """Sorted alias list with hub state and ``catalog_available``."""
        servers: list[dict[str, Any]] = []
        for alias in sorted(self._records):
            record = self._records[alias]
            description = _entry_description(record.entry)
            servers.append(
                {
                    "alias": record.alias,
                    "enabled": record.enabled,
                    "runtime_state": record.runtime_state,
                    "transport": record.transport,
                    "entry": dict(record.entry),
                    "last_error": (
                        None if record.last_error is None else dict(record.last_error)
                    ),
                    "catalog_available": record.catalog_available,
                    "discovery_error": (
                        None
                        if record.discovery_error is None
                        else dict(record.discovery_error)
                    ),
                    "description": description,
                    "env_keys": list(record.env_keys),
                }
            )
        return servers

    def tools_view(self, alias: str) -> list[dict[str, str]]:
        record = self._executable(alias)
        return [
            {"name": descriptor.name, "description": descriptor.description}
            for descriptor in sorted(record.descriptors, key=lambda item: item.name)
        ]

    def tool_detail(self, alias: str, tool: str) -> dict[str, Any]:
        record = self._executable(alias)
        for descriptor in record.descriptors:
            if descriptor.name == tool:
                return descriptor.model_dump(mode="json", by_alias=True, exclude_none=True)
        raise CatalogError("tool_unknown", "no such tool in a valid inventory")

    def route(self, alias: str, tool: str) -> tuple[ProviderToolDescriptor, object]:
        record = self._executable(alias)
        for descriptor in record.descriptors:
            if descriptor.name == tool:
                assert record.provider is not None
                return descriptor, record.provider
        raise CatalogError("tool_unknown", "no such tool in a valid inventory")

    def _executable(self, alias: str) -> AliasCatalog:
        record = self._records.get(alias)
        if record is None:
            raise CatalogError("alias_unknown", "no such MCP server alias")
        if not record.catalog_available or record.provider is None:
            raise CatalogError(
                "alias_unavailable",
                "the alias or its inventory is not executable",
            )
        return record


@dataclass(frozen=True)
class _Page:
    items: list[dict[str, Any]]
    #: ``None`` marks the last page; the caller encodes its next cursor.
    next_offset: int | None


class CursorCodec:
    """HMAC-SHA256 signed, base64url cursors over a closed payload.

    The signing key is random, in-memory, and runtime-local: a cursor from an
    older Client process cannot be verified and is therefore invalid. Nothing
    secret or tool-controlled rides in the cursor and nothing persists.
    """

    def __init__(self, key: bytes) -> None:
        self._key = key

    def encode(
        self, *, revision: str, level: str, alias: str, offset: int
    ) -> str:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise CatalogError("invalid_cursor", "cursor offset is invalid")
        payload = {
            "revision": revision,
            "level": level,
            "alias": alias,
            "offset": offset,
        }
        body = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).decode("ascii")
        signature = self._sign(body)
        cursor = f"{body}.{signature}"
        if len(cursor) > MAX_CURSOR_LENGTH:  # pragma: no cover - closed content
            raise CatalogError("invalid_cursor", "cursor overflow")
        return cursor

    def decode(
        self,
        cursor: str,
        *,
        current_revision: str,
        level: str,
        alias: str,
    ) -> int:
        if not isinstance(cursor, str) or cursor.count(".") != 1:
            raise CatalogError("invalid_cursor", "cursor is malformed")
        body, signature = cursor.split(".")
        if not body or not signature or not hmac.compare_digest(signature, self._sign(body)):
            raise CatalogError("invalid_cursor", "cursor signature is invalid")
        try:
            padded = body + "=" * (-len(body) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        except (ValueError, UnicodeDecodeError):
            raise CatalogError("invalid_cursor", "cursor payload is invalid") from None
        if not isinstance(payload, dict) or set(payload) != {
            "revision",
            "level",
            "alias",
            "offset",
        }:
            raise CatalogError("invalid_cursor", "cursor payload is invalid")
        if payload["revision"] != current_revision:
            raise CatalogError("catalog_stale", "the catalog revision moved")
        if payload["level"] != level or payload["alias"] != alias:
            raise CatalogError("invalid_cursor", "cursor scope does not match")
        offset = payload["offset"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise CatalogError("invalid_cursor", "cursor offset is invalid")
        return offset

    def _sign(self, body: str) -> str:
        digest = hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class ClientCatalog:
    """Mutable record store emitting immutable snapshots with a revision."""

    def __init__(self, *, runtime_id: str | None = None) -> None:
        self._runtime_id = (
            runtime_id
            if runtime_id is not None
            else uuid.uuid4().hex
        )
        if not re.fullmatch(r"[0-9a-f]{32}", self._runtime_id):
            raise ValueError("runtime id must be 32 lowercase hex characters")
        self._records: dict[str, AliasCatalog] = {}
        self._generation = 0
        self._codec_key = __import__("secrets").token_bytes(32)
        self.snapshot = CatalogSnapshot(generation=0, _records=dict(self._records))

    @property
    def revision(self) -> str:
        revision = f"{self._runtime_id}:{self._generation}"
        assert len(revision) <= MAX_REVISION_LENGTH
        return revision

    @property
    def cursor_codec(self) -> CursorCodec:
        return CursorCodec(self._codec_key)

    def update_alias(self, record: AliasCatalog) -> None:
        previous = self._records.get(record.alias)
        self._records[record.alias] = record
        self._bump_if_effective(previous, record)

    def remove_alias(self, alias: str) -> None:
        if alias not in self._records:
            return
        del self._records[alias]
        self._advance()

    def reserve_route(self, alias: str, tool: str, revision: str) -> RouteReservation:
        if revision != self.revision:
            raise CatalogError("catalog_stale", "the catalog revision moved")
        descriptor, provider = self.snapshot.route(alias, tool)
        return RouteReservation(
            alias=alias,
            tool=tool,
            revision=revision,
            descriptor=descriptor,
            provider=provider,
            _generation=self.snapshot.generation,
        )

    @staticmethod
    def paginate(
        items: Sequence[Mapping[str, Any]],
        *,
        offset: int,
        limit: int,
        render: Any,
        encode_cursor: Any = None,
        revision: str | None = None,
        level: str = "servers",
        alias: str = "",
    ) -> _Page:
        """Build one bounded page under the full JSON budget.

        The budget outranks ``limit``: shrink the item count and hand back a
        cursor; if a single item cannot fit, ``result_too_large``. An empty
        page with a cursor is never produced; an offset past the end is an
        invalid cursor.
        """
        if offset < 0 or offset > len(items):
            raise CatalogError("invalid_cursor", "cursor offset is out of range")
        if not items:
            # An empty snapshot renders a legitimate empty last page; only a
            # cursor pointing past real items is invalid.
            return _Page(items=[], next_offset=None)
        selected = items[offset : offset + limit]
        kept: list[dict[str, Any]] = []
        for item in selected:
            rendered = render(item)
            kept.append(rendered)
            # Budget the full envelope (level/alias/revision/cursor overhead
            # included), not just the kept items; the MCP JSON-RPC wrapper
            # rides within the same bound's margin.
            envelope = json.dumps(
                {"level": "tools", "items": kept, "catalog_revision": "0" * 64,
                 "next_cursor": "0" * 1024}
            ).encode("utf-8")
            if len(envelope) > MAX_JSON_BYTES:
                if not kept:
                    raise CatalogError(
                        "result_too_large", "one item exceeds the result budget"
                    )
                kept.pop()
                break
        if kept:
            next_offset = offset + len(kept)
            return _Page(
                items=kept,
                next_offset=None if next_offset >= len(items) else next_offset,
            )
        if offset >= len(items):
            raise CatalogError("invalid_cursor", "cursor offset is out of range")
        raise CatalogError("result_too_large", "one item exceeds the result budget")

    # ------------------------------------------------------------------
    # Revision transitions
    # ------------------------------------------------------------------

    def _bump_if_effective(
        self,
        previous: AliasCatalog | None,
        record: AliasCatalog,
    ) -> None:
        if previous is None:
            self._advance()
            return
        executable_change = (
            previous.catalog_available != record.catalog_available
            or previous.descriptors != record.descriptors
            or previous.provider is not record.provider
        )
        if executable_change:
            # Effective change of the executable snapshot: the revision
            # (generation) moves.
            self._generation += 1
        # The snapshot is always republished, even for display-only changes
        # (runtime state, hub errors): the frozen snapshot must never serve
        # a stale record, while the revision stays put on display-only
        # changes per the catalog contract.
        self._publish_snapshot()

    def _advance(self) -> None:
        self._generation += 1
        self._publish_snapshot()

    def _publish_snapshot(self) -> None:
        self.snapshot = CatalogSnapshot(
            generation=self._generation, _records=dict(self._records)
        )


_ENTRY_DESCRIPTION_KEYS = ("description", "about", "summary")


def _entry_description(entry: Mapping[str, Any]) -> str | None:
    """The real configured metadata if present, otherwise ``None``.

    No description is ever fabricated. ``command`` argv and ``env`` never
    reach the description, whatever their keys look like.
    """
    for key in _ENTRY_DESCRIPTION_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None
