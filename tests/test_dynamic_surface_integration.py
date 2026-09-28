"""End to end: SDK MCP client → /mcp → Server → WebSocket → Client → MCP server.

Neutral synthetic MCP servers only (stdio and Streamable HTTP): no product
dependency, no account, no network beyond loopback.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client
from sse_starlette.sse import AppStatus

from mcp_relay.client import ClientSettings, build_client
from mcp_relay.config import init_config, mcp_entry_add, set_value
from mcp_relay.server import RelaySettings
from tests.composite_app import create_app

pytestmark = pytest.mark.integration

CLIENT_TOKEN = "client-secret-synthetic-credential-0000000000000000"
MCP_TOKEN = "control-secret-synthetic-credential-0000000000000000"

MINI_SERVER = """
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

mcp = MCPServer('mini')

@mcp.tool()
async def echo(text: str) -> dict[str, str]:
    '''Return the text unchanged.'''
    return {'echo': text}

@mcp.tool(structured_output=False)
async def ping() -> str:
    '''Answer pong.'''
    return 'pong'

@mcp.tool(name='files.read', structured_output=False)
async def files_read() -> str:
    '''A tool whose name is not portable as-is.'''
    return 'read'

@mcp.tool(structured_output=False)
async def fail() -> str:
    '''Always fails.'''
    raise ToolError('tool says no')

if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        mcp.run(transport='streamable-http', port=int(sys.argv[1]))
    else:
        mcp.run()
"""


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@dataclass
class Relay:
    session: ClientSession
    app: Any
    client: Any
    config_path: Path
    notifications: list[str] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    async def tool_names(self) -> list[str]:
        return [tool.name for tool in (await self.session.list_tools()).tools]

    async def wait_for_tools(self, predicate: Any, timeout: float = 15.0) -> list[str]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            names = await self.tool_names()
            if predicate(names):
                return names
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"tools never matched: {names}")
            await asyncio.sleep(0.05)


def _write_config(tmp_path: Path, entry: dict[str, Any], *, admin: bool) -> Path:
    config_path = tmp_path / "config.yaml"
    init_config(config_path, "client", env={"RELAY_CLIENT_TOKEN": CLIENT_TOKEN})
    mcp_entry_add(config_path, "mini", entry, None)
    set_value(config_path, "client", "admin", "true" if admin else "false")
    return config_path


@contextlib.asynccontextmanager
async def relay(tmp_path: Path, config_path: Path) -> AsyncIterator[Relay]:
    # sse-starlette flags every SSE stream for exit once any uvicorn server in
    # the process shuts down; tests start several servers in one process.
    AppStatus.should_exit = False
    port = _free_port()
    app = create_app(
        RelaySettings(
            client_id="test-client",
            client_token=CLIENT_TOKEN,
            mcp_token=MCP_TOKEN,
            max_timeout_seconds=10,
        )
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    client = build_client(
        ClientSettings(
            server_url=f"ws://127.0.0.1:{port}/ws",
            client_id="test-client",
            client_token=CLIENT_TOKEN,
            workspace=workspace,
        ),
        config_path=config_path,
    )
    server_task = asyncio.create_task(server.serve())
    client_task: asyncio.Task[None] | None = None
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        client.start_initial_reconciliation()
        client_task = asyncio.create_task(client.run())
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {MCP_TOKEN}"}
        ) as http:
            async with streamable_http_client(
                f"http://127.0.0.1:{port}/mcp", http_client=http
            ) as (read_stream, write_stream):
                state: dict[str, Relay] = {}

                async def on_message(message: Any) -> None:
                    if isinstance(message, mcp_types.ToolListChangedNotification):
                        state["relay"].notifications.append("tools/list_changed")
                        state["relay"].changed.set()

                async with ClientSession(
                    read_stream, write_stream, message_handler=on_message
                ) as session:
                    await session.initialize()
                    state["relay"] = Relay(session, app, client, config_path)
                    yield state["relay"]
    finally:
        client.stop()
        if client_task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(client_task, timeout=5)
        await client.aclose()
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(server_task, timeout=5)


def _mini(tmp_path: Path) -> Path:
    path = tmp_path / "mini_mcp.py"
    path.write_text(MINI_SERVER, encoding="utf-8")
    return path


def test_stdio_tools_are_published_and_callable(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path, {"command": [sys.executable, str(_mini(tmp_path))]}, admin=False
    )

    async def scenario() -> None:
        async with relay(tmp_path, config) as r:
            names = await r.wait_for_tools(lambda n: "mini_echo" in n)
            hashed = [name for name in names if name.startswith("mini_files_read_")]
            assert len(hashed) == 1 and len(hashed[0]) == len("mini_files_read_") + 8
            assert sorted(set(names) - set(hashed)) == [
                "mini_echo",
                "mini_fail",
                "mini_ping",
                "relay_registry_search",
                "relay_status",
            ]
            listed = {t.name: t for t in (await r.session.list_tools()).tools}
            assert listed["mini_echo"].description == "Return the text unchanged."
            assert listed["mini_echo"].input_schema["properties"]["text"]["type"] == "string"

            echo = await r.session.call_tool("mini_echo", {"text": "hello"})
            assert echo.is_error is False
            assert echo.structured_content == {"echo": "hello"}
            assert listed["mini_echo"].output_schema is not None

            renamed = await r.session.call_tool(hashed[0], {})
            assert renamed.content[0].text == "read"

            failed = await r.session.call_tool("mini_fail", {})
            assert failed.is_error is True
            assert "tool says no" in failed.content[0].text

            status = await r.session.call_tool("relay_status", {})
            report = status.structured_content
            assert report["client"]["connected"] is True
            assert report["client"]["report"] == "live"
            assert report["client"]["admin"] is False
            assert report["server"]["published_tools"] == 4
            [server] = report["mcp_servers"]
            assert server["alias"] == "mini"
            assert server["runtime_state"] == "running"
            assert server["published_tools"] == 4

    asyncio.run(scenario())


def test_tools_allowlist_and_description_override(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        {
            "command": [sys.executable, str(_mini(tmp_path))],
            "tools": {"echo": {"description": "Echo for the agent."}, "ping": None},
        },
        admin=False,
    )

    async def scenario() -> None:
        async with relay(tmp_path, config) as r:
            names = await r.wait_for_tools(lambda n: "mini_echo" in n)
            assert sorted(names) == [
                "mini_echo",
                "mini_ping",
                "relay_registry_search",
                "relay_status",
            ]
            listed = {t.name: t for t in (await r.session.list_tools()).tools}
            assert listed["mini_echo"].description == "Echo for the agent."
            assert listed["mini_ping"].description == "Answer pong."

    asyncio.run(scenario())


def test_admin_tools_follow_the_local_switch_and_changes_are_announced(
    tmp_path: Path,
) -> None:
    config = _write_config(
        tmp_path, {"command": [sys.executable, str(_mini(tmp_path))]}, admin=True
    )

    async def scenario() -> None:
        async with relay(tmp_path, config) as r:
            names = await r.wait_for_tools(lambda n: "mini_echo" in n)
            assert {
                "relay_mcp_add",
                "relay_mcp_modify",
                "relay_mcp_delete",
                "relay_mcp_enable",
                "relay_mcp_disable",
            } <= set(names)

            r.changed.clear()
            disabled = await r.session.call_tool("relay_mcp_disable", {"alias": "mini"})
            assert disabled.is_error is False
            assert disabled.structured_content["runtime_state"] == "disabled"
            await asyncio.wait_for(r.changed.wait(), timeout=10)
            names = await r.wait_for_tools(lambda n: "mini_echo" not in n)
            assert not [name for name in names if name.startswith("mini_")]

            refused = await r.session.call_tool("relay_mcp_delete", {"alias": "nope"})
            assert refused.is_error is True
            assert '"alias_unknown"' in refused.content[0].text

    asyncio.run(scenario())


def test_admin_tools_hidden_when_disabled_and_disconnect_is_announced(
    tmp_path: Path,
) -> None:
    config = _write_config(
        tmp_path, {"command": [sys.executable, str(_mini(tmp_path))]}, admin=False
    )

    async def scenario() -> None:
        async with relay(tmp_path, config) as r:
            names = await r.wait_for_tools(lambda n: "mini_echo" in n)
            assert not [name for name in names if name.startswith("relay_mcp_")]
            direct = await r.session.call_tool("relay_mcp_add", {"alias": "x", "entry": {}})
            assert direct.is_error is True

            live = (await r.session.call_tool("relay_status", {})).structured_content
            assert live["client"]["report"] == "live"

            r.changed.clear()
            r.client.stop()
            await asyncio.wait_for(r.changed.wait(), timeout=10)
            names = await r.wait_for_tools(lambda n: "mini_echo" not in n)
            assert sorted(names) == ["relay_registry_search", "relay_status"]
            status = (await r.session.call_tool("relay_status", {})).structured_content
            assert status["client"]["connected"] is False
            assert status["client"]["report"] == "cached"
            assert status["server"]["published_tools"] == 0

    asyncio.run(scenario())


def test_streamable_http_mcp_server(tmp_path: Path) -> None:
    port = _free_port()
    process = __import__("subprocess").Popen(
        [sys.executable, str(_mini(tmp_path)), str(port)],
        stdout=__import__("subprocess").DEVNULL,
        stderr=__import__("subprocess").DEVNULL,
    )
    try:
        for _ in range(200):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            __import__("time").sleep(0.05)
        config = _write_config(
            tmp_path, {"url": f"http://127.0.0.1:{port}/mcp"}, admin=False
        )

        async def scenario() -> None:
            async with relay(tmp_path, config) as r:
                await r.wait_for_tools(lambda n: "mini_ping" in n)
                ping = await r.session.call_tool("mini_ping", {})
                assert ping.content[0].text == "pong"

        asyncio.run(scenario())
    finally:
        process.terminate()
        process.wait(timeout=10)
