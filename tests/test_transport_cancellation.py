"""Cancellation semantics against the FastMCP-backed transports.

The historic owner-task mechanics are gone; the observable contracts are
replayed against real servers and real processes:

- cancelling a startup in flight closes the provisional transport;
- cancelling a close waiter does not abandon cleanup: a second close
  finishes it and the stdio child is reaped (no orphans);
- cancelling one concurrent startup does not poison the other.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mcp_relay.config import mcp_entry_add
from mcp_relay.mcp_hub import AliasLaunch, FastMcpClientTransport, McpHub
from tests.processes import process_exists

_MINI_SERVER = """\
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "echo text back",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1}},
            "required": ["text"],
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
    elif method == "tools/call":
        text = request["params"]["arguments"]["text"]
        result = {"content": [{"type": "text", "text": "echo:" + text}]}
    else:
        continue
    response = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
    sys.stdout.write(json.dumps(response) + "\\n")
    sys.stdout.flush()
"""


def test_cancelled_spawn_closes_provisional_transport(tmp_path: Path) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        closed = asyncio.Event()

        class Transport:
            async def list_tools(self, cursor: object = None) -> object:
                entered.set()
                await asyncio.Event().wait()

            async def call_tool(
                self, name: object, arguments: object
            ) -> object:
                raise AssertionError("no dispatch before discovery completes")

            async def close(self) -> None:
                closed.set()

        cfg = tmp_path / "relay.yaml"
        cfg.write_text(
            "relay_url: ws://localhost:9999/ws\n", encoding="utf-8"
        )
        cfg.chmod(0o600)
        mcp_entry_add(cfg, "probe", {"command": ["synthetic"]}, {})
        hub = McpHub(cfg, tmp_path, transport_factory=lambda launch: Transport())
        task = asyncio.create_task(hub.reconcile_all())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert "probe" not in hub.provider_clients()

    asyncio.run(asyncio.wait_for(scenario(), timeout=15))


def test_cancelled_close_waiter_does_not_orphan_the_child(tmp_path: Path) -> None:
    """Cancelling one close waiter leaves cleanup intact; close finishes."""
    script = tmp_path / "mini_server_pid.py"
    pid_file = tmp_path / "child.pid"
    script.write_text(
        "import os\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        + _MINI_SERVER,
        encoding="utf-8",
    )
    transport = FastMcpClientTransport(
        AliasLaunch(
            alias="mini",
            transport="stdio",
            argv=[sys.executable, str(script)],
            url=None,
            env={},
            cwd=None,
        ),
        init_timeout_seconds=10.0,
    )

    async def scenario() -> None:
        await transport.list_tools()
        close_task = asyncio.create_task(transport.close())
        await asyncio.sleep(0.01)  # let the close get underway
        close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_task
        # A second close finishes the cleanup; the waiter that was cancelled
        # did not leave the shutdown half-done.
        await asyncio.wait_for(transport.close(), timeout=10)

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))

    child_pid = int(pid_file.read_text())
    assert not process_exists(child_pid)


def test_cancelled_concurrent_startup_preserves_the_other_request(
    tmp_path: Path,
) -> None:
    """Two concurrent first requests: cancelling one keeps the other working."""
    script = tmp_path / "mini_server.py"
    script.write_text(_MINI_SERVER, encoding="utf-8")
    transport = FastMcpClientTransport(
        AliasLaunch(
            alias="mini",
            transport="stdio",
            argv=[sys.executable, str(script)],
            url=None,
            env={},
            cwd=None,
        ),
        init_timeout_seconds=10.0,
    )

    async def scenario() -> None:
        try:
            first = asyncio.create_task(transport.list_tools())
            second = asyncio.create_task(transport.list_tools())
            first.cancel()
            # A fast legacy handshake may finish before the cancel lands:
            # either outcome is honest. The contract is that cancelling one
            # startup never poisons the other request.
            try:
                await first
            except asyncio.CancelledError:
                pass
            tools = await asyncio.wait_for(second, timeout=10)
            assert [tool.name for tool in tools.tools] == ["echo"]
        finally:
            await transport.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))
