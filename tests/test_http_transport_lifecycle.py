"""Real HTTP SDK lifecycle in a subprocess with an external deadlock deadline.

Subprocess-isolated replay of the HTTP transport contracts: list,
concurrent calls, cross-task close, terminal semantics.
"""

from __future__ import annotations

import subprocess
import sys


def test_http_sdk_cross_task_close_and_concurrent_calls() -> None:
    runner = '''
import asyncio
import socket
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp_relay.mcp_hub import AliasLaunch, FastMcpClientTransport

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
