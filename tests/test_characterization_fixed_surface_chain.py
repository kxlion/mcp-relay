"""Characterization: the real public chain against synthetic MCP servers.

Synthetic local MCP servers (stdio subprocess and Streamable HTTP) are
driven through the PUBLIC chain only — ``McpHub`` + ``ClientCatalog`` +
``mcp_command.execute_command`` — never naming a transport class. Locked
contracts (must survive the Tranche 3 FastMCP transport replacement):

- discovery: a reachable alias becomes running and its tool catalog is
  published; an unreachable (offline) alias stays unavailable with no
  catalog;
- native results pass through intact: structuredContent, unknown
  third-party fields, and MCP ``isError`` results are never interpreted;
- an oversized result is refused with the honest ``result_too_large`` /
  ``unknown`` answer after exactly one send;
- admin: a disabled entry is never spawned or executable until re-enabled.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_relay.config import mcp_entry_add, mcp_entry_set_enabled
from mcp_relay.json_bounds import MAX_TOOL_RESULT_BYTES
from mcp_relay.mcp_catalog import ClientCatalog
from mcp_relay.mcp_command import CommandError, execute_command
from mcp_relay.mcp_hub import AliasState, McpHub, production_transport_factory

_MINI_STDIO = """\
import asyncio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

mcp = MCPServer('mini')

@mcp.tool(structured_output=False)
async def echo(text: str) -> dict:
    return {'content': [{'type': 'text', 'text': text}],
            'structuredContent': {'echo': text},
            'isError': False, 'resultType': 'complete', 'futureField': [1, 2]}

@mcp.tool(structured_output=False)
async def fail() -> dict:
    raise ToolError('tool says no')

@mcp.tool(structured_output=False)
async def flood(size: int) -> dict:
    return {'content': [{'type': 'text', 'text': 'x' * size}], 'isError': False}

@mcp.tool(structured_output=False)
async def linger(seconds: float) -> dict:
    import asyncio as _aio
    await _aio.sleep(seconds)
    return {'content': [{'type': 'text', 'text': 'lingered'}], 'isError': False}

if __name__ == '__main__':
    mcp.run()
"""


def _configure(config_path: Path, entry: dict[str, object], alias: str = "mini") -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.chmod(0o600) if config_path.exists() else None
    if not config_path.exists():
        config_path.write_text(
            "relay_url: ws://localhost:1/ws\n", encoding="utf-8"
        )
        config_path.chmod(0o600)
    mcp_entry_add(config_path, alias, entry, None)


def _free_port() -> int:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.close()
    return port


def _build_synthetic_http_server() -> MCPServer:
    """Neutral synthetic Streamable-HTTP MCP server: echo, fail, flood."""
    mcp = MCPServer("mini-http")

    @mcp.tool(structured_output=False)
    async def echo(text: str) -> dict:
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"echo": text},
            "isError": False,
            "resultType": "complete",
            "futureField": [1, 2],
        }

    @mcp.tool(structured_output=False)
    async def fail() -> dict:
        raise ToolError("tool says no")

    @mcp.tool(structured_output=False)
    async def flood(size: int) -> dict:
        return {"content": [{"type": "text", "text": "x" * size}], "isError": False}

    @mcp.tool(structured_output=False)
    async def linger(seconds: float) -> dict:
        await asyncio.sleep(seconds)
        return {"content": [{"type": "text", "text": "lingered"}], "isError": False}

    return mcp


def _command(alias: str, tool: str, revision: str, **arguments: object) -> dict[str, object]:
    return {
        "alias": alias,
        "tool": tool,
        "arguments": dict(arguments),
        "catalog_revision": revision,
    }


# ---------------------------------------------------------------------------
# stdio chain
# ---------------------------------------------------------------------------


def test_stdio_chain_discovery_and_native_result_passthrough(tmp_path: Path) -> None:
    mini = tmp_path / "mini_mcp.py"
    mini.write_text(_MINI_STDIO, encoding="utf-8")
    config_path = tmp_path / "config" / "config.yaml"
    _configure(config_path, {"command": [sys.executable, str(mini)]})
    hub = McpHub(config_path, tmp_path, transport_factory=production_transport_factory)
    catalog = ClientCatalog()

    async def scenario() -> None:
        status = await hub.reconcile_all()
        assert status["mini"].state is AliasState.RUNNING
        hub.publish_catalog(catalog)
        tools = catalog.snapshot.tools_view("mini")
        assert [item["name"] for item in tools] == ["echo", "fail", "flood", "linger"]

        # Native structured result: the SDK serializes the tool's dict as
        # JSON text; unknown third-party fields inside survive verbatim.
        outcome = await execute_command(
            _command("mini", "echo", catalog.revision, text="hello"),
            catalog=catalog,
        )
        dumped = outcome.result.model_dump(mode="json", by_alias=True, exclude_none=True)
        payload = json.loads(dumped["content"][0]["text"])
        assert payload["structuredContent"] == {"echo": "hello"}
        assert payload["isError"] is False
        assert payload["resultType"] == "complete"
        assert payload["futureField"] == [1, 2]
        # The SDK's own unknown top-level field also passes through.
        assert dumped["resultType"] == "complete"

        # Native MCP error: relayed as a RESULT, no Relay error code.
        outcome = await execute_command(
            _command("mini", "fail", catalog.revision),
            catalog=catalog,
        )
        assert outcome.result.is_error is True
        assert "tool says no" in outcome.result.content[0].text

        # Oversized result: refused honestly, execution state unknown.
        with pytest.raises(CommandError) as excinfo:
            await execute_command(
                _command("mini", "flood", catalog.revision,
                         size=MAX_TOOL_RESULT_BYTES + 1024),
                catalog=catalog,
            )
        assert excinfo.value.code == "result_too_large"
        assert excinfo.value.execution_state == "unknown"

    try:
        asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    finally:
        asyncio.run(hub.forget("mini"))


def test_stdio_offline_alias_stays_unavailable_without_catalog(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "config.yaml"
    _configure(config_path, {"command": ["/nonexistent/relay-probe-binary"]})
    hub = McpHub(config_path, tmp_path, transport_factory=production_transport_factory)
    catalog = ClientCatalog()

    async def scenario() -> None:
        status = await hub.reconcile_all()
        assert status["mini"].state is AliasState.UNAVAILABLE
        assert hub.state_of("mini") is AliasState.UNAVAILABLE
        assert hub.last_error("mini") is not None
        assert hub.provider_clients().get("mini") is None
        hub.publish_catalog(catalog)
        servers = {item["alias"]: item for item in catalog.snapshot.servers_view()}
        assert servers["mini"]["catalog_available"] is False

        # A command against the offline alias refuses before dispatch.
        with pytest.raises(CommandError) as excinfo:
            await execute_command(
                _command("mini", "echo", catalog.revision, text="x"),
                catalog=catalog,
            )
        assert excinfo.value.code in {"alias_unknown", "alias_unavailable"}
        assert excinfo.value.execution_state == "not_started"

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


# ---------------------------------------------------------------------------
# Streamable HTTP chain
# ---------------------------------------------------------------------------


def test_http_chain_discovery_and_native_result_passthrough(tmp_path: Path) -> None:
    port = _free_port()
    mcp = _build_synthetic_http_server()
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp", json_response=True
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    )
    config_path = tmp_path / "config" / "config.yaml"
    _configure(config_path, {"url": f"http://127.0.0.1:{port}/mcp"})
    hub = McpHub(config_path, tmp_path, transport_factory=production_transport_factory)
    catalog = ClientCatalog()

    async def scenario() -> None:
        server_task = asyncio.create_task(server.serve())
        try:
            for _ in range(200):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started

            status = await hub.reconcile_all()
            assert status["mini"].state is AliasState.RUNNING
            hub.publish_catalog(catalog)
            tools = catalog.snapshot.tools_view("mini")
            assert [item["name"] for item in tools] == ["echo", "fail", "flood", "linger"]

            outcome = await execute_command(
                _command("mini", "echo", catalog.revision, text="over-http"),
                catalog=catalog,
            )
            dumped = outcome.result.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
            payload = json.loads(dumped["content"][0]["text"])
            assert payload["structuredContent"] == {"echo": "over-http"}
            assert payload["isError"] is False
            assert payload["futureField"] == [1, 2]
            assert dumped["resultType"] == "complete"

            outcome = await execute_command(
                _command("mini", "fail", catalog.revision),
                catalog=catalog,
            )
            assert outcome.result.is_error is True
            assert "tool says no" in outcome.result.content[0].text

            with pytest.raises(CommandError) as excinfo:
                await execute_command(
                    _command("mini", "flood", catalog.revision,
                             size=MAX_TOOL_RESULT_BYTES + 1024),
                    catalog=catalog,
                )
            assert excinfo.value.code == "result_too_large"
            assert excinfo.value.execution_state == "unknown"
        finally:
            await hub.forget("mini")
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=5)

    asyncio.run(asyncio.wait_for(scenario(), timeout=40))


# ---------------------------------------------------------------------------
# Admin: disabled entries are never spawned nor executable
# ---------------------------------------------------------------------------


def test_disabled_alias_is_not_spawned_until_re_enabled(tmp_path: Path) -> None:
    mini = tmp_path / "mini_mcp.py"
    mini.write_text(_MINI_STDIO, encoding="utf-8")
    config_path = tmp_path / "config" / "config.yaml"
    _configure(config_path, {"command": [sys.executable, str(mini)]})
    mcp_entry_set_enabled(config_path, "mini", False)
    hub = McpHub(config_path, tmp_path, transport_factory=production_transport_factory)
    catalog = ClientCatalog()

    async def scenario() -> None:
        status = await hub.reconcile_all()
        assert status["mini"].state is AliasState.DISABLED
        assert hub.provider_clients().get("mini") is None
        hub.publish_catalog(catalog)
        servers = {item["alias"]: item for item in catalog.snapshot.servers_view()}
        assert servers["mini"]["catalog_available"] is False
        with pytest.raises(CommandError) as excinfo:
            await execute_command(
                _command("mini", "echo", catalog.revision, text="x"),
                catalog=catalog,
            )
        assert excinfo.value.execution_state == "not_started"

        # Re-enable: the entry is kept, the alias becomes executable.
        mcp_entry_set_enabled(config_path, "mini", True)
        status = await hub.reconcile_all()
        assert status["mini"].state is AliasState.RUNNING
        hub.publish_catalog(catalog)
        outcome = await execute_command(
            _command("mini", "echo", catalog.revision, text="back"),
            catalog=catalog,
        )
        dumped = outcome.result.model_dump(mode="json", by_alias=True, exclude_none=True)
        payload = json.loads(dumped["content"][0]["text"])
        assert payload["structuredContent"] == {"echo": "back"}

    try:
        asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    finally:
        asyncio.run(hub.forget("mini"))
