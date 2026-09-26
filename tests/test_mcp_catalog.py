"""Tests for the client-side catalog: snapshots, revision, pagination, cursors.

The catalog is the immutable snapshot layer between the hub (which owns the
transports) and the fixed facade's ``relay_mcp_list`` / ``relay_mcp_command``
operations. Key identity is the exact ``(alias, tool)`` tuple; the revision is
``<runtime_uuid_hex>:<generation>`` and moves only on effective change.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcp_relay.json_bounds import MAX_JSON_BYTES
from mcp_relay.mcp_catalog import (
    DEFAULT_PAGE_LIMIT,
    MAX_CURSOR_LENGTH,
    MAX_PAGE_LIMIT,
    AliasCatalog,
    CatalogError,
    ClientCatalog,
    CursorCodec,
    RouteReservation,
    validate_limit,
    validate_selector,
)
from mcp_relay.provider_tools import ProviderToolDescriptor


def _descriptor(name: str, *, description: str = "Tool") -> ProviderToolDescriptor:
    return ProviderToolDescriptor.model_validate(
        {
            "provider_name": "probe",
            "tool_name": name,
            "description": description,
            "input_schema": {"type": "object", "properties": {}},
        }
    )


class _FakeProvider:
    """Route owner stand-in; identity is what matters, never its value."""


def _record(
    alias: str = "probe",
    *,
    available: bool = True,
    descriptors: tuple[ProviderToolDescriptor, ...] | None = None,
    provider: object | None = None,
    enabled: bool = True,
    runtime_state: str = "running",
    last_error: dict[str, str] | None = None,
    entry: dict[str, Any] | None = None,
) -> AliasCatalog:
    return AliasCatalog(
        alias=alias,
        enabled=enabled,
        runtime_state=runtime_state,
        transport="stdio",
        entry={"command": ["/bin/tool"]} if entry is None else entry,
        last_error=last_error,
        catalog_available=available,
        discovery_error=None if available else {"code": "discovery_failed", "message": "x"},
        descriptors=descriptors if descriptors is not None else (_descriptor("echo"),),
        provider=provider if provider is not None else (_FakeProvider() if available else None),
    )


def _seed(catalog: ClientCatalog, *records: AliasCatalog) -> None:
    for record in records:
        catalog.update_alias(record)


# ---------------------------------------------------------------------------
# Revision
# ---------------------------------------------------------------------------


def test_revision_is_runtime_uuid_plus_generation_and_initially_zero() -> None:
    catalog = ClientCatalog(runtime_id="a" * 32)
    assert catalog.revision == f"{'a' * 32}:0"
    assert len(catalog.revision) <= 128


def test_identical_re_read_does_not_change_the_revision() -> None:
    provider = _FakeProvider()
    catalog = ClientCatalog()
    _seed(catalog, _record(provider=provider, descriptors=(_descriptor("echo"),)))
    first = catalog.revision

    catalog.update_alias(
        _record(provider=provider, descriptors=(_descriptor("echo"),))
    )

    assert catalog.revision == first


@pytest.mark.parametrize(
    "changed",
    [
        "descriptors",
        "availability",
        "route",
        "alias_added",
        "alias_removed",
    ],
)
def test_effective_changes_bump_the_generation_exactly_once(changed: str) -> None:
    catalog = ClientCatalog()
    provider = _FakeProvider()
    _seed(catalog, _record(provider=provider))
    first = catalog.revision

    if changed == "descriptors":
        catalog.update_alias(_record(provider=provider, descriptors=(_descriptor("echo"), _descriptor("zip"))))
    elif changed == "availability":
        catalog.update_alias(
            _record(provider=None, available=False, last_error={"code": "x", "message": "y"})
        )
    elif changed == "route":
        catalog.update_alias(_record(provider=_FakeProvider()))
    elif changed == "alias_added":
        catalog.update_alias(_record(alias="second", provider=_FakeProvider()))
    else:
        catalog.remove_alias("probe")

    assert catalog.revision != first
    assert int(catalog.revision.split(":")[1]) == int(first.split(":")[1]) + 1


def test_display_only_changes_keep_the_revision() -> None:
    catalog = ClientCatalog()
    provider = _FakeProvider()
    _seed(catalog, _record(provider=provider))
    first = catalog.revision

    catalog.update_alias(
        _record(
            provider=provider,
            runtime_state="running",
            last_error=None,
            entry={"command": ["/bin/other"]},
        )
    )

    assert catalog.revision == first


def test_recovery_after_invalid_notification_is_a_new_transition() -> None:
    catalog = ClientCatalog()
    provider = _FakeProvider()
    _seed(catalog, _record(provider=provider))
    healthy = catalog.revision

    catalog.update_alias(
        _record(provider=None, available=False, last_error={"code": "x", "message": "y"})
    )
    stale = catalog.revision
    assert stale != healthy

    catalog.update_alias(_record(provider=provider, descriptors=(_descriptor("echo"),)))
    assert catalog.revision != stale
    assert int(catalog.revision.split(":")[1]) == int(healthy.split(":")[1]) + 2


# ---------------------------------------------------------------------------
# Resolution and route reservation
# ---------------------------------------------------------------------------


def test_reservation_happy_path_returns_provider_and_descriptor() -> None:
    provider = _FakeProvider()
    catalog = ClientCatalog()
    _seed(catalog, _record(provider=provider))
    revision = catalog.revision

    reservation = catalog.reserve_route("probe", "echo", revision)

    assert isinstance(reservation, RouteReservation)
    assert reservation.provider is provider
    assert reservation.descriptor.name == "echo"
    assert reservation.revision == revision


def test_reservation_order_is_revision_alias_availability_tool() -> None:
    catalog = ClientCatalog()
    provider = _FakeProvider()
    _seed(catalog, _record(provider=provider))

    with pytest.raises(CatalogError) as stale:
        catalog.reserve_route("probe", "echo", f"{'b' * 32}:0")
    assert stale.value.code == "catalog_stale"

    with pytest.raises(CatalogError) as unknown:
        catalog.reserve_route("nope", "echo", catalog.revision)
    assert unknown.value.code == "alias_unknown"

    catalog.update_alias(
        _record(provider=None, available=False, last_error={"code": "x", "message": "y"})
    )
    with pytest.raises(CatalogError) as unavailable:
        catalog.reserve_route("probe", "echo", catalog.revision)
    assert unavailable.value.code == "alias_unavailable"

    catalog.update_alias(_record(provider=_FakeProvider()))
    with pytest.raises(CatalogError) as tool_unknown:
        catalog.reserve_route("probe", "zap", catalog.revision)
    assert tool_unknown.value.code == "tool_unknown"


def test_reservation_is_invalid_after_route_replacement() -> None:
    catalog = ClientCatalog()
    provider = _FakeProvider()
    _seed(catalog, _record(provider=provider))
    reservation = catalog.reserve_route("probe", "echo", catalog.revision)

    catalog.update_alias(_record(provider=_FakeProvider()))

    assert not reservation.still_valid(catalog)


def test_exact_names_are_never_normalized() -> None:
    provider = _FakeProvider()
    catalog = ClientCatalog()
    _seed(
        catalog,
        _record(
            provider=provider,
            descriptors=(_descriptor("ZIP_Upload.v2", description="Case"),),
        ),
    )
    assert catalog.reserve_route("probe", "ZIP_Upload.v2", catalog.revision) is not None
    with pytest.raises(CatalogError) as error:
        catalog.reserve_route("probe", "zip_upload_v2", catalog.revision)
    assert error.value.code == "tool_unknown"


# ---------------------------------------------------------------------------
# Selector and limit validation
# ---------------------------------------------------------------------------


def test_tool_without_alias_is_invalid() -> None:
    with pytest.raises(CatalogError) as error:
        validate_selector(alias=None, tool="echo")
    assert error.value.code == "invalid_arguments"


def test_limit_defaults_to_20_and_rejects_out_of_bounds() -> None:
    assert validate_limit(None) == DEFAULT_PAGE_LIMIT
    assert validate_limit(1) == 1
    assert validate_limit(MAX_PAGE_LIMIT) == MAX_PAGE_LIMIT
    for bad in (0, -1, 101, True, "5", 2.5, None - 1 if False else 100.0):
        with pytest.raises(CatalogError) as error:
            validate_limit(bad)
        assert error.value.code == "invalid_arguments"


def test_detail_level_rejects_limit_and_cursor() -> None:
    with pytest.raises(CatalogError) as error:
        validate_selector(alias="probe", tool="echo", limit=5)
    assert error.value.code == "invalid_arguments"
    with pytest.raises(CatalogError) as error:
        validate_selector(alias="probe", tool="echo", cursor="x")
    assert error.value.code == "invalid_arguments"


# ---------------------------------------------------------------------------
# Cursor codec
# ---------------------------------------------------------------------------


def test_cursor_round_trip_preserves_the_closed_content() -> None:
    codec = CursorCodec(b"k" * 32)
    encoded = codec.encode(revision="r" * 33, level="tools", alias="probe", offset=40)
    assert len(encoded) <= MAX_CURSOR_LENGTH

    assert codec.decode(
        encoded,
        current_revision="r" * 33,
        level="tools",
        alias="probe",
    ) == 40


def test_cursor_tampering_and_foreign_runtime_are_invalid() -> None:
    codec = CursorCodec(b"k" * 32)
    other = CursorCodec(b"z" * 32)
    encoded = codec.encode(revision="r1", level="servers", alias="", offset=0)

    with pytest.raises(CatalogError) as tampered:
        other.decode(encoded, current_revision="r1", level="servers", alias="")
    assert tampered.value.code == "invalid_cursor"

    payload, signature = encoded.split(".")
    bad = payload[:-2] + ("AA" if not payload.endswith("AA") else "BB")
    with pytest.raises(CatalogError) as forged:
        codec.decode(f"{bad}.{signature}", current_revision="r1", level="servers", alias="")
    assert forged.value.code == "invalid_cursor"

    with pytest.raises(CatalogError) as junk:
        codec.decode("not-a-cursor", current_revision="r1", level="servers", alias="")
    assert junk.value.code == "invalid_cursor"


def test_cursor_scope_and_staleness_are_distinct_errors() -> None:
    codec = CursorCodec(b"k" * 32)
    encoded = codec.encode(revision="r1", level="tools", alias="probe", offset=4)

    with pytest.raises(CatalogError) as scope:
        codec.decode(encoded, current_revision="r1", level="tools", alias="other")
    assert scope.value.code == "invalid_cursor"
    with pytest.raises(CatalogError) as scope_level:
        codec.decode(encoded, current_revision="r1", level="servers", alias="probe")
    assert scope_level.value.code == "invalid_cursor"
    with pytest.raises(CatalogError) as stale:
        codec.decode(encoded, current_revision="r2", level="tools", alias="probe")
    assert stale.value.code == "catalog_stale"


def test_cursor_offsets_are_non_negative() -> None:
    codec = CursorCodec(b"k" * 32)
    with pytest.raises(CatalogError) as error:
        codec.encode(revision="r1", level="tools", alias="probe", offset=-1)
    assert error.value.code == "invalid_cursor"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_pages_walk_the_snapshot_without_overlap() -> None:
    items = [{"name": f"tool-{index:02d}"} for index in range(45)]
    page = ClientCatalog.paginate(items, offset=0, limit=20, render=lambda item: item)
    assert len(page.items) == 20
    assert page.next_offset == 20

    second = ClientCatalog.paginate(
        items, offset=page.next_offset, limit=20, render=lambda item: item
    )
    assert second.items[0]["name"] == "tool-20"
    assert second.next_offset == 40

    last = ClientCatalog.paginate(
        items, offset=second.next_offset, limit=20, render=lambda item: item
    )
    assert len(last.items) == 5
    assert last.next_offset is None


def test_budget_trims_the_page_and_never_emits_an_empty_page_with_cursor() -> None:
    big = "x" * 900
    items = [{"blob": big}] * 200
    page = ClientCatalog.paginate(
        items, offset=0, limit=100, render=lambda item: item
    )
    assert 0 < len(page.items) < 100
    assert page.next_offset == len(page.items)
    rendered = json.dumps({"items": page.items}).encode("utf-8")
    assert len(rendered) <= MAX_JSON_BYTES


def test_single_item_over_budget_is_result_too_large() -> None:
    huge = {"blob": "x" * (MAX_JSON_BYTES + 1)}
    with pytest.raises(CatalogError) as error:
        ClientCatalog.paginate([huge], offset=0, limit=20, render=lambda item: item)
    assert error.value.code == "result_too_large"


def test_offset_past_the_end_is_invalid() -> None:
    with pytest.raises(CatalogError) as error:
        ClientCatalog.paginate([{"a": 1}], offset=5, limit=20, render=lambda item: item)
    assert error.value.code == "invalid_cursor"


# ---------------------------------------------------------------------------
# Snapshot views
# ---------------------------------------------------------------------------


def test_servers_view_is_sorted_with_catalog_available_and_no_secrets() -> None:
    catalog = ClientCatalog()
    _seed(catalog, _record(alias="zeta"), _record(alias="alpha"))
    servers = catalog.snapshot.servers_view()
    assert [server["alias"] for server in servers] == ["alpha", "zeta"]
    for server in servers:
        assert set(server) == {
            "alias",
            "enabled",
            "runtime_state",
            "transport",
            "entry",
            "last_error",
            "catalog_available",
            "discovery_error",
            "description",
            "env_keys",
        }
        assert server["catalog_available"] is True
        assert server["description"] is None
        assert server["env_keys"] == []


def test_servers_view_carries_the_safe_discovery_error() -> None:
    """Spec: the servers view exposes a safe discovery error when down."""
    catalog = ClientCatalog()
    _seed(catalog, _record(alias="alpha"), _record(alias="down", available=False))
    servers = catalog.snapshot.servers_view()
    down = next(server for server in servers if server["alias"] == "down")
    assert down["catalog_available"] is False
    assert down["discovery_error"] == {"code": "discovery_failed", "message": "x"}
    up = next(server for server in servers if server["alias"] == "alpha")
    assert up["discovery_error"] is None


def test_tools_view_carries_only_names_and_descriptions_sorted() -> None:
    catalog = ClientCatalog()
    _seed(
        catalog,
        _record(
            descriptors=(
                _descriptor("alpha", description="A"),
                _descriptor("ZIP", description="Z"),
            ),
        ),
    )
    tools = catalog.snapshot.tools_view("probe")
    assert tools == [
        {"name": "ZIP", "description": "Z"},
        {"name": "alpha", "description": "A"},
    ]


def test_detail_view_returns_the_full_descriptor() -> None:
    catalog = ClientCatalog()
    _seed(catalog, _record(descriptors=(_descriptor("echo", description="Echo"),)))
    detail = catalog.snapshot.tool_detail("probe", "echo")
    assert detail["name"] == "echo"
    assert detail["description"] == "Echo"
    assert detail["inputSchema"] == {"type": "object", "properties": {}}


def test_unavailable_alias_exposes_catalog_available_false_and_safe_error() -> None:
    catalog = ClientCatalog()
    _seed(
        catalog,
        _record(
            provider=None,
            available=False,
            last_error={"code": "spawn_failed", "message": "no"},
        ),
    )
    servers = catalog.snapshot.servers_view()
    assert servers[0]["catalog_available"] is False
    with pytest.raises(CatalogError) as error:
        catalog.snapshot.tools_view("probe")
    assert error.value.code == "alias_unavailable"
