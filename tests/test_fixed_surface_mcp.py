"""Shared synthetic MCP server used by the fixed-surface integration tests."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


def build_synthetic_mcp_server() -> MCPServer:
    """A neutral synthetic MCP server: echo (native), ping, fail (raises)."""
    mcp = MCPServer("mini")

    @mcp.tool(structured_output=False)
    async def echo(text: str) -> dict:
        """Return the caller's text unchanged inside a native result."""
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"echo": text},
            "isError": False,
        }

    @mcp.tool(structured_output=False)
    async def ping() -> dict:
        """Answer 'pong'."""
        return {
            "content": [{"type": "text", "text": "pong"}],
            "isError": False,
        }

    @mcp.tool(structured_output=False)
    async def fail() -> dict:
        """Raise: the SDK renders a native isError result from the error."""
        raise ToolError("tool says no")

    return mcp
