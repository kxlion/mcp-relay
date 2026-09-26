"""Tests for the fixed Relay facade tool definitions (single source)."""

from __future__ import annotations

import pytest

from mcp_relay.relay_tools import (
    ADMIN_PUBLIC_TOOL_NAMES,
    ADMIN_WIRE_OPERATION_NAMES,
    PUBLIC_TO_WIRE,
    PUBLIC_TOOL_DESCRIPTIONS,
    PUBLIC_TOOL_NAMES,
    RELAY_CLIENT_STATUS,
    RELAY_MCP_ADD,
    RELAY_MCP_COMMAND,
    RELAY_MCP_DELETE,
    RELAY_MCP_DISABLE,
    RELAY_MCP_ENABLE,
    RELAY_MCP_LIST,
    RELAY_MCP_MODIFY,
    RELAY_REGISTRY_SEARCH,
    RELAY_SERVER_STATUS,
    SERVER_LOCAL_TOOL_NAMES,
    WIRE_CLIENT_STATUS,
    WIRE_MCP_ADD,
    WIRE_MCP_COMMAND,
    WIRE_MCP_DELETE,
    WIRE_MCP_DISABLE,
    WIRE_MCP_ENABLE,
    WIRE_MCP_LIST,
    WIRE_MCP_MODIFY,
    WIRE_OPERATION_NAMES,
    is_admin_public_tool,
    is_admin_wire_operation,
)

MAX_PUBLIC_TOOL_NAME_LENGTH = 128
MAX_DESCRIPTION_LENGTH = 2048


def test_public_surface_is_exactly_the_ten_fixed_tools() -> None:
    assert PUBLIC_TOOL_NAMES == frozenset(
        {
            "relay_server_status",
            "relay_registry_search",
            "relay_client_status",
            "relay_mcp_list",
            "relay_mcp_command",
            "relay_mcp_add",
            "relay_mcp_modify",
            "relay_mcp_delete",
            "relay_mcp_enable",
            "relay_mcp_disable",
        }
    )
    # The constants name the same tools as the canonical set.
    assert {
        RELAY_SERVER_STATUS,
        RELAY_REGISTRY_SEARCH,
        RELAY_CLIENT_STATUS,
        RELAY_MCP_LIST,
        RELAY_MCP_COMMAND,
        RELAY_MCP_ADD,
        RELAY_MCP_MODIFY,
        RELAY_MCP_DELETE,
        RELAY_MCP_ENABLE,
        RELAY_MCP_DISABLE,
    } == PUBLIC_TOOL_NAMES


def test_server_local_tools_are_the_status_and_search_pair() -> None:
    assert SERVER_LOCAL_TOOL_NAMES == (RELAY_SERVER_STATUS, RELAY_REGISTRY_SEARCH)
    assert set(SERVER_LOCAL_TOOL_NAMES) < PUBLIC_TOOL_NAMES


def test_wire_operations_are_exactly_the_eight_client_operations() -> None:
    assert WIRE_OPERATION_NAMES == frozenset(
        {
            "client.status",
            "mcp.list",
            "mcp.command",
            "mcp.add",
            "mcp.modify",
            "mcp.delete",
            "mcp.enable",
            "mcp.disable",
        }
    )
    assert {
        WIRE_CLIENT_STATUS,
        WIRE_MCP_LIST,
        WIRE_MCP_COMMAND,
        WIRE_MCP_ADD,
        WIRE_MCP_MODIFY,
        WIRE_MCP_DELETE,
        WIRE_MCP_ENABLE,
        WIRE_MCP_DISABLE,
    } == WIRE_OPERATION_NAMES


def test_public_to_wire_mapping_is_a_bijection_over_client_tools() -> None:
    assert set(PUBLIC_TO_WIRE) == PUBLIC_TOOL_NAMES - set(SERVER_LOCAL_TOOL_NAMES)
    assert set(PUBLIC_TO_WIRE.values()) == WIRE_OPERATION_NAMES
    assert len(set(PUBLIC_TO_WIRE.values())) == len(PUBLIC_TO_WIRE)
    assert PUBLIC_TO_WIRE[RELAY_CLIENT_STATUS] == "client.status"
    assert PUBLIC_TO_WIRE[RELAY_MCP_LIST] == "mcp.list"
    assert PUBLIC_TO_WIRE[RELAY_MCP_COMMAND] == "mcp.command"
    assert PUBLIC_TO_WIRE[RELAY_MCP_ADD] == "mcp.add"
    assert PUBLIC_TO_WIRE[RELAY_MCP_MODIFY] == "mcp.modify"
    assert PUBLIC_TO_WIRE[RELAY_MCP_DELETE] == "mcp.delete"
    assert PUBLIC_TO_WIRE[RELAY_MCP_ENABLE] == "mcp.enable"
    assert PUBLIC_TO_WIRE[RELAY_MCP_DISABLE] == "mcp.disable"


def test_admin_identification_covers_only_the_five_admin_verbs() -> None:
    assert ADMIN_PUBLIC_TOOL_NAMES == frozenset(
        {
            RELAY_MCP_ADD,
            RELAY_MCP_MODIFY,
            RELAY_MCP_DELETE,
            RELAY_MCP_ENABLE,
            RELAY_MCP_DISABLE,
        }
    )
    assert ADMIN_WIRE_OPERATION_NAMES == frozenset(
        {
            WIRE_MCP_ADD,
            WIRE_MCP_MODIFY,
            WIRE_MCP_DELETE,
            WIRE_MCP_ENABLE,
            WIRE_MCP_DISABLE,
        }
    )
    # Discovery and execution are never administration, and neither are the
    # status/search tools: the admin switch must not classify by prefix.
    assert RELAY_MCP_LIST not in ADMIN_PUBLIC_TOOL_NAMES
    assert RELAY_MCP_COMMAND not in ADMIN_PUBLIC_TOOL_NAMES
    assert RELAY_CLIENT_STATUS not in ADMIN_PUBLIC_TOOL_NAMES
    assert WIRE_MCP_LIST not in ADMIN_WIRE_OPERATION_NAMES
    assert WIRE_MCP_COMMAND not in ADMIN_WIRE_OPERATION_NAMES
    assert WIRE_CLIENT_STATUS not in ADMIN_WIRE_OPERATION_NAMES


@pytest.mark.parametrize(
    ("public_tool", "wire_operation"),
    sorted(PUBLIC_TO_WIRE.items()),
)
def test_admin_identification_agrees_between_public_and_wire_names(
    public_tool: str, wire_operation: str
) -> None:
    assert is_admin_public_tool(public_tool) == (
        public_tool in ADMIN_PUBLIC_TOOL_NAMES
    )
    assert is_admin_wire_operation(wire_operation) == (
        wire_operation in ADMIN_WIRE_OPERATION_NAMES
    )
    assert is_admin_public_tool(public_tool) == is_admin_wire_operation(
        wire_operation
    )


@pytest.mark.parametrize(
    "name",
    ["", "mcp", "mcp.", "mcp_add", "client_status", "relay_mcp_add", "mcp.list "],
)
def test_admin_wire_identification_matches_only_exact_wire_names(
    name: str,
) -> None:
    assert is_admin_wire_operation(name) is False


def test_descriptions_exist_for_every_public_tool_and_stay_bounded() -> None:
    assert set(PUBLIC_TOOL_DESCRIPTIONS) == PUBLIC_TOOL_NAMES
    for name, description in PUBLIC_TOOL_DESCRIPTIONS.items():
        assert description
        assert len(name) <= MAX_PUBLIC_TOOL_NAME_LENGTH
        assert len(description) <= MAX_DESCRIPTION_LENGTH


def test_descriptions_do_not_overstate_third_party_behaviour() -> None:
    # Tools that reach third-party MCP servers never promise read-only or
    # idempotent behaviour: the wrapper makes no safety claim about targets.
    for name in (
        RELAY_MCP_LIST,
        RELAY_MCP_COMMAND,
        RELAY_MCP_ADD,
        RELAY_MCP_MODIFY,
        RELAY_MCP_DELETE,
        RELAY_MCP_ENABLE,
        RELAY_MCP_DISABLE,
    ):
        lowered = PUBLIC_TOOL_DESCRIPTIONS[name].lower()
        assert "read-only" not in lowered
        assert "idempotent" not in lowered
        assert "safe" not in lowered
        assert "harmless" not in lowered
