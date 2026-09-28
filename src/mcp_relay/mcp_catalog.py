"""Client-side third-party catalog: per-alias records and public tool names.

The hub owns transports; this module only stores what the hub publishes for
each alias and derives the catalog the Server exposes as native MCP tools.

Public names are ``<alias>__<tool>``. A tool name outside the portable
``[A-Za-z0-9_-]`` alphabet, or too long, gets a readable truncated form plus
a short SHA-256 suffix of its exact identity, so a name never depends on the
other tools present. The Server never parses a public name: every catalog
entry carries its exact ``(alias, tool)`` identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .protocol import MAX_CATALOG_TOOLS, MAX_PUBLIC_TOOL_NAME_LENGTH
from .provider_tools import ProviderToolDescriptor

__all__ = ["AliasCatalog", "CatalogError", "ClientCatalog", "public_tool_name"]

_PORTABLE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_UNPORTABLE_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_HASH_LENGTH = 8


class CatalogError(Exception):
    """A closed-code refusal raised before any MCP send."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def public_tool_name(alias: str, tool: str) -> str:
    """Return the stable public name of one ``(alias, tool)`` identity."""
    name = f"{alias}__{tool}"
    if _PORTABLE_NAME.fullmatch(tool) and len(name) <= MAX_PUBLIC_TOOL_NAME_LENGTH:
        return name
    digest = hashlib.sha256(f"{alias}\0{tool}".encode()).hexdigest()[:_HASH_LENGTH]
    readable = f"{alias}__{_UNPORTABLE_CHARS.sub('_', tool)}"
    return f"{readable[: MAX_PUBLIC_TOOL_NAME_LENGTH - _HASH_LENGTH - 1]}_{digest}"


@dataclass(frozen=True)
class AliasCatalog:
    """One alias's executable state plus the display metadata for status."""

    alias: str
    enabled: bool
    runtime_state: str
    transport: str
    catalog_available: bool
    error: Mapping[str, str] | None
    descriptors: tuple[ProviderToolDescriptor, ...] = ()
    provider: object | None = None
    #: The ``tools`` allowlist from the entry; ``None`` publishes every tool.
    #: Values are optional description overrides.
    tool_filter: Mapping[str, str | None] | None = None

    def __post_init__(self) -> None:
        if self.catalog_available and self.provider is None:
            raise ValueError("an available alias needs a route provider")

    def exposed(self) -> tuple[ProviderToolDescriptor, ...]:
        """Descriptors this alias publishes, after the local allowlist."""
        if not self.catalog_available:
            return ()
        if self.tool_filter is None:
            return self.descriptors
        return tuple(d for d in self.descriptors if d.name in self.tool_filter)


class ClientCatalog:
    """Alias records published by the hub, and the derived public catalog."""

    def __init__(self) -> None:
        self._records: dict[str, AliasCatalog] = {}
        #: Per-alias publication problems of the last built catalog.
        self.publication_errors: dict[str, dict[str, str]] = {}

    @property
    def records(self) -> Mapping[str, AliasCatalog]:
        return self._records

    def update_alias(self, record: AliasCatalog) -> None:
        self._records[record.alias] = record

    def remove_alias(self, alias: str) -> None:
        self._records.pop(alias, None)

    def route(self, alias: str, tool: str) -> object:
        """Resolve the provider for one exposed tool, or refuse before send."""
        record = self._records.get(alias)
        if record is None:
            raise CatalogError("alias_unknown", "no such MCP server alias")
        if not record.catalog_available or record.provider is None:
            raise CatalogError(
                "alias_unavailable", "the alias or its inventory is not executable"
            )
        if not any(d.name == tool for d in record.exposed()):
            raise CatalogError("tool_unknown", "no such tool in the published catalog")
        return record.provider

    def build(self, *, max_bytes: int) -> list[dict[str, Any]]:
        """Build the wire catalog, deterministic and bounded to ``max_bytes``.

        Aliases are added in sorted order; an alias whose tools would push the
        catalog over the frame budget, or whose public names collide, is left
        out whole and reported in ``publication_errors``.
        """
        tools: list[dict[str, Any]] = []
        errors: dict[str, dict[str, str]] = {}
        names: dict[str, str] = {}
        size = 0
        for alias in sorted(self._records):
            record = self._records[alias]
            entries = [_entry(record, d) for d in record.exposed()]
            alias_names = [entry["name"] for entry in entries]
            if len(set(alias_names)) != len(alias_names) or any(
                name in names for name in alias_names
            ):
                errors[alias] = {
                    "code": "name_collision",
                    "message": "two tools map to the same public name",
                }
                continue
            alias_size = sum(len(json.dumps(entry)) + 1 for entry in entries)
            if (
                size + alias_size > max_bytes
                or len(tools) + len(entries) > MAX_CATALOG_TOOLS
            ):
                errors[alias] = {
                    "code": "catalog_too_large",
                    "message": "the alias tools do not fit in the catalog budget",
                }
                continue
            size += alias_size
            tools.extend(entries)
            names.update(dict.fromkeys(alias_names, alias))
        self.publication_errors = errors
        return sorted(tools, key=lambda entry: entry["name"])


def _entry(record: AliasCatalog, descriptor: ProviderToolDescriptor) -> dict[str, Any]:
    override = (record.tool_filter or {}).get(descriptor.name)
    entry: dict[str, Any] = {
        "name": public_tool_name(record.alias, descriptor.name),
        "alias": record.alias,
        "tool": descriptor.name,
        "description": override or descriptor.description,
        "input_schema": descriptor.input_schema,
    }
    if descriptor.output_schema is not None:
        entry["output_schema"] = descriptor.output_schema
    if descriptor.annotations:
        entry["annotations"] = descriptor.annotations
    return entry
