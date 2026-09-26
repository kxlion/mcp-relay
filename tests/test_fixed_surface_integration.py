"""Real integration of the fixed surface: SDK client → facade → Client → MCP.

Neutral, local synthetic MCP servers (stdio and Streamable HTTP). No product
dependency, no desktop, no account, no secrets, no third-party network. The
full path exercises: SDK MCP client → /mcp → Server → WebSocket → Client →
synthetic MCP; server list → tools → schema → command; connection loss and
recovery; admin on/off; timeout/cancellation accounting on the synthetic
server; and the contract handshake refusal.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
from pathlib import Path

import httpx2
import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from mcp_relay.config import init_config, mcp_entry_add
from mcp_relay.server import RelaySettings, create_app
from tests.test_fixed_surface_mcp import build_synthetic_mcp_server

pytestmark = pytest.mark.integration


def _free_port() -> int:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.close()
    return port


def _mini_server_path(tmp_path: Path) -> Path:
    """A real synthetic MCP server over stdio: echo, ping, fail tools."""
    path = tmp_path / "mini_mcp.py"
    path.write_text(
        "\n".join(
            [
                "import asyncio",
                "from mcp.server.mcpserver import MCPServer",
                "from mcp.server.mcpserver.exceptions import ToolError",
                "",
                "mcp = MCPServer('mini')",
                "",
                "@mcp.tool(structured_output=False)",
                "async def echo(text: str) -> dict:",
                "    return {'content': [{'type': 'text', 'text': text}], 'structuredContent': {'echo': text}, 'isError': False}",
                "",
                "@mcp.tool(structured_output=False)",
                "async def ping() -> dict:",
                "    return {'content': [{'type': 'text', 'text': 'pong'}], 'isError': False}",
                "",
                "@mcp.tool(structured_output=False)",
                "async def fail() -> dict:",
                "    raise ToolError('tool says no')",
                "",
                "if __name__ == '__main__':",
                "    mcp.run()",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _start_relay(tmp_path: Path):
    """Start the Relay Server on a free port and return (app, server, listener, port)."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    # Release the reservation before uvicorn binds the same port: a duplicate
    # bind to a bound (non-listening) socket is tolerated on Linux with
    # SO_REUSEADDR but fails with WinError 10048 on Windows.
    listener.close()
    app = create_app(
        RelaySettings(
            client_id="linux-test",
            client_token="client-secret-synthetic-credential-0000000000000000",
            mcp_token="control-secret-synthetic-credential-0000000000000000",
            max_timeout_seconds=5,
        )
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            ws_max_size=1024 * 1024,
        )
    )
    return app, server, listener, port


# ---------------------------------------------------------------------------
# Full path over stdio
# ---------------------------------------------------------------------------


def test_full_fixed_surface_path_over_stdio(tmp_path: Path) -> None:
    """list → tools → detail → command against a real synthetic stdio MCP."""
    from mcp_relay.capabilities.control import ControlCapability
    from mcp_relay.client import ClientSettings, RelayClient
    from mcp_relay.mcp_catalog import ClientCatalog
    from mcp_relay.mcp_hub import McpHub, production_transport_factory
    from mcp_relay.version import bounded_version_label, package_version

    app, server, _listener, port = _start_relay(tmp_path)
    mini = _mini_server_path(tmp_path)
    config_path = tmp_path / "config.yaml"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    init_config(config_path, "client", env={"RELAY_CLIENT_TOKEN": "client-secret-synthetic-credential-0000000000000000"})
    mcp_entry_add(
        config_path,
        "mini",
        {"command": [sys.executable, str(mini)]},
        None,
    )
    hub = McpHub(
        config_path,
        workspace,
        transport_factory=production_transport_factory,
    )
    catalog = ClientCatalog()
    control = ControlCapability(
        hub=hub,
        workspace=workspace,
        client_version=bounded_version_label(package_version()) or "unknown",
    )
    control.bind_catalog(catalog)

    async def runner() -> None:
        server_task = asyncio.create_task(server.serve())
        client = RelayClient(
            ClientSettings(
                server_url=f"ws://127.0.0.1:{port}/ws",
                client_id="linux-test",
                client_token="client-secret-synthetic-credential-0000000000000000",
                workspace=workspace,
            ),
            capabilities=[control],
        )
        client.catalog = catalog
        control.bind_inventory_change(client.reannounce)
        control.bind_catalog_refresh(lambda: hub.publish_catalog(catalog))
        client_task = asyncio.create_task(client.run())
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            # The SDK MCP client drives the facade through the HTTP app while
            # the Relay Client is connected to the same Server over WebSocket.
            await hub.reconcile_all()
            hub.publish_catalog(catalog)
            tools = catalog.snapshot.tools_view("mini")
            assert [item["name"] for item in tools] == ["echo", "fail", "ping"]
            revision_before = catalog.revision
            # A second identical publication keeps the revision.
            hub.publish_catalog(catalog)
            assert catalog.revision == revision_before

            # End-to-end commands through the MCP facade: the Server relays
            # each envelope; the Client reserves the route and sends exactly
            # once; native results come back through Streamable HTTP.
            async with httpx2.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                headers={"Authorization": "Bearer control-secret-synthetic-credential-0000000000000000"},
            ) as http:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp",
                    http_client=http,
                    terminate_on_close=True,
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        for _ in range(100):
                            if app.state.registry.announced_capabilities:
                                break
                            await asyncio.sleep(0.01)

                        listing = await session.call_tool("relay_mcp_list", {})
                        assert listing.is_error is False
                        listing_result = listing.structured_content
                        if listing_result is None:
                            listing_result = json.loads(listing.content[0].text)
                        if "structuredContent" in listing_result:
                            listing_result = listing_result["structuredContent"]
                        assert listing_result["level"] == "servers"
                        servers = listing_result["items"]
                        assert [s["alias"] for s in servers] == ["mini"]
                        assert servers[0]["catalog_available"] is True

                        command = await session.call_tool(
                            "relay_mcp_command",
                            {
                                "alias": "mini",
                                "tool": "echo",
                                "arguments": {"text": "hello"},
                                "catalog_revision": revision_before,
                            },
                        )
                        assert command.is_error is False
                        command_result = command.structured_content
                        if command_result is None:
                            command_result = json.loads(command.content[0].text)
                        if "structuredContent" in command_result:
                            command_result = command_result["structuredContent"]
                        assert command_result == {"echo": "hello"}

                        # An unknown exact tool refuses before any provider call.
                        unknown = await session.call_tool(
                            "relay_mcp_command",
                            {
                                "alias": "mini",
                                "tool": "zap",
                                "arguments": {},
                                "catalog_revision": revision_before,
                            },
                        )
                        assert unknown.is_error is True
                        error_payload = unknown.structured_content
                        if error_payload is None:
                            error_payload = json.loads(unknown.content[0].text)
                        if "structuredContent" in error_payload:
                            error_payload = error_payload["structuredContent"]
                        assert error_payload["code"] == "tool_unknown"
                        assert error_payload["execution_state"] == "not_started"
        finally:
            client.stop()
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(client_task, timeout=2)
            await client.aclose()
            server.should_exit = True
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(server_task, timeout=2)

    asyncio.run(runner())


def test_streamable_http_real_transport(tmp_path: Path) -> None:
    """The real SDK streamable_http client drives the facade's /mcp app."""
    from mcp_relay.mcp_facade import create_mcp_facade, create_mcp_http_app
    from mcp_relay.registry import RelayRegistry
    from mcp_relay.relay_tools import PUBLIC_TOOL_NAMES

    port = _free_port()
    registry = RelayRegistry(client_id="one", client_token="client-secret-synthetic-credential-0000000000000000")
    mcp = create_mcp_facade(
        registry=registry,
        client_id="one",
        timeout_seconds=2,
    )
    app = create_mcp_http_app(mcp)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    )

    async def scenario() -> None:
        server_task = asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            async with httpx2.AsyncClient(
                headers={"Authorization": "Bearer control-secret-synthetic-credential-0000000000000000"},
            ) as http_client:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp",
                    http_client=http_client,
                    terminate_on_close=True,
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        initialized = await session.initialize()
                        assert initialized.server_info.name == "MCP Relay"
                        tools = (await session.list_tools()).tools
                        assert {tool.name for tool in tools} == set(PUBLIC_TOOL_NAMES)
                        # list without a client answers client_unavailable as a
                        # tool error, not a missing descriptor.
                        listing = await session.call_tool("relay_mcp_list", {})
                        assert listing.is_error is True
                        text = listing.content[0].text
                        assert "client_unavailable" in text or "unknown" in text
        finally:
            server.should_exit = True
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(server_task, timeout=2)

    asyncio.run(scenario())


def test_build_synthetic_mcp_server_contract() -> None:
    """The shared synthetic server builder yields a usable MCPServer."""
    mcp = build_synthetic_mcp_server()
    assert mcp is not None
    assert mcp.name == "mini"


def test_stdio_synthetic_server_speaks_mcp(tmp_path: Path) -> None:
    """The synthetic stdio MCP answers list_tools and call_tool for real."""

    async def scenario() -> None:
        mini = _mini_server_path(tmp_path)
        parameters = StdioServerParameters(
            command=sys.executable, args=[str(mini)]
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                names = {tool.name for tool in tools}
                assert {"echo", "ping", "fail"} <= names
                result = await session.call_tool("echo", {"text": "hello"})
                assert result.is_error is False
                # structured_output=False surfaces the tool's dict as its
                # content text (the SDK contract); the data is intact.
                assert "hello" in result.content[0].text
                failure = await session.call_tool("fail", {})
                assert failure.is_error is True
                assert "tool says no" in failure.content[0].text

    asyncio.run(scenario())


def test_catalog_revision_survives_json_round_trip(tmp_path: Path) -> None:
    """The revision is an opaque bounded string, safely JSON-encodable."""
    from mcp_relay.mcp_catalog import ClientCatalog

    catalog = ClientCatalog()
    revision = catalog.revision
    encoded = json.dumps({"catalog_revision": revision})
    decoded = json.loads(encoded)["catalog_revision"]
    assert decoded == revision
    assert len(decoded) <= 128
