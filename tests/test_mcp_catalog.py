"""Client catalog: public names, allowlist, frame budget and route resolution."""

from __future__ import annotations

import pytest

from mcp_relay.mcp_catalog import (
    AliasCatalog,
    CatalogError,
    ClientCatalog,
    public_tool_name,
)
from mcp_relay.protocol import Catalog
from mcp_relay.provider_tools import ProviderToolDescriptor


def _descriptor(name: str, description: str = "A tool") -> ProviderToolDescriptor:
    return ProviderToolDescriptor(
        provider_name="x",
        tool_name=name,
        description=description,
        input_schema={"type": "object", "properties": {}},
    )


def _record(
    alias: str, *names: str, available: bool = True, tool_filter=None
) -> AliasCatalog:
    return AliasCatalog(
        alias=alias,
        enabled=True,
        runtime_state="running" if available else "unavailable",
        transport="stdio",
        catalog_available=available,
        error=None if available else {"code": "spawn_failed", "message": "down"},
        descriptors=tuple(_descriptor(name) for name in names) if available else (),
        provider=object() if available else None,
        tool_filter=tool_filter,
    )


def test_portable_names_are_kept_readable() -> None:
    assert public_tool_name("browser", "navigate") == "browser_navigate"
    assert public_tool_name("fs", "read-file_2") == "fs_read-file_2"


@pytest.mark.parametrize("tool", ["files.read", "ns:tool", "x" * 70])
def test_unportable_names_get_a_stable_hash_suffix(tool: str) -> None:
    name = public_tool_name("alias", tool)
    assert name == public_tool_name("alias", tool)
    assert len(name) <= 64
    assert name.startswith("alias_")
    assert all(ch.isalnum() or ch in "_-" for ch in name)


def test_sanitized_names_never_collide_with_a_portable_twin() -> None:
    assert public_tool_name("a", "b.c") != public_tool_name("a", "b_c")
    assert public_tool_name("a", "b.c") != public_tool_name("a", "b:c")


def test_build_is_sorted_and_carries_the_exact_identity() -> None:
    catalog = ClientCatalog()
    catalog.update_alias(_record("zeta", "run"))
    catalog.update_alias(_record("alpha", "files.read", "list"))
    tools = catalog.build(max_bytes=1_000_000)
    names = [tool["name"] for tool in tools]
    assert names == sorted(names)
    hashed = next(tool for tool in tools if tool["tool"] == "files.read")
    assert hashed["alias"] == "alpha"
    # The wire frame model accepts what the Client builds.
    Catalog(version=2, type="catalog", tools=tools)


def test_unavailable_alias_publishes_nothing() -> None:
    catalog = ClientCatalog()
    catalog.update_alias(_record("down", available=False))
    assert catalog.build(max_bytes=1_000_000) == []


def test_allowlist_filters_and_overrides_descriptions() -> None:
    catalog = ClientCatalog()
    catalog.update_alias(
        _record("fs", "read", "write", "delete", tool_filter={"read": "Read one file.", "write": None})
    )
    tools = {tool["tool"]: tool for tool in catalog.build(max_bytes=1_000_000)}
    assert set(tools) == {"read", "write"}
    assert tools["read"]["description"] == "Read one file."
    assert tools["write"]["description"] == "A tool"


def test_alias_over_budget_is_left_out_whole_and_reported() -> None:
    catalog = ClientCatalog()
    catalog.update_alias(_record("aaa", "one"))
    catalog.update_alias(_record("bbb", *(f"t{i}" for i in range(50))))
    first = catalog.build(max_bytes=10_000_000)
    small = len(str([t for t in first if t["alias"] == "aaa"])) + 200
    tools = catalog.build(max_bytes=small)
    assert {tool["alias"] for tool in tools} == {"aaa"}
    assert catalog.publication_errors["bbb"]["code"] == "catalog_too_large"


def test_route_resolves_only_published_tools() -> None:
    catalog = ClientCatalog()
    record = _record("fs", "read", "write", tool_filter={"read": None})
    catalog.update_alias(record)
    assert catalog.route("fs", "read") is record.provider
    with pytest.raises(CatalogError) as filtered:
        catalog.route("fs", "write")
    assert filtered.value.code == "tool_unknown"
    with pytest.raises(CatalogError) as unknown:
        catalog.route("nope", "read")
    assert unknown.value.code == "alias_unknown"
    catalog.update_alias(_record("fs", available=False))
    with pytest.raises(CatalogError) as down:
        catalog.route("fs", "read")
    assert down.value.code == "alias_unavailable"
