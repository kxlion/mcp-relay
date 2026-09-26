"""Loopback integration of the control surface: facade -> client -> hub -> alias."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx2
import pytest
import uvicorn
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcp_relay.capabilities.control import ControlCapability
from mcp_relay.client import ClientSettings, RelayClient, _run_client
from mcp_relay.json_bounds import MAX_WS_MESSAGE_BYTES
from mcp_relay.mcp_catalog import ClientCatalog
from mcp_relay.mcp_hub import McpHub, production_transport_factory
from mcp_relay.server import RelaySettings, create_app

_MINI_SERVER = """\
import json
import sys

TOOLS = [
    {
        "name": "ping",
        "description": "p",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
]

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mini", "version": "0.0.1"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    else:
        continue
    response = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
    sys.stdout.write(json.dumps(response) + "\\n")
    sys.stdout.flush()
"""


@pytest.mark.integration
def test_run_client_registers_the_control_capability(tmp_path: Path) -> None:
    """Regression: _run_client must build RelayClient AFTER the capability append.

    RelayClient snapshots the capability list in __init__; a control
    capability appended after construction is invisible, and every routed
    verb (mcp.list, client.status, admin verbs) fails with
    "unsupported provider tool" — exactly the e2e failure of 2026-09-08.
    """
    settings = ClientSettings(
        server_url="ws://127.0.0.1:1/ws",
        client_id="d",
        client_token='client-synthetic-credential-0000000000000000',
        workspace=tmp_path,
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("client: {}\n", encoding="utf-8")

    captured: dict[str, object] = {}

    class _ProbeClient(RelayClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            captured["capability_ids"] = list(self._unique_capabilities)

    with (
        patch("mcp_relay.client.RelayClient", _ProbeClient),
        patch(
            "mcp_relay.capabilities.control.ControlCapability.start",
            new=AsyncMock(),
        ),
        patch(
            "mcp_relay.capabilities.control.ControlCapability.wait_unavailable",
            new=AsyncMock(),
        ),
        patch("mcp_relay.client._run_with_signal_handlers", new=AsyncMock()),
        patch(
            "mcp_relay.client._refresh_catalog",
            new=lambda *args: None,
        ),
    ):
        asyncio.run(_run_client(settings, config_path=config_path))
    # The control capability was registered in the client snapshot.
    assert len(captured["capability_ids"]) == 1


def test_real_loopback_control_surface_end_to_end(tmp_path: Path) -> None:
    """Drive every control tool through the real server/client round-trip."""

    def _private(path: Path) -> None:
        if path.parent.name:
            import os

            if os.name != "nt":
                path.chmod(0o600)

    config_path = tmp_path / "config" / "config.yaml"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "relay_url": "ws://127.0.0.1:1/ws",
                "workspace": str(workspace),
            }
        ),
        encoding="utf-8",
    )
    _private(config_path)

    mini_server = tmp_path / "mini_server.py"
    mini_server.write_text(_MINI_SERVER, encoding="utf-8")

    async def _resolver(source: str, version: str | None) -> None:
        raise AssertionError("no source aliases in this scenario")

    hub = McpHub(
        config_path,
        workspace,
        transport_factory=production_transport_factory,
        source_resolver=_resolver,
        provider_timeout_seconds=5.0,
    )
    catalog = ClientCatalog()
    # The bench YAML carries ``client.admin: true``: the fail-closed default
    # is locked, and this end-to-end scenario exercises the admin verbs.
    control = ControlCapability(
        hub=hub,
        workspace=workspace,
        client_version="0.0.0",
        admin_enabled=True,
    )
    control.bind_catalog(catalog)
    # Mirror the production wiring: the catalog refresh callback and the
    # initial publication happen when the Client starts from YAML.
    control.bind_catalog_refresh(lambda: hub.publish_catalog(catalog))

    async def scenario() -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        server_settings = RelaySettings(
            client_id="linux-test",
            client_token='client-secret-synthetic-credential-0000000000000000',
            mcp_token='control-secret-synthetic-credential-0000000000000000',
            max_timeout_seconds=10,
        )
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
            settings = ClientSettings(
                server_url=f"ws://127.0.0.1:{port}/ws",
                client_id="linux-test",
                client_token='client-secret-synthetic-credential-0000000000000000',
                workspace=workspace,
            )

            async def resolver():
                return hub.provider_clients()

            client = RelayClient(
                settings,
                capabilities=[control],
                provider_resolver=resolver,
            )
            control.bind_inventory_change(client.reannounce)
            client_task = asyncio.create_task(client.run())
            try:
                headers = {"Authorization": 'Bearer control-secret-synthetic-credential-0000000000000000'}
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

                            async def invoke(tool_name: str, arguments: dict) -> dict:
                                result = await session.call_tool(tool_name, arguments)
                                assert result.is_error is False, result
                                if result.structured_content is not None:
                                    return result.structured_content
                                assert result.content
                                payload = json.loads(result.content[0].text)
                                if "structuredContent" in payload:
                                    return payload["structuredContent"]
                                return payload

                            # relay_client_status: end-to-end ping with useful payload.
                            status = await invoke("relay_client_status", {})
                            assert status["client"]["protocol"] == 1
                            assert status["client"]["workspace"] == str(workspace)
                            assert status["disk_differs"] == []

                            # relay_mcp_add: validate -> YAML commit -> spawn stdio.
                            add = await invoke(
                                "relay_mcp_add",
                                {
                                    "alias": "mini",
                                    "entry": {
                                        "command": [sys.executable, str(mini_server)]
                                    },
                                },
                            )
                            assert add["status"] == "running"

                            listing = await invoke("relay_mcp_list", {})
                            assert listing["level"] == "servers"
                            assert [s["alias"] for s in listing["items"]] == ["mini"]
                            mini = listing["items"][0]
                            assert mini["runtime_state"] == "running"
                            assert mini["catalog_available"] is True
                            assert mini["transport"] == "stdio"
                            assert mini["entry"]["command"] == [
                                sys.executable,
                                str(mini_server),
                            ]

                            # The re-announcement reached the Server registry: the
                            # fixed surface counters reflect the connected client.
                            snapshot = await app.state.registry.status_snapshot()
                            assert snapshot.client_operations >= 1
                            assert snapshot.public_tools >= 1

                            # enable/disable round trip.
                            disabled = await invoke(
                                "relay_mcp_disable", {"alias": "mini"}
                            )
                            assert disabled["runtime_state"] == "disabled"
                            enabled = await invoke(
                                "relay_mcp_enable", {"alias": "mini"}
                            )
                            assert enabled["runtime_state"] == "running"

                            # relay_mcp_delete with strict existence.
                            deleted = await invoke(
                                "relay_mcp_delete", {"alias": "mini"}
                            )
                            assert deleted == {"alias": "mini", "status": "deleted"}
                            unknown_result = await session.call_tool(
                                "relay_mcp_delete", {"alias": "mini"}
                            )
                            assert unknown_result.is_error is False
                            assert unknown_result.structured_content["code"] == "alias_unknown"
            finally:
                client.stop()
                await asyncio.wait_for(client_task, timeout=2)
                await client.aclose()
                await hub.forget("mini")
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=2)

    asyncio.run(scenario())
