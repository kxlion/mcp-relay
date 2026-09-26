from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcp_relay.client import ClientSettings, RelayClient
from mcp_relay.json_bounds import MAX_WS_MESSAGE_BYTES
from mcp_relay.output_models import ProviderToolResult
from mcp_relay.protocol import InvokeMessage
from mcp_relay.provider_tools import ProviderToolDescriptor
from mcp_relay.server import RelaySettings, create_app
from mcp_relay.version import package_version


class _EchoCapability:
    """Synthetic generic capability standing in for the removed builtins."""

    def __init__(self) -> None:
        self.tools = frozenset({"sample.echo"})
        self.closed = 0

    async def start(self) -> None:
        return None

    async def list_tools(self) -> list[ProviderToolDescriptor]:
        return [
            ProviderToolDescriptor(
                provider_name="sample",
                tool_name="echo",
                description="Synthetic generic capability",
                input_schema={
                    "type": "object",
                    "properties": {"echo": {"type": "string", "minLength": 1}},
                    "required": ["echo"],
                    "additionalProperties": False,
                },
            )
        ]

    async def invoke(self, message: InvokeMessage) -> dict[str, object]:
        return {"echo": message.arguments["echo"]}

    async def wait_unavailable(self) -> None:
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed += 1


class _EchoProvider:
    """Route provider calling the echo capability with the native result."""

    async def call_tool(
        self, tool_name: str, arguments: dict[str, object]
    ) -> ProviderToolResult:
        echo = arguments["echo"]
        assert isinstance(echo, str)
        return ProviderToolResult(
            content=[],
            structured_content={"echo": echo},
            is_error=False,
        )


@pytest.mark.integration
def test_real_loopback_server_starts_without_a_server_side_client_identity() -> None:
    async def scenario() -> None:
        try:
            server_settings = RelaySettings(
                mcp_token="mcp-secret-synthetic-credential-0000000000000000",
                client_token="client-secret-synthetic-credential-0000000000000000",
                mcp_bind_host="127.0.0.1",
                max_timeout_seconds=5,
            )
        except (TypeError, ValueError) as exc:
            pytest.fail(f"loopback server still requires a configured Client ID: {exc}")
            raise AssertionError("unreachable")

        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        app = create_app(server_settings)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="critical",
                ws_max_size=MAX_WS_MESSAGE_BYTES,
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            snapshot = await app.state.registry.status_snapshot()
            assert snapshot.client_id is None
            assert snapshot.connected is False
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=2)


    asyncio.run(scenario())


@pytest.mark.integration
def test_real_loopback_server_client_and_runner(tmp_path: Path) -> None:
    async def scenario() -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        server_settings = RelaySettings(
            client_id="linux-test",
            client_token="client-secret-synthetic-credential-0000000000000000",
            mcp_token="control-secret-synthetic-credential-0000000000000000",
            max_timeout_seconds=5,
        )
        app = create_app(server_settings)
        server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=port, log_level="critical",
                ws_max_size=MAX_WS_MESSAGE_BYTES,
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            client = RelayClient(
                ClientSettings(
                    server_url=f"ws://127.0.0.1:{port}/ws",
                    client_id="linux-test",
                    client_token="client-secret-synthetic-credential-0000000000000000",
                    workspace=tmp_path,
                ),
                capabilities=[_EchoCapability()],
            )
            # The third-party execution path (mcp.command) needs a catalog
            # with a route for the synthetic capability.
            from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog

            catalog = ClientCatalog()
            catalog.update_alias(
                AliasCatalog(
                    alias="sample",
                    enabled=True,
                    runtime_state="running",
                    transport="stdio",
                    entry={"command": ["/bin/sample"]},
                    last_error=None,
                    catalog_available=True,
                    discovery_error=None,
                    descriptors=(
                        ProviderToolDescriptor(
                            provider_name="sample",
                            tool_name="echo",
                            description="Synthetic generic capability",
                            input_schema={
                                "type": "object",
                                "properties": {"echo": {"type": "string", "minLength": 1}},
                                "required": ["echo"],
                                "additionalProperties": False,
                            },
                        ),
                    ),
                    provider=_EchoProvider(),
                )
            )
            client.catalog = catalog
            client_task = asyncio.create_task(client.run())
            headers = {"Authorization": "Bearer control-secret-synthetic-credential-0000000000000000"}
            async with httpx2.AsyncClient(headers=headers) as http:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp",
                    http_client=http,
                    terminate_on_close=True,
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        for _ in range(100):
                            if app.state.registry.last_heartbeat is not None:
                                break
                            await asyncio.sleep(0.01)
                        echo = await session.call_tool(
                            "relay_mcp_command",
                            {
                                "alias": "sample",
                                "tool": "echo",
                                "arguments": {"echo": "hello"},
                                "catalog_revision": catalog.revision,
                            },
                        )
                        assert echo.is_error is False
                        assert echo.structured_content == {"echo": "hello"}
                        # The Client learned the Server's package version from the
                        # existing authenticated handshake, not from any extra call.
                        assert client.server_version == package_version()
                        assert client.connection_metadata == {
                            "server_version": package_version()
                        }
                        # The System/Terminal builtin surface is gone: unknown
                        # provider routes fail closed through the catalog-backed MCP
                        # command instead of a direct invocation endpoint.
                        for alias, tool in (
                            ("sample", "ping"), ("provider", "unconfigured")
                        ):
                            forbidden = await session.call_tool(
                                "relay_mcp_command",
                                {
                                    "alias": alias,
                                    "tool": tool,
                                    "arguments": {},
                                    "catalog_revision": catalog.revision,
                                },
                            )
                            assert forbidden.is_error is True
            client.stop()
            await asyncio.wait_for(client_task, timeout=2)
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=2)

    asyncio.run(scenario())
