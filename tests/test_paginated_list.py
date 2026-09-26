"""Paginated ``mcp.list``: the spec's three-level discovery contract.

Per the fixed-facade plan: first pages re-read the target inventories, cursor
pages continue the existing snapshot, responses are closed objects
``{level, items|tool, catalog_revision, next_cursor}``, and cursor failures
surface the closed ``invalid_cursor`` / ``catalog_stale`` codes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from mcp_relay.config import mcp_entry_add
from mcp_relay.mcp_catalog import ClientCatalog, CursorCodec
from mcp_relay.mcp_command import CommandError
from tests.test_control_capability import (
    Factory,
    _capability,
    _client_yaml,
    _invoke,
)


class TwoToolTransport:
    """A stdio-like transport whose inventory can change between reads."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.closed = False
        self.tools: list[dict[str, Any]] = [
            {
                "name": "ping",
                "description": "synthetic ping",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "echo",
                "description": "synthetic echo",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    async def list_tools(self, cursor: str | None = None) -> object:
        del cursor
        if self.fail:
            raise ConnectionError("synthetic failure")
        return {"tools": list(self.tools)}

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        del name, arguments
        return {"content": [{"type": "text", "text": "pong"}]}

    async def close(self) -> None:
        self.closed = True


class TwoToolFactory:
    def __init__(self) -> None:
        self.transports: list[TwoToolTransport] = []

    def __call__(self, launch: object) -> TwoToolTransport:
        transport = TwoToolTransport()
        self.transports.append(transport)
        return transport


def _two_capability(config_path: Path, workspace: Path) -> Any:
    """A capability bound to a catalog, with a two-tool running alias."""
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    capability = _capability(config_path, workspace, TwoToolFactory())
    # Mirror the production wiring in client.py: the catalog is bound and the
    # hub publishes its alias state into it.
    catalog = ClientCatalog()
    capability.bind_catalog(catalog)
    capability.hub.publish_catalog(catalog)
    asyncio.run(capability.hub.reconcile_all())
    capability.hub.publish_catalog(catalog)
    return capability


def test_servers_page_carries_level_revision_and_cursor(tmp_path: Path) -> None:
    """The closed servers response: {level, items, catalog_revision, next_cursor}."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "two", {"command": ["/bin/two"]}, None)
    capability = _two_capability(config_path, workspace)

    result = _invoke(capability, "mcp.list", {})

    assert result["level"] == "servers"
    assert [item["alias"] for item in result["items"]] == ["one", "two"]
    assert result["catalog_revision"] == capability._catalog.revision
    assert result["next_cursor"] is None
    item = result["items"][0]
    assert item["runtime_state"] == "running"
    assert item["catalog_available"] is True
    assert "s3cret" not in str(result)


def test_tools_page_lists_exact_names_with_pagination(tmp_path: Path) -> None:
    """With alias: {level: tools, items: [{name, description}]}, page 1 -> cursor."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _two_capability(config_path, workspace)

    page1 = _invoke(capability, "mcp.list", {"alias": "one", "limit": 1})

    assert page1["level"] == "tools"
    assert page1["alias"] == "one"
    assert [item["name"] for item in page1["items"]] == ["echo"]
    assert set(page1["items"][0]) == {"name", "description"}
    assert page1["next_cursor"] is not None

    page2 = _invoke(
        capability,
        "mcp.list",
        {"alias": "one", "limit": 1, "cursor": page1["next_cursor"]},
    )

    assert [item["name"] for item in page2["items"]] == ["ping"]
    assert page2["next_cursor"] is None


def test_detail_level_returns_the_full_descriptor(tmp_path: Path) -> None:
    """alias+tool: {level: tool, tool: {...}}; pagination is refused there."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _two_capability(config_path, workspace)

    detail = _invoke(capability, "mcp.list", {"alias": "one", "tool": "echo"})

    assert detail["level"] == "tool"
    assert detail["alias"] == "one"
    assert detail["tool"]["name"] == "echo"
    assert detail["catalog_revision"] == capability._catalog.revision

    with pytest.raises(CommandError) as refused:
        _invoke(
            capability, "mcp.list", {"alias": "one", "tool": "echo", "limit": 1}
        )
    assert refused.value.code == "invalid_arguments"
    assert refused.value.execution_state == "not_started"


def test_unknown_alias_and_tool_answer_the_closed_codes(tmp_path: Path) -> None:
    """alias_unknown / tool_unknown / alias_unavailable per the error table.

    List failures are raised, not returned: the Client transports them as
    ``ClientError`` so the facade renders an ``isError=true`` MCP result.
    """
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _two_capability(config_path, workspace)

    with pytest.raises(CommandError) as ghost_alias:
        _invoke(capability, "mcp.list", {"alias": "ghost"})
    assert ghost_alias.value.code == "alias_unknown"
    assert ghost_alias.value.execution_state == "not_started"

    with pytest.raises(CommandError) as ghost_tool:
        _invoke(capability, "mcp.list", {"alias": "one", "tool": "ghost"})
    assert ghost_tool.value.code == "tool_unknown"


def test_foreign_cursor_is_invalid_and_stale_cursor_is_catalog_stale(
    tmp_path: Path,
) -> None:
    """Runtime-local cursors: foreign runtime -> invalid_cursor; moved
    revision -> catalog_stale."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _two_capability(config_path, workspace)
    catalog: ClientCatalog = capability._catalog
    foreign = CursorCodec(b"z" * 32).encode(
        revision=catalog.revision, level="tools", alias="one", offset=1
    )

    with pytest.raises(CommandError) as foreign_call:
        _invoke(
            capability, "mcp.list", {"alias": "one", "limit": 1, "cursor": foreign}
        )
    assert foreign_call.value.code == "invalid_cursor"

    page1 = _invoke(capability, "mcp.list", {"alias": "one", "limit": 1})
    assert page1["next_cursor"] is not None
    # An effective change moves the revision: the cursor no longer matches.
    catalog.remove_alias("one")

    with pytest.raises(CommandError) as stale_call:
        _invoke(
            capability,
            "mcp.list",
            {"alias": "one", "limit": 1, "cursor": page1["next_cursor"]},
        )
    assert stale_call.value.code == "catalog_stale"


def test_first_page_rereads_the_inventory_after_a_notification(
    tmp_path: Path,
) -> None:
    """First pages refresh: an upstream change becomes visible on re-list."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _two_capability(config_path, workspace)
    factory_tools = capability.hub  # keep a handle for readability below

    before = _invoke(capability, "mcp.list", {"alias": "one"})
    assert [item["name"] for item in before["items"]] == ["echo", "ping"]

    # Upstream tools/list_changed: invalidate, mutate, then re-discover.
    provider = capability.hub.provider_clients()["one"]
    provider.invalidate_inventory()
    provider._transport.tools.append(
        {
            "name": "third",
            "description": "added later",
            "inputSchema": {"type": "object", "properties": {}},
        }
    )

    after = _invoke(capability, "mcp.list", {"alias": "one"})
    assert [item["name"] for item in after["items"]] == ["echo", "ping", "third"]
    del factory_tools


def test_unavailable_alias_keeps_the_servers_view_and_answers_alias_unavailable(
    tmp_path: Path,
) -> None:
    """Discovery failure isolation: alias stays listed, tools are refused."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "broken", {"command": ["/bin/broken"]}, None)
    capability = _capability(config_path, workspace, Factory(fail_aliases={"broken"}))
    catalog = ClientCatalog()
    capability.bind_catalog(catalog)
    capability.hub.publish_catalog(catalog)
    asyncio.run(capability.hub.reconcile_all())
    capability.hub.publish_catalog(catalog)

    result = _invoke(capability, "mcp.list", {})
    states = {item["alias"]: item for item in result["items"]}
    assert states["broken"]["catalog_available"] is False
    assert states["broken"]["last_error"] is not None

    with pytest.raises(CommandError) as refused:
        _invoke(capability, "mcp.list", {"alias": "broken"})
    assert refused.value.code == "alias_unavailable"
