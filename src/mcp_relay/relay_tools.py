"""Single source of truth for the fixed Relay facade tool surface.

This module defines the complete public tool surface exposed by the Server's
MCP facade, the wire operation names announced over the Server-Client
protocol, their stable descriptions, and the identification of the
administration-gated operations.

There are no derived per-provider public names here: third-party MCP tools
are never published individually by the Server. The Client alone owns the
third-party catalog; discovery (``relay_mcp_list``) and execution
(``relay_mcp_command``) are the only paths to it.
"""

from __future__ import annotations

from typing import Mapping

# Server-local tools: answered by the Server itself, never routed to a Client.
RELAY_SERVER_STATUS = "relay_server_status"
RELAY_REGISTRY_SEARCH = "relay_registry_search"

SERVER_LOCAL_TOOL_NAMES: tuple[str, ...] = (
    RELAY_SERVER_STATUS,
    RELAY_REGISTRY_SEARCH,
)

# Client-owned operations: the Server routes these to the connected Client.
RELAY_CLIENT_STATUS = "relay_client_status"
RELAY_MCP_LIST = "relay_mcp_list"
RELAY_MCP_COMMAND = "relay_mcp_command"
RELAY_MCP_ADD = "relay_mcp_add"
RELAY_MCP_MODIFY = "relay_mcp_modify"
RELAY_MCP_DELETE = "relay_mcp_delete"
RELAY_MCP_ENABLE = "relay_mcp_enable"
RELAY_MCP_DISABLE = "relay_mcp_disable"

# The complete fixed public surface, registered at Server startup. It never
# varies with connectivity, administration rights, or third-party catalogs.
PUBLIC_TOOL_NAMES: frozenset[str] = frozenset(
    {
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
    }
)

# Wire operation names carried in ``Capabilities.tools``. This list is
# independent of the ``client.admin`` setting: the operations stay
# announced even when the Client would refuse the administration verbs.
WIRE_CLIENT_STATUS = "client.status"
WIRE_MCP_LIST = "mcp.list"
WIRE_MCP_COMMAND = "mcp.command"
WIRE_MCP_ADD = "mcp.add"
WIRE_MCP_MODIFY = "mcp.modify"
WIRE_MCP_DELETE = "mcp.delete"
WIRE_MCP_ENABLE = "mcp.enable"
WIRE_MCP_DISABLE = "mcp.disable"

WIRE_OPERATION_NAMES: frozenset[str] = frozenset(
    {
        WIRE_CLIENT_STATUS,
        WIRE_MCP_LIST,
        WIRE_MCP_COMMAND,
        WIRE_MCP_ADD,
        WIRE_MCP_MODIFY,
        WIRE_MCP_DELETE,
        WIRE_MCP_ENABLE,
        WIRE_MCP_DISABLE,
    }
)

# Exact mapping between the facade-facing public tool names and the wire
# operation names for every Client-owned operation. The key sets are disjoint
# from the Server-local tools and the mapping is a bijection.
PUBLIC_TO_WIRE: Mapping[str, str] = {
    RELAY_CLIENT_STATUS: WIRE_CLIENT_STATUS,
    RELAY_MCP_LIST: WIRE_MCP_LIST,
    RELAY_MCP_COMMAND: WIRE_MCP_COMMAND,
    RELAY_MCP_ADD: WIRE_MCP_ADD,
    RELAY_MCP_MODIFY: WIRE_MCP_MODIFY,
    RELAY_MCP_DELETE: WIRE_MCP_DELETE,
    RELAY_MCP_ENABLE: WIRE_MCP_ENABLE,
    RELAY_MCP_DISABLE: WIRE_MCP_DISABLE,
}

# Administration-gated operations. The single Client boolean
# ``client.admin`` gates exactly these verbs; discovery
# (``relay_mcp_list`` / ``mcp.list``), execution (``relay_mcp_command`` /
# ``mcp.command``), the two status tools and the Server-local search are
# never gated. Identification is an explicit set: never a name-prefix rule.
ADMIN_PUBLIC_TOOL_NAMES: frozenset[str] = frozenset(
    {
        RELAY_MCP_ADD,
        RELAY_MCP_MODIFY,
        RELAY_MCP_DELETE,
        RELAY_MCP_ENABLE,
        RELAY_MCP_DISABLE,
    }
)

ADMIN_WIRE_OPERATION_NAMES: frozenset[str] = frozenset(
    {
        WIRE_MCP_ADD,
        WIRE_MCP_MODIFY,
        WIRE_MCP_DELETE,
        WIRE_MCP_ENABLE,
        WIRE_MCP_DISABLE,
    }
)

# Stable, bounded descriptions for the fixed public tools. They describe only
# Relay-owned behaviour and never promise read-only or idempotent third-party
# behaviour: discovered MCP tools may have destructive effects.
PUBLIC_TOOL_DESCRIPTIONS: Mapping[str, str] = {
    RELAY_SERVER_STATUS: (
        "Return the Relay Server's safe status (connection, heartbeat, "
        "versions); always answers locally, Client or not."
    ),
    RELAY_REGISTRY_SEARCH: (
        "Search the official MCP Registry (read-only, server-side) and return "
        "bounded server metadata; never touches the client channel."
    ),
    RELAY_CLIENT_STATUS: (
        "Report the connected Relay Client's real runtime status, including "
        "its administration setting, catalog revision and hub counters."
    ),
    RELAY_MCP_LIST: (
        "Progressively discover the Client's configured MCP servers and their "
        "tools through paginated, signed-cursor listing; always available."
    ),
    RELAY_MCP_COMMAND: (
        "Execute one discovered third-party MCP tool by alias and exact tool "
        "name, requiring the catalog revision obtained from discovery."
    ),
    RELAY_MCP_ADD: (
        "Declare and start a new MCP server alias on the Client (admin)."
    ),
    RELAY_MCP_MODIFY: (
        "Replace an existing MCP server alias entry on the Client (admin)."
    ),
    RELAY_MCP_DELETE: (
        "Stop and remove an MCP server alias from the Client (admin)."
    ),
    RELAY_MCP_ENABLE: "Enable an MCP server alias on the Client (admin).",
    RELAY_MCP_DISABLE: (
        "Disable an MCP server alias on the Client; the entry is kept (admin)."
    ),
}


def is_admin_public_tool(name: str) -> bool:
    """Return whether the fixed public tool requires the admin setting."""
    return name in ADMIN_PUBLIC_TOOL_NAMES


def is_admin_wire_operation(name: str) -> bool:
    """Return whether the wire operation requires the admin setting."""
    return name in ADMIN_WIRE_OPERATION_NAMES


__all__ = [
    "ADMIN_PUBLIC_TOOL_NAMES",
    "ADMIN_WIRE_OPERATION_NAMES",
    "PUBLIC_TO_WIRE",
    "PUBLIC_TOOL_DESCRIPTIONS",
    "PUBLIC_TOOL_NAMES",
    "RELAY_CLIENT_STATUS",
    "RELAY_MCP_ADD",
    "RELAY_MCP_COMMAND",
    "RELAY_MCP_DELETE",
    "RELAY_MCP_DISABLE",
    "RELAY_MCP_ENABLE",
    "RELAY_MCP_LIST",
    "RELAY_MCP_MODIFY",
    "RELAY_REGISTRY_SEARCH",
    "RELAY_SERVER_STATUS",
    "SERVER_LOCAL_TOOL_NAMES",
    "WIRE_CLIENT_STATUS",
    "WIRE_MCP_ADD",
    "WIRE_MCP_COMMAND",
    "WIRE_MCP_DELETE",
    "WIRE_MCP_DISABLE",
    "WIRE_MCP_ENABLE",
    "WIRE_MCP_LIST",
    "WIRE_MCP_MODIFY",
    "WIRE_OPERATION_NAMES",
    "is_admin_public_tool",
    "is_admin_wire_operation",
]
