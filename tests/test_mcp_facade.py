"""MCP facade against a scripted Client: surface, dispatch, errors and status."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from fastmcp import Client

from mcp_relay.mcp_facade import create_mcp_facade
from mcp_relay.protocol import (
    RELAY_CONTRACT,
    Capabilities,
    Catalog,
    ClientError,
    ClientResult,
    Progress,
    Register,
)
from mcp_relay.registry import RelayRegistry

ADMIN_TOOLS = {
    "relay_mcp_add",
    "relay_mcp_modify",
    "relay_mcp_delete",
    "relay_mcp_enable",
    "relay_mcp_disable",
}

Responder = Callable[[dict[str, Any]], dict[str, Any] | None]


class ScriptedClient:
    """A registered socket that answers each invoke through ``respond``."""

    def __init__(self, registry: RelayRegistry, respond: Responder | None = None) -> None:
        self.registry = registry
        self.respond = respond or (lambda _: None)
        self.invokes: list[dict[str, Any]] = []
        self.cancels: list[str] = []

    async def send_json(self, message: object) -> None:
        assert isinstance(message, dict)
        if message.get("type") == "cancel":
            self.cancels.append(message["request_id"])
        if message.get("type") != "invoke":
            return
        self.invokes.append(message)
        reply = self.respond(message)
        if reply is not None:
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(self._answer(message["request_id"], reply))
            )

    async def _answer(self, request_id: str, reply: dict[str, Any]) -> None:
        if "error" in reply:
            await self.registry.handle_error(
                ClientError(version=2, type="error", request_id=request_id, error=reply["error"])
            )
        else:
            for value in reply.get("progress", ()):
                await self.registry.handle_progress(
                    Progress(version=2, type="progress", request_id=request_id, progress=value)
                )
            await self.registry.handle_result(
                ClientResult(version=2, type="result", request_id=request_id, result=reply["result"])
            )

    async def connect(self, *, admin: bool = False, tools: list[dict[str, Any]] | None = None) -> None:
        await self.registry.register(
            self, Register(version=1, type="register", client_id="one", relay_contract=RELAY_CONTRACT)
        )
        await self.registry.set_capabilities(
            self,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                client_version="0.2.0",
                admin=admin,
            ),
        )
        await self.registry.set_catalog(
            self, Catalog(version=2, type="catalog", tools=tools or [])
        )


def _tool(name: str = "fs_read", tool: str = "read") -> dict[str, Any]:
    return {
        "name": name,
        "alias": "fs",
        "tool": tool,
        "description": "Read a file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "annotations": {"readOnlyHint": True},
    }


def _ok(text: str = "done", structured: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": False}
    if structured is not None:
        result["structuredContent"] = structured
    return {"result": result}


def _facade(timeout: float = 2.0) -> tuple[RelayRegistry, Any]:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    return registry, create_mcp_facade(registry=registry, timeout_seconds=timeout)


def _run(scenario: Callable[[], Any]) -> None:
    asyncio.run(scenario())


async def _names(client: Client) -> set[str]:
    return {tool.name for tool in await client.list_tools()}


def test_offline_surface_is_status_and_registry_search_only() -> None:
    registry, mcp = _facade()

    async def scenario() -> None:
        async with Client(mcp) as client:
            assert await _names(client) == {"relay_status", "relay_registry_search"}
            status = (await client.call_tool("relay_status", {})).structured_content
            assert status["client"]["connected"] is False
            assert status["client"]["report"] == "unavailable"
            assert status["mcp_servers"] is None
            assert status["server"]["relay_contract"] == RELAY_CONTRACT

    _run(scenario)


def test_client_tools_are_published_natively_with_their_schema() -> None:
    registry, mcp = _facade()
    scripted = ScriptedClient(registry)

    async def scenario() -> None:
        await scripted.connect(tools=[_tool()])
        async with Client(mcp) as client:
            listed = {tool.name: tool for tool in await client.list_tools()}
            assert set(listed) == {"relay_status", "relay_registry_search", "fs_read"}
            read = listed["fs_read"]
            assert read.description == "Read a file."
            assert read.input_schema["required"] == ["path"]
            assert read.annotations.read_only_hint is True

    _run(scenario)


def test_admin_tools_are_listed_only_when_the_client_allows_them() -> None:
    registry, mcp = _facade()
    scripted = ScriptedClient(registry, lambda _: _ok(structured={"alias": "x"}))

    async def scenario() -> None:
        async with Client(mcp) as client:
            await scripted.connect(admin=False)
            assert not ADMIN_TOOLS & await _names(client)
            await registry.disconnect(scripted)
            await scripted.connect(admin=True)
            assert ADMIN_TOOLS <= await _names(client)
            result = await client.call_tool("relay_mcp_disable", {"alias": "fs"})
            assert result.structured_content == {"alias": "x"}
            assert scripted.invokes[-1]["tool_name"] == "mcp.disable"
            assert scripted.invokes[-1]["arguments"] == {"alias": "fs"}

    _run(scenario)


def test_relayed_call_sends_the_exact_identity_and_returns_the_native_result() -> None:
    registry, mcp = _facade()
    scripted = ScriptedClient(registry, lambda _: {**_ok("body", {"k": 1}), "progress": [50]})

    async def scenario() -> None:
        await scripted.connect(tools=[_tool("fs_read_1a2b3c4d", "read.v2")])
        async with Client(mcp) as client:
            result = await client.call_tool("fs_read_1a2b3c4d", {"path": "/tmp/a"})
            assert result.content[0].text == "body"
            assert result.structured_content == {"k": 1}
        [invoke] = scripted.invokes
        assert invoke["tool_name"] == "mcp.command"
        assert invoke["arguments"] == {
            "alias": "fs",
            "tool": "read.v2",
            "arguments": {"path": "/tmp/a"},
        }

    _run(scenario)


@pytest.mark.parametrize(
    ("reply", "code", "state", "message"),
    [
        (
            {"error": {"code": "tool_unknown", "message": "no such tool", "execution_state": "not_started"}},
            "tool_unknown",
            "not_started",
            "no such tool",
        ),
        (
            {"error": {"code": "weird_code", "message": "leaky detail", "execution_state": "unknown"}},
            "execution_failed",
            "unknown",
            "client invocation failed",
        ),
        (
            {"error": {"code": "busy", "message": "an action is already running", "execution_state": "not_started"}},
            "client_busy",
            "not_started",
            "client invocation failed",
        ),
        (None, "timeout", "unknown", "invocation timed out"),
    ],
)
def test_client_failures_become_closed_error_results(
    reply: dict[str, Any] | None, code: str, state: str, message: str
) -> None:
    registry, mcp = _facade(timeout=0.2)
    scripted = ScriptedClient(registry, lambda _: reply)

    async def scenario() -> None:
        await scripted.connect(tools=[_tool()])
        async with Client(mcp) as client:
            result = await client.call_tool("fs_read", {"path": "x"}, raise_on_error=False)
        assert result.is_error is True
        assert result.content[0].text == (
            f'{{"code":"{code}","message":"{message}","execution_state":"{state}"}}'
        )
        if reply is None:
            assert scripted.cancels == [scripted.invokes[0]["request_id"]]

    _run(scenario)


def test_status_probes_the_client_live_and_falls_back_to_cache() -> None:
    registry, mcp = _facade()
    report = {
        "version": "0.2.0",
        "uptime_seconds": 12,
        "admin": False,
        "disk_differs": [],
        "mcp_servers": [{"alias": "fs", "runtime_state": "running", "published_tools": 1}],
    }
    scripted = ScriptedClient(registry, lambda _: _ok(structured=report))

    async def scenario() -> None:
        await scripted.connect(tools=[_tool()])
        async with Client(mcp) as client:
            live = (await client.call_tool("relay_status", {})).structured_content
            assert live["client"]["report"] == "live"
            assert live["client"]["uptime_seconds"] == 12
            assert live["mcp_servers"] == report["mcp_servers"]
            assert live["server"]["published_tools"] == 1
            assert scripted.invokes[-1]["tool_name"] == "client.status"

            await registry.disconnect(scripted, reason="closed:1000")
            cached = (await client.call_tool("relay_status", {})).structured_content
            assert cached["client"]["connected"] is False
            assert cached["client"]["report"] == "cached"
            assert cached["client"]["report_age_seconds"] >= 0
            assert cached["client"]["last_disconnect"]["reason"] == "closed:1000"
            assert cached["server"]["published_tools"] == 0

    _run(scenario)


def test_status_does_not_wait_on_a_busy_client() -> None:
    registry, mcp = _facade(timeout=5)
    scripted = ScriptedClient(registry)  # never answers

    async def scenario() -> None:
        await scripted.connect(tools=[_tool()])
        async with Client(mcp) as client:
            call = asyncio.create_task(client.call_tool("fs_read", {"path": "x"}))
            while not scripted.invokes:
                await asyncio.sleep(0.01)
            loop = asyncio.get_running_loop()
            started = loop.time()
            status = (await client.call_tool("relay_status", {})).structured_content
            assert loop.time() - started < 1
            assert status["client"]["invocation_state"] == "busy"
            assert status["client"]["report"] == "unavailable"
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)

    _run(scenario)
