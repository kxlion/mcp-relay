from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn
from fastmcp import Client
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CancelledNotification, CancelledNotificationParams

from mcp_relay.mcp_facade import (
    create_mcp_facade,
)
from mcp_relay.output_models import ProviderToolResult
from mcp_relay.protocol import (
    RELAY_CONTRACT,
    Capabilities,
    ClientResult,
    InvokeMessage,
    Progress,
    Register,
)
from mcp_relay.provider_tools import ProviderToolDescriptor
from mcp_relay.providers.base import ProviderConnectionError
from mcp_relay.registry import (
    ClientBusyError,
    ClientOfflineError,
    LateResponseError,
    RelayRegistry,
    RemoteClientError,
    UnknownClientError,
    UnsupportedToolError,
)
from mcp_relay.server import RelaySettings, create_app


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


class FakeSocket:
    async def send_json(self, message: object) -> None:
        pass


class BlockingSocket:
    def __init__(self) -> None:
        self.messages: list[object] = []
        self.invoke_seen = asyncio.Event()
        self.cancel_seen = asyncio.Event()

    async def send_json(self, message: object) -> None:
        self.messages.append(message)
        if isinstance(message, dict) and message.get("type") == "invoke":
            self.invoke_seen.set()
        elif isinstance(message, dict) and message.get("type") == "cancel":
            self.cancel_seen.set()


def synthetic_descriptors():
    return [
        ProviderToolDescriptor(
            provider_name="sample", tool_name=name,
            description="Synthetic generic MCP tool",
            input_schema={
                "type": "object",
                "properties": ({"mode": {"type": "string", "enum": ["read"]}} if name == "inspect" else {}),
                "required": ["mode"] if name == "inspect" else [],
                "additionalProperties": False,
            },
        ) for name in ("echo", "inspect")
    ]


class StubRegistry:
    def __init__(self, result: ProviderToolResult | BaseException) -> None:
        self.result = result
        self.announced_descriptors: dict[str, object] = {}
        self.calls: list[tuple[object, ...]] = []
        self.public_tools = 0

    def set_public_tools_count(self, count: int) -> None:
        self.public_tools = count

    def set_progress_listener(self, listener: object) -> None:  # noqa: ARG002
        pass

    async def invoke(self, *args: object) -> object:
        self.calls.append(args)
        if isinstance(self.result, BaseException):
            raise self.result
        assert isinstance(self.result, ProviderToolResult)
        # Mirrors the real registry: the bounded wire mirror becomes the
        # native MCP result exactly once (Tranche 4).
        from mcp_relay.mcp_results import native_result

        return native_result(self.result)


def test_facade_surface_is_static_and_never_varies_with_the_client() -> None:
    """The facade lists exactly the fixed tools before and after announcements."""
    from mcp_relay.relay_tools import PUBLIC_TOOL_NAMES

    async def scenario() -> None:
        registry = RelayRegistry(client_token="client-token-synthetic-credential-0000000000000000")
        mcp = create_mcp_facade(
            registry=registry,
            timeout_seconds=1,
        )
        static_names = [tool.name for tool in await mcp.list_tools()]
        assert set(static_names) == set(PUBLIC_TOOL_NAMES)
        socket = FakeSocket()
        registered = await registry.register(
            socket,
            Register(version=1, type="register", client_id="one", relay_contract=RELAY_CONTRACT),
        )
        assert registered.client_id == "one"
        await registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                tools=["mcp.list", "mcp.command", "mcp.add"],
                client_version="0.2.0",
            ),
        )
        # The client announcement changes nothing: no third-party tool is
        # ever published individually, no fixed tool appears or disappears.
        assert [tool.name for tool in await mcp.list_tools()] == static_names

    run(scenario())


def test_facade_never_publishes_third_party_tools() -> None:
    """Third-party descriptors do not become public tools under any name."""
    async def scenario() -> None:
        registry = RelayRegistry(client_token="client-token-synthetic-credential-0000000000000000")
        mcp = create_mcp_facade(
            registry=registry,
            timeout_seconds=1,
        )
        socket = FakeSocket()
        await registry.register(
            socket,
            Register(version=1, type="register", client_id="cua-one", relay_contract=RELAY_CONTRACT),
        )
        await registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                tools=["mcp.list", "mcp.command"],
                client_version="0.2.0",
            ),
        )
        names = [tool.name for tool in await mcp.list_tools()]
        assert "relay_cua_browser_type" not in names
        assert "relay_sample_echo" not in names
        # Exactly the fixed surface: nothing derived from providers.
        assert len(names) == len(set(names))
        assert all(name.startswith("relay_") for name in names)

    run(scenario())


def test_tool_discovery_is_exact_and_closed() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(client_id="one", client_token="client-token-synthetic-credential-0000000000000000")
        mcp = create_mcp_facade(registry=registry, client_id="one", timeout_seconds=1)
        async with Client(mcp) as session:
            tools = await session.list_tools()

        from mcp_relay.relay_tools import PUBLIC_TOOL_NAMES

        assert {tool.name for tool in tools} == set(PUBLIC_TOOL_NAMES)
        by_name = {tool.name: tool for tool in tools}
        for name in ("relay_server_status",):
            assert by_name[name].input_schema == {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }
            assert by_name[name].output_schema is not None
            assert by_name[name].output_schema["additionalProperties"] is False
        search_schema = by_name["relay_registry_search"].input_schema
        assert search_schema["additionalProperties"] is False
        assert search_schema["required"] == ["query"]
        assert set(search_schema["properties"]) == {
            "query",
            "limit",
            "cursor",
            "version",
            "updated_since",
            "include_deleted",
        }
    run(scenario())


@pytest.mark.integration
def test_public_mcp_call_cancellation_sends_one_cancel_and_releases_request() -> None:
    async def scenario() -> None:
        client_socket = BlockingSocket()
        relay_request_id: str | None = None
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        app = create_app(
            RelaySettings(
                client_id="one",
                client_token="client-token-synthetic-credential-0000000000000000",
                mcp_token="control-token-synthetic-credential-0000000000000000",
                max_timeout_seconds=10,
            )
        )
        registry = app.state.registry
        await registry.register(
            client_socket,
            Register(version=1, type="register", client_id="one", relay_contract=RELAY_CONTRACT),
        )
        await registry.set_capabilities(
            client_socket,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                tools=["mcp.list", "mcp.command"],
                client_version="0.2.0",
            ),
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="critical",
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            async with httpx2.AsyncClient(
                headers={"Authorization": "Bearer control-token-synthetic-credential-0000000000000000"},
            ) as http_client:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp",
                    http_client=http_client,
                    terminate_on_close=True,
                ) as (read_stream, write_stream):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=10,
                    ) as session:
                        await session.initialize()
                        call = asyncio.create_task(
                            session.call_tool(
                                "relay_mcp_list", {}
                            )
                        )
                        await asyncio.wait_for(client_socket.invoke_seen.wait(), timeout=1)
                        # The SDK does not expose the request id; call_tool is
                        # the next request after initialize in this isolated session.
                        request_id = session._dispatcher._next_id  # pyright: ignore[reportPrivateUsage]
                        await session.send_notification(
                            CancelledNotification(
                                params=CancelledNotificationParams(
                                    requestId=request_id,
                                    reason="test cancellation",
                                )
                            )  # type: ignore[arg-type]
                        )
                        await asyncio.wait_for(client_socket.cancel_seen.wait(), timeout=2)
                        call.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await call
                        relay_request_id = next(
                            message["request_id"]
                            for message in client_socket.messages
                            if isinstance(message, dict)
                            and message.get("type") == "invoke"
                        )
            await asyncio.wait_for(client_socket.cancel_seen.wait(), timeout=2)
        except* anyio.BrokenResourceError:
            # MCP 2 cancels the HTTP response task after the request is
            # cancelled; the late response can only reach a closed client
            # stream and is irrelevant to Relay cancellation semantics.
            pass
        finally:
            server.should_exit = True
            server.force_exit = True
            await asyncio.wait_for(server_task, timeout=2)

        assert registry.pending_count == 0
        assert relay_request_id is not None
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_result(
                ClientResult(
                    version=2,
                    type="result",
                    request_id=relay_request_id,
                    result=ProviderToolResult(
                        content=[], structuredContent={"late": True}
                    ),
                )
            )
        assert [
            message["type"]
            for message in client_socket.messages
            if isinstance(message, dict)
        ] == ["invoke", "cancel"]

    run(scenario())



def test_status_reports_offline_and_online_safe_state() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(client_id="one", client_token="client-token-synthetic-credential-0000000000000000")
        mcp = create_mcp_facade(registry=registry, client_id="one", timeout_seconds=1)
        async with Client(mcp) as session:
            offline = await session.call_tool("relay_server_status", {})
            socket = FakeSocket()
            await registry.register(
                socket,
                Register(
                    version=1,
                    type="register",
                    relay_contract=RELAY_CONTRACT,
                    client_id="one",
                ),
            )
            await registry.set_capabilities(
                socket,
                Capabilities(
                    version=1,
                    type="capabilities",
                    relay_contract=RELAY_CONTRACT,
                    tools=["mcp.list", "mcp.command", "mcp.add", "mcp.modify", "mcp.delete", "mcp.enable", "mcp.disable", "client.status"],
                client_version="0.2.0",
            ),
            )
            online = await session.call_tool("relay_server_status", {})

        assert offline.is_error is False
        assert offline.structured_content == {
            "client_id": "one",
            "connected": False,
            "capabilities": [],
            "invocation_state": "idle",
            "progress": None,
            "heartbeat_age_seconds": None,
            "client_version": "unknown",
            "connected_since": None,
            "last_disconnect": None,
            "counters": {"public_tools": 10, "client_operations": 0},
            "suggested_action": "start_client",
        }
        assert online.is_error is False
        assert online.structured_content is not None
        assert online.structured_content["client_id"] == "one"
        assert online.structured_content["connected"] is True
        assert online.structured_content["capabilities"] == [
            "client.status",
            "mcp.add",
            "mcp.command",
            "mcp.delete",
            "mcp.disable",
            "mcp.enable",
            "mcp.list",
            "mcp.modify",
        ]
        # The public status reports the announced bounded version.
        assert online.structured_content["client_version"] == "0.2.0"
        assert online.structured_content["suggested_action"] is None
        assert online.structured_content["connected_since"] is not None
        assert online.structured_content["counters"] == {
            "public_tools": 10,
            "client_operations": 8,
        }
        assert set(online.structured_content) == {
            "client_id",
            "connected",
            "capabilities",
            "invocation_state",
            "progress",
            "heartbeat_age_seconds",
            "client_version",
            "connected_since",
            "last_disconnect",
            "counters",
            "suggested_action",
        }

    run(scenario())


def test_status_reports_announced_client_version() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(client_id="one", client_token="client-token-synthetic-credential-0000000000000000")
        mcp = create_mcp_facade(registry=registry, client_id="one", timeout_seconds=1)
        async with Client(mcp) as session:
            socket = FakeSocket()
            await registry.register(
                socket,
                Register(version=1, type="register", client_id="one", relay_contract=RELAY_CONTRACT),
            )
            await registry.set_capabilities(
                socket,
                Capabilities(
                    version=1,
                    type="capabilities",
                    relay_contract=RELAY_CONTRACT,
                    tools=["sample.echo"],
                    client_version="0.2.0",
                ),
            )
            online = await session.call_tool("relay_server_status", {})
            # After the client disconnects no stale version metadata remains.
            await registry.disconnect(socket)
            offline = await session.call_tool("relay_server_status", {})

        assert online.is_error is False
        assert online.structured_content is not None
        assert online.structured_content["client_version"] == "0.2.0"
        assert offline.structured_content is not None
        assert offline.structured_content["connected"] is False
        assert offline.structured_content["client_version"] == "unknown"

    run(scenario())


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("relay_mcp_list", {}),
        (
            "relay_mcp_command",
            {
                "alias": "sample",
                "tool": "echo",
                "arguments": {},
                "catalog_revision": "0" * 32 + ":0",
            },
        ),
    ],
)
def test_dispatched_tools_preserve_client_result_payload(
    tool_name: str, arguments: dict[str, object]
) -> None:
    """Client-routed tools return the Client's payload untouched."""
    async def scenario() -> None:
        payload = {"pong": True, "catalog_revision": "r", "items": [1, 2]}
        registry = StubRegistry(
            ProviderToolResult(content=[], structuredContent=payload)
        )
        mcp = create_mcp_facade(  # type: ignore[arg-type]
            registry=registry, client_id="one", timeout_seconds=2.5
        )
        async with Client(mcp) as session:
            response = await session.call_tool(tool_name, arguments)

        assert response.is_error is False
        # Tranche 4: every client-routed tool answers the native MCP result;
        # the Client's structuredContent rides through verbatim.
        assert response.structured_content == payload
        assert len(registry.calls) == 1
        client_id, message, timeout = registry.calls[0]
        assert client_id == "one"
        assert type(message) is InvokeMessage
        assert message.version == 2
        assert message.request_id
        assert message.arguments == arguments
        assert timeout == 2.5

    run(scenario())


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (UnknownClientError("sensitive"), "client_unavailable"),
        (ClientOfflineError("sensitive"), "client_unavailable"),
        (ClientBusyError("sensitive"), "client_busy"),
        (UnsupportedToolError("sensitive"), "tool_unknown"),
        (TimeoutError("sensitive"), "timeout"),
        (RemoteClientError("secret-code", "sensitive"), "execution_failed"),
    ],
)
def test_expected_relay_failures_are_safe_mcp_tool_errors(
    error: BaseException, expected_code: str
) -> None:
    """Dispatch failures become closed JSON errors; internals never leak."""
    async def scenario() -> None:
        registry = StubRegistry(error)
        mcp = create_mcp_facade(  # type: ignore[arg-type]
            registry=registry, client_id="one", timeout_seconds=1
        )
        async with Client(mcp) as session:
            response = await session.call_tool(
                "relay_client_status", {}, raise_on_error=False
            )

        assert response.is_error is True
        text = response.content[0].text  # type: ignore[union-attr]
        start = text.index("{") if "{" in text else 0
        payload = json.loads(text[start:]) if "{" in text else {"code": text}
        if expected_code == "execution_failed" and isinstance(
            error, RemoteClientError
        ):
            # An unknown Relay code is normalized to the closed
            # execution_failed code; the secret message never does pass.
            assert payload["code"] == "execution_failed"
            assert payload["message"] == "client invocation failed"
            assert "sensitive" not in text
        else:
            assert payload.get("code") == expected_code or expected_code in text
        assert "execution_state" in payload or "message" in payload or expected_code in text

    run(scenario())


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (UnknownClientError("sensitive"), "client_unavailable"),
        (ClientOfflineError("sensitive"), "client_unavailable"),
        (ClientBusyError("sensitive"), "client_busy"),
        (
            UnsupportedToolError("sensitive"),
            "tool_unknown",
        ),
        (TimeoutError("sensitive"), "timeout"),
        (
            RemoteClientError("secret-code", "sensitive"),
            "execution_failed",
        ),
        (
            ProviderConnectionError("sensitive"),
            "internal_error",
        ),
    ],
)
def test_relay_failures_carry_closed_error_codes(
    error: BaseException, expected_code: str
) -> None:
    """MCP clients see a closed JSON error; exception details never leak.

    Each expected dispatch failure maps to one fixed Relay code rendered as
    ``{code, message}`` JSON text (execution_state included where the wire
    contract carries it).
    """

    async def scenario() -> None:
        registry = StubRegistry(error)
        mcp = create_mcp_facade(  # type: ignore[arg-type]
            registry=registry, client_id="one", timeout_seconds=1
        )
        async with Client(mcp) as session:
            response = await session.call_tool(
                "relay_client_status", {}, raise_on_error=False
            )

        assert response.is_error is True
        text = response.content[0].text  # type: ignore[union-attr]
        assert "sensitive" not in text
        assert "UNEXPECTED_SECRET" not in text
        if expected_code == "execution_failed" and isinstance(
            error, RemoteClientError
        ):
            # Unknown Relay codes normalize to the closed execution_failed
            # code; the secret message never does.
            assert '"execution_failed"' in text
            assert "client invocation failed" in text
        elif expected_code == "internal_error":
            assert "internal relay error" in text
        else:
            assert f'"{expected_code}"' in text
        assert "sensitive" not in text

    run(scenario())


def test_known_relay_codes_pass_through_unknown_ones_normalize() -> None:
    """The facade's closed code contract over client-emitted errors."""

    async def scenario(error: RemoteClientError) -> dict[str, Any]:
        registry = StubRegistry(error)
        mcp = create_mcp_facade(  # type: ignore[arg-type]
            registry=registry, client_id="one", timeout_seconds=1
        )
        async with Client(mcp) as session:
            response = await session.call_tool(
                "relay_client_status", {}, raise_on_error=False
            )
        assert response.is_error is True
        text = response.content[0].text  # type: ignore[union-attr]
        payload: dict[str, Any] = json.loads(text[text.index("{"):])
        return payload

    # A known Relay code keeps its code and its execution state.
    known = run(
        scenario(
            RemoteClientError(
                "permission_denied",
                "administration is disabled on this client",
                execution_state="not_started",
            )
        )
    )
    assert known["code"] == "permission_denied"
    assert known["execution_state"] == "not_started"

    # A transport-generic "busy" normalizes to the existing client_busy code.
    busy = run(
        scenario(
            RemoteClientError("busy", "an action is already running")
        )
    )
    assert busy["code"] == "client_busy"

    # Any other unknown code normalizes to the closed execution_failed code.
    unknown = run(
        scenario(RemoteClientError("totally_new_code", "detail", "unknown"))
    )
    assert unknown["code"] == "execution_failed"
    assert unknown["message"] == "client invocation failed"
    assert unknown["execution_state"] == "unknown"



@pytest.mark.parametrize("tool_name", ["relay_server_status", "relay_client_status"])
def test_unexpected_tool_failures_never_reach_mcp_clients(tool_name: str) -> None:
    class ExplodingRegistry(StubRegistry):
        async def status_snapshot(self) -> object:
            raise RuntimeError("Bearer UNEXPECTED_SECRET")

    async def scenario() -> None:
        registry = ExplodingRegistry(RuntimeError("Bearer UNEXPECTED_SECRET"))
        mcp = create_mcp_facade(  # type: ignore[arg-type]
            registry=registry, client_id="one", timeout_seconds=1
        )
        async with Client(mcp) as session:
            response = await session.call_tool(tool_name, {}, raise_on_error=False)

        assert response.is_error is True
        assert "internal relay error" in response.content[0].text  # type: ignore[union-attr]
        assert "UNEXPECTED_SECRET" not in response.content[0].text  # type: ignore[union-attr]

    run(scenario())


def test_unexpected_argument_shapes_are_relayed_to_the_client() -> None:
    """The Client remains the sole validator of operation arguments.

    Bounded arguments that do not satisfy the operation's semantics (a bad
    alias, an unknown field) are forwarded verbatim over the wire; the
    Client's closed envelope answers with its structured error.
    """
    async def scenario() -> None:
        registry = StubRegistry(ProviderToolResult(content=[], structuredContent={}))
        mcp = create_mcp_facade(  # type: ignore[arg-type]
            registry=registry, client_id="one", timeout_seconds=1
        )
        async with Client(mcp) as session:
            enum_violation = await session.call_tool(
                "relay_mcp_command",
                {"alias": "sample", "tool": "inspect", "arguments": {"mode": "arbitrary"}, "catalog_revision": "r"},
            )
            extra = await session.call_tool(
                "relay_mcp_list", {"limit": 999}
            )

        assert enum_violation.is_error is False
        assert extra.is_error is False
        assert [call[1].tool_name for call in registry.calls] == [
            "mcp.command",
            "mcp.list",
        ]
        assert registry.calls[0][1].arguments == {
            "alias": "sample",
            "tool": "inspect",
            "arguments": {"mode": "arbitrary"},
            "catalog_revision": "r",
        }
        assert registry.calls[1][1].arguments == {"limit": 999}

    run(scenario())


def test_command_returns_a_native_call_tool_result() -> None:
    """The spec pins a native CallToolResult for relay_mcp_command.

    Multimodal content blocks, structuredContent, isError and top-level
    extensions survive; the SDK's global JSON text-wrapping is never applied.
    """
    async def scenario() -> None:
        registry = StubRegistry(
            ProviderToolResult(
                content=[
                    {"type": "text", "text": "hello"},
                    {"type": "image", "data": "aGk=", "mimeType": "image/png"},
                ],
                structuredContent={"echo": "ok"},
                isError=False,
            )
        )
        mcp = create_mcp_facade(  # type: ignore[arg-type]
            registry=registry, client_id="one", timeout_seconds=2
        )
        async with Client(mcp) as session:
            response = await session.call_tool(
                "relay_mcp_command",
                {
                    "alias": "sample",
                    "tool": "echo",
                    "arguments": {"x": 1},
                    "catalog_revision": "r",
                },
            )

        types = [block.type for block in response.content]
        assert types == ["text", "image"], response.content
        assert response.content[0].text == "hello"  # type: ignore[union-attr]
        assert response.structured_content == {"echo": "ok"}
        assert response.is_error is False

    run(scenario())


def test_command_relay_failure_is_a_closed_json_error_result() -> None:
    """Relay failures on the command path are isError CallToolResults with
    the closed {code, message, execution_state} object in one text block."""
    async def scenario() -> None:
        registry = StubRegistry(ClientOfflineError("client is offline"))
        mcp = create_mcp_facade(  # type: ignore[arg-type]
            registry=registry, client_id="one", timeout_seconds=1
        )
        async with Client(mcp) as session:
            response = await session.call_tool(
                "relay_mcp_command",
                {
                    "alias": "sample",
                    "tool": "echo",
                    "arguments": {},
                    "catalog_revision": "r",
                },
                raise_on_error=False,
            )

        assert response.is_error is True
        payload = json.loads(response.content[0].text)  # type: ignore[union-attr]
        assert payload["code"] == "client_unavailable"
        assert payload["message"] == "client offline"
        assert payload["execution_state"] in {"not_started", "unknown"}

    run(scenario())


def _registry_transport(
    page: bytes | Exception, *, status_code: int = 200
) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        if isinstance(page, Exception):
            raise page
        return httpx2.Response(status_code, content=page)

    return httpx2.MockTransport(handler)


def _search_page(count: int = 1, next_cursor: str | None = None) -> bytes:
    servers = [
        {
            "server": {
                "name": f"io.example/author/server-{index}",
                "title": f"Server {index}",
                "description": "Registry fixture server.",
                "version": "1.0.0",
                "packages": [
                    {
                        "registryType": "npm",
                        "identifier": f"pkg-{index}",
                        "version": "1.0.0",
                    }
                ],
            },
            "_meta": {
                "io.modelcontextprotocol.registry/official": {
                    "status": "active",
                    "isLatest": True,
                }
            },
        }
        for index in range(count)
    ]
    metadata: dict[str, object] = {"count": count}
    if next_cursor is not None:
        metadata["nextCursor"] = next_cursor
    return json.dumps({"servers": servers, "metadata": metadata}).encode()


def test_registry_search_returns_bounded_structured_results() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(client_id="one", client_token="client-token-synthetic-credential-0000000000000000")
        mcp = create_mcp_facade(
            registry=registry,
            client_id="one",
            timeout_seconds=1,
            registry_transport=_registry_transport(_search_page(count=2)),
        )
        async with Client(mcp) as session:
            response = await session.call_tool(
                "relay_registry_search", {"query": "author", "limit": 2}
            )

        assert response.is_error is False
        content = response.structured_content
        assert content is not None
        assert content["next_cursor"] is None
        assert [item["name"] for item in content["results"]] == [
            "io.example/author/server-0",
            "io.example/author/server-1",
        ]
        first = content["results"][0]
        assert first["title"] == "Server 0"
        assert first["description"] == "Registry fixture server."
        assert first["version"] == "1.0.0"
        assert first["repository_url"] is None
        assert first["packages"] == [
            {"registry_type": "npm", "identifier": "pkg-0", "version": "1.0.0"}
        ]

    run(scenario())


def test_registry_search_preserves_the_registry_pagination_cursor() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(client_id="one", client_token="client-token-synthetic-credential-0000000000000000")
        mcp = create_mcp_facade(
            registry=registry,
            client_id="one",
            timeout_seconds=1,
            registry_transport=_registry_transport(
                _search_page(next_cursor="io.example/x:1")
            ),
        )
        async with Client(mcp) as session:
            response = await session.call_tool(
                "relay_registry_search",
                {"query": "author", "cursor": "io.example/x:0"},
            )

        assert response.is_error is False
        content = response.structured_content
        assert content is not None
        assert content["next_cursor"] == "io.example/x:1"
        assert content["results"][0]["name"] == "io.example/author/server-0"

    run(scenario())


def test_registry_search_never_touches_the_client_channel() -> None:
    class RecordingRegistry(RelayRegistry):
        def __init__(self) -> None:
            super().__init__(client_id="one", client_token="client-token-synthetic-credential-0000000000000000")
            self.invocations: list[tuple[object, ...]] = []

        async def invoke(self, *args: object) -> object:
            self.invocations.append(args)
            raise AssertionError("registry search must not invoke the client")

    async def scenario() -> None:
        registry = RecordingRegistry()
        mcp = create_mcp_facade(
            registry=registry,  # type: ignore[arg-type]
            client_id="one",
            timeout_seconds=1,
            registry_transport=_registry_transport(_search_page()),
        )
        async with Client(mcp) as session:
            response = await session.call_tool(
                "relay_registry_search", {"query": "author"}
            )

        assert response.is_error is False
        assert registry.invocations == []

    run(scenario())


@pytest.mark.parametrize(
    "page",
    [
        httpx2.ConnectError("connection refused"),
        httpx2.ReadTimeout("timed out"),
        b"<html>not json</html>",
    ],
)
def test_registry_search_maps_outages_to_registry_unreachable(
    page: bytes | Exception,
) -> None:
    async def scenario() -> None:
        registry = RelayRegistry(client_id="one", client_token="client-token-synthetic-credential-0000000000000000")
        mcp = create_mcp_facade(
            registry=registry,
            client_id="one",
            timeout_seconds=1,
            registry_transport=_registry_transport(page),
        )
        async with Client(mcp) as session:
            response = await session.call_tool(
                "relay_registry_search", {"query": "author"}, raise_on_error=False
            )

        assert response.is_error is True
        text = response.content[0].text  # type: ignore[union-attr]
        payload = json.loads(text[text.index("{") :])
        assert payload["code"] == "registry_unreachable"
        assert payload["message"]
        assert payload["suggested_action"] == "retry_later"

    run(scenario())


def test_registry_search_rejects_out_of_bounds_arguments() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(client_id="one", client_token="client-token-synthetic-credential-0000000000000000")
        mcp = create_mcp_facade(
            registry=registry,
            client_id="one",
            timeout_seconds=1,
            registry_transport=_registry_transport(_search_page()),
        )
        async with Client(mcp) as session:
            overlong = await session.call_tool(
                "relay_registry_search", {"query": "q" * 201}, raise_on_error=False
            )
            unexpected = await session.call_tool(
                "relay_registry_search",
                {"query": "ok", "unexpected": True},
                raise_on_error=False,
            )
            bad_limit = await session.call_tool(
                "relay_registry_search",
                {"query": "ok", "limit": 51},
                raise_on_error=False,
            )
            bad_updated_since = await session.call_tool(
                "relay_registry_search",
                {"query": "ok", "updated_since": "yesterday"},
                raise_on_error=False,
            )

        for response in (overlong, unexpected, bad_limit, bad_updated_since):
            assert response.is_error is True

    run(scenario())


@pytest.mark.integration
def test_official_streamable_http_client_uses_authenticated_canonical_mcp_url() -> None:
    async def scenario() -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        app = create_app(
            RelaySettings(
                client_id="one",
                client_token="client-placeholder-synthetic-credential-0000000000000000",
                mcp_token="control-placeholder-synthetic-credential-0000000000000000",
            )
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="critical",
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            async with httpx2.AsyncClient(
                headers={"Authorization": "Bearer control-placeholder-synthetic-credential-0000000000000000"},
            ) as http_client:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp",
                    http_client=http_client,
                    terminate_on_close=False,
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        initialized = await session.initialize()
                        tools = (await session.list_tools()).tools
                        status = await session.call_tool("relay_server_status", {})
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=2)

        assert initialized.server_info.name == "MCP Relay"
        from mcp_relay.relay_tools import PUBLIC_TOOL_NAMES

        assert {tool.name for tool in tools} == set(PUBLIC_TOOL_NAMES)
        assert status.is_error is False
        assert status.structured_content is not None
        assert status.structured_content["connected"] is False

    run(scenario())


def test_ws_progress_frames_reach_the_calling_mcp_client() -> None:
    """Progress frames from the WS tunnel surface as MCP progress notifications.

    The facade binds the invoking tool's FastMCP Context to the relay
    request_id; the registry's progress listener forwards each in-flight
    ``progress`` frame to that context, which notifies the MCP client.
    """

    async def scenario() -> None:
        received: list[tuple[float, float | None, str | None]] = []

        async def progress_handler(
            progress: float, total: float | None, message: str | None
        ) -> None:
            received.append((progress, total, message))

        registry = RelayRegistry(client_id="one", client_token="client-token-synthetic-credential-0000000000000000")
        mcp = create_mcp_facade(registry=registry, client_id="one", timeout_seconds=5)
        socket = BlockingSocket()
        await registry.register(
            socket,
            Register(version=1, type="register", client_id="one", relay_contract=RELAY_CONTRACT),
        )
        await registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                tools=["mcp.list"],
                client_version="0.2.0",
            ),
        )
        async with Client(mcp, progress_handler=progress_handler) as session:
            call = asyncio.create_task(session.call_tool("relay_mcp_list", {}))
            await asyncio.wait_for(socket.invoke_seen.wait(), timeout=2)
            request_id = next(
                message["request_id"]
                for message in socket.messages
                if isinstance(message, dict) and message.get("type") == "invoke"
            )
            # The relay Client's progress frame rides the WS tunnel ingress.
            await registry.handle_progress(
                Progress(
                    version=2,
                    type="progress",
                    request_id=request_id,
                    progress=40,
                    message="halfway",
                )
            )
            await registry.handle_result(
                ClientResult(
                    version=2,
                    type="result",
                    request_id=request_id,
                    result=ProviderToolResult(
                        content=[], structuredContent={"pong": True}
                    ),
                )
            )
            response = await asyncio.wait_for(call, timeout=5)

        assert response.is_error is False
        assert received == [(40.0, None, "halfway")]

    run(scenario())
