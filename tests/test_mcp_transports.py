"""The hub's FastMCP client transports against real servers.

These tests lock the observable contracts:

- a real synthetic stdio MCP server is spawned, listed, called, and the
  child process is reaped on close (no orphans);
- a real wire ``notifications/tools/list_changed`` reaches the bound
  ``on_tools_changed`` hook (upstream inventory invalidation wiring);
- an unspawnable command surfaces as an exception;
- close is idempotent and terminal; close-before-open is safe;
- cross-task open/close is bounded and leaves no "different task" errors;
- the same contracts hold over Streamable HTTP.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_relay.mcp_hub import (
    AliasLaunch,
    FastMcpClientTransport,
)
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


def _launch(script: Path, *, env: dict[str, str] | None = None) -> AliasLaunch:
    return AliasLaunch(
        alias="mini",
        transport="stdio",
        argv=[sys.executable, str(script)],
        url=None,
        env=env or {},
        cwd=None,
    )


def test_fastmcp_transport_spawns_a_synthetic_mcp_server_and_reaps_it(
    tmp_path: Path,
) -> None:
    script = tmp_path / "mini_server.py"
    script.write_text(_MINI_SERVER, encoding="utf-8")
    pid_file = tmp_path / "child.pid"
    script_with_pid = tmp_path / "mini_server_pid.py"
    script_with_pid.write_text(
        "import os\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        + _MINI_SERVER,
        encoding="utf-8",
    )
    transport = FastMcpClientTransport(
        _launch(script_with_pid), init_timeout_seconds=10.0
    )

    async def scenario() -> None:
        try:
            tools = await transport.list_tools()
            names = [tool.name for tool in tools.tools]
            assert names == ["echo"]
            result = await transport.call_tool("echo", {"text": "hello"})
            assert result.content[0].text == "echo:hello"
        finally:
            await transport.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))

    # No orphan process: the real stdio child was reaped by the close.
    child_pid = int(pid_file.read_text())
    assert not process_exists(child_pid)


def test_fastmcp_transport_surfaces_tools_list_changed(tmp_path: Path) -> None:
    """A real ``notifications/tools/list_changed`` reaches the bound callback."""
    script = tmp_path / "mini_server_notify.py"
    script.write_text(
        _MINI_SERVER.replace(
            'elif method == "tools/call":',
            '''elif method == "tools/call":
        if request["params"]["arguments"]["text"] == "notify":
            notification = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
            sys.stdout.write(json.dumps(notification) + "\\n")
            sys.stdout.flush()''',
        ),
        encoding="utf-8",
    )
    transport = FastMcpClientTransport(
        _launch(script), init_timeout_seconds=10.0
    )
    changed = asyncio.Event()

    async def on_changed() -> None:
        changed.set()

    transport.on_tools_changed = on_changed

    async def scenario() -> None:
        try:
            await transport.list_tools()
            # The notification rides the tools/call round-trip: the client's
            # message handler processes it before the response resolves.
            await asyncio.wait_for(
                transport.call_tool("echo", {"text": "notify"}), timeout=5
            )
            assert await asyncio.wait_for(changed.wait(), timeout=5), (
                "tools/list_changed never surfaced"
            )
        finally:
            await transport.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


def test_fastmcp_transport_reports_unspawnable_commands(tmp_path: Path) -> None:
    launch = AliasLaunch(
        alias="ghost",
        transport="stdio",
        argv=[str(tmp_path / "does-not-exist")],
        url=None,
        env={},
        cwd=None,
    )
    transport = FastMcpClientTransport(launch, init_timeout_seconds=5.0)

    async def scenario() -> None:
        try:
            await transport.list_tools()
        except Exception:
            return
        raise AssertionError("expected the spawn failure to surface")

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


def test_fastmcp_transport_close_is_terminal_idempotent_and_pre_open_safe(
    tmp_path: Path,
) -> None:
    script = tmp_path / "mini_server.py"
    script.write_text(_MINI_SERVER, encoding="utf-8")

    async def scenario() -> None:
        # close-before-open is safe and idempotent.
        never_opened = FastMcpClientTransport(
            _launch(script), init_timeout_seconds=5.0
        )
        await never_opened.close()
        await never_opened.close()

        transport = FastMcpClientTransport(
            _launch(script), init_timeout_seconds=10.0
        )
        await transport.list_tools()
        await transport.close()
        await transport.close()  # idempotent
        # Close is terminal: the transport is unusable afterwards.
        with pytest.raises(RuntimeError):
            await transport.list_tools()
        with pytest.raises(RuntimeError):
            await transport.call_tool("echo", {"text": "x"})
        await transport.close()  # still idempotent after terminal use

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


def test_fastmcp_transport_cross_task_close_reaps_the_child(tmp_path: Path) -> None:
    """Open in one task, close from another: no 'different task' escape."""
    script = tmp_path / "mini_server_pid.py"
    pid_file = tmp_path / "child.pid"
    script.write_text(
        "import os\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        + _MINI_SERVER,
        encoding="utf-8",
    )
    transport = FastMcpClientTransport(
        _launch(script), init_timeout_seconds=10.0
    )

    async def scenario() -> None:
        async def open_and_use() -> object:
            await transport.list_tools()
            return await transport.call_tool("echo", {"text": "cross-task"})

        result = await asyncio.create_task(open_and_use())
        assert result.content[0].text == "echo:cross-task"
        # Close from a different task than the one that opened the session.
        close_task = asyncio.create_task(transport.close())
        await asyncio.wait_for(close_task, timeout=10)
        # Idempotent second close from yet another task.
        await asyncio.create_task(transport.close())

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))

    child_pid = int(pid_file.read_text())
    assert not process_exists(child_pid)


def test_fastmcp_transport_http_lifecycle(tmp_path: Path) -> None:
    """Streamable HTTP: list, concurrent calls, cross-task close, terminal."""
    runner = '''
import asyncio
import socket
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp_relay.mcp_hub import FastMcpClientTransport, AliasLaunch

async def main():
    mcp = MCPServer("synthetic")
    @mcp.tool()
    async def echo(text: str) -> str:
        return text
    app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    launch = AliasLaunch("mini", "streamable_http", None,
                         f"http://127.0.0.1:{port}/mcp", {}, None)
    transport = FastMcpClientTransport(launch, init_timeout_seconds=2)
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(.01)
        assert server.started
        tools = await asyncio.create_task(transport.list_tools())
        assert [t.name for t in tools.tools] == ["echo"]
        results = await asyncio.gather(
            *(asyncio.create_task(transport.call_tool("echo", {"text": str(i)}))
              for i in range(3))
        )
        assert [r.content[0].text for r in results] == ["0", "1", "2"]
    finally:
        await asyncio.create_task(transport.close())
        server.should_exit = True
        await asyncio.wait_for(serving, 3)
        listener.close()
    # Public contract: close is terminal and idempotent; no leaked session.
    try:
        await transport.call_tool("echo", {"text": "after-close"})
        raise SystemExit("transport still usable after close")
    except RuntimeError:
        pass
    await transport.close()
    print("http-lifecycle-ok")

asyncio.run(main())
'''
    result = subprocess.run(
        [sys.executable, "-c", runner], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "http-lifecycle-ok" in result.stdout
    assert "different task" not in result.stderr


@pytest.mark.parametrize("broken_inventory", [False, True, "initialize_timeout"])
def test_real_sdk_cross_task_cleanup_is_bounded(tmp_path: Path, broken_inventory):
    """Subprocess deadline also catches a cancellation-insensitive deadlock."""
    script = tmp_path / "mini_server.py"
    server = _MINI_SERVER
    if broken_inventory == "initialize_timeout":
        server = server.replace('if method == "initialize":', 'if method == "initialize":\n        continue')
    elif broken_inventory:
        server = server.replace('result = {"tools": TOOLS}', 'result = {"tools": 42}')
    script.write_text(server, encoding="utf-8")
    runner = '''
import asyncio
import sys
from pathlib import Path
from mcp_relay.config import mcp_entry_add, mcp_entry_set_enabled
from mcp_relay.mcp_hub import McpHub, AliasState, production_transport_factory

async def main():
    root = Path(sys.argv[1])
    cfg = root / "relay.yaml"
    cfg.write_text("relay_url: ws://localhost:9999/ws" + chr(10) + "workspace: " + str(root) + chr(10))
    cfg.chmod(0o600)
    mcp_entry_add(cfg, "mini", {"command": [sys.executable, str(root / "mini_server.py")]}, {})
    launches = []
    def factory(launch):
        transport = production_transport_factory(launch, timeout_seconds=2)
        launches.append(transport)
        return transport
    hub = McpHub(cfg, root, transport_factory=factory, provider_timeout_seconds=3)
    await asyncio.create_task(hub.reconcile_all())
    broken = sys.argv[2] != "False"
    assert hub.state_of("mini") == (AliasState.UNAVAILABLE if broken else AliasState.RUNNING)
    assert len(launches) == (3 if broken else 1)
    if not broken:
        result = await launches[0].call_tool("echo", {"text": "cross-task"})
        assert result.content[0].text == "echo:cross-task"
        mcp_entry_set_enabled(cfg, "mini", False)
        await asyncio.create_task(hub.reconcile_all())
        assert hub.state_of("mini") == AliasState.DISABLED
        mcp_entry_set_enabled(cfg, "mini", True)
        await asyncio.create_task(hub.reconcile_all())
        assert hub.state_of("mini") == AliasState.RUNNING
        assert len(launches) == 2
    await asyncio.create_task(hub.forget("mini"))
    # Public contract: after forget() every alias is stopped; a stopped alias
    # is not routable and its runtime state is gone.
    assert hub.runtime_states().get("mini") is None
    print("lifecycle-ok")

asyncio.run(main())
'''
    result = subprocess.run(
        [sys.executable, "-c", runner, str(tmp_path), str(broken_inventory)],
        capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "lifecycle-ok" in result.stdout
    assert "different task" not in result.stderr
