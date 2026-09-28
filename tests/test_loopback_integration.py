from __future__ import annotations

import asyncio
import socket

import pytest
import uvicorn

from mcp_relay.json_bounds import MAX_WS_MESSAGE_BYTES
from mcp_relay.server import RelaySettings
from tests.composite_app import create_app


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
