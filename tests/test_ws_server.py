"""Tests for the two Relay listeners.

Part 1: the /ws handshake and lifecycle — authentication, register-first,
contract checks, frame bounds, duplicate connections, disconnect cleanup and
reconnection.

Part 2: the MCP listener app and the WS listener app are two factories
receiving the same ``RelayRegistry``, each serving exactly one surface.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from starlette.websockets import WebSocketDisconnect

import mcp_relay.ws_server as ws_server
from mcp_relay.protocol import (
    RELAY_CONTRACT,
    Capabilities,
    Register,
)
from mcp_relay.registry import RelayRegistry
from mcp_relay.server import RelaySettings
from mcp_relay.version import package_version
from tests.composite_app import create_app


def settings() -> RelaySettings:
    return RelaySettings(
        client_id="client-a", client_token='client-secret-synthetic-credential-0000000000000000', mcp_token='control-secret-synthetic-credential-0000000000000000'
    )


def register_frame() -> dict[str, object]:
    return {
        "version": 1,
        "type": "register",
        "client_id": "client-a",
        "relay_contract": RELAY_CONTRACT,
    }


def capabilities_frame() -> dict[str, object]:
    return {
        "version": 1,
        "type": "capabilities",
        "admin": True,
        "relay_contract": RELAY_CONTRACT,
        "client_version": "0.2.0",
    }


# ---------------------------------------------------------------------------
# Part 1: characterization of the current /ws handler (green before refactor)
# ---------------------------------------------------------------------------


def test_characterized_handshake_accepts_valid_bearer_and_token_free_register() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_json(register_frame())
            registered = ws.receive_json()

    assert registered == {
        "version": 1,
        "type": "registered",
        "client_id": "client-a",
        "server_version": package_version(),
        "relay_contract": RELAY_CONTRACT,
    }


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(None, id="missing-bearer"),
        pytest.param({"Authorization": "Bearer wrong"}, id="wrong-bearer"),
        pytest.param({"Authorization": 'Basic client-secret-synthetic-credential-0000000000000000'}, id="wrong-scheme"),
        pytest.param(
            {"Authorization": 'Bearer control-secret-synthetic-credential-0000000000000000'}, id="other-channel-token"
        ),
    ],
)
def test_client_bearer_gate_denies_before_upgrade(
    headers: dict[str, str] | None,
) -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        kwargs: dict[str, dict[str, str]] = (
            {} if headers is None else {"headers": headers}
        )
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws", **kwargs):
                pytest.fail("unauthorized socket upgraded")

    assert exc_info.value.code == 1008
    assert 'client-secret-synthetic-credential-0000000000000000' not in exc_info.value.reason


def test_duplicate_authorization_headers_deny_before_upgrade() -> None:
    app = create_app(settings())
    sent: list[dict[str, object]] = []

    async def probe() -> None:
        async def receive() -> dict[str, str]:
            return {"type": "websocket.connect"}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await app(
            {
                "type": "websocket",
                "asgi": {"version": "3.0"},
                "scheme": "ws",
                "path": "/ws",
                "raw_path": b"/ws",
                "query_string": b"",
                "root_path": "",
                "headers": [
                    (b"authorization", b"Bearer client-secret"),
                    (b"Authorization", b"Bearer client-secret"),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("127.0.0.1", 80),
                "subprotocols": [],
            },
            receive,
            send,
        )

    asyncio.run(probe())
    assert sent == [{"type": "websocket.close", "code": 1008, "reason": "authentication failed"}]


def test_characterized_register_is_required_as_the_first_frame() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_json({"version": 2, "type": "heartbeat"})
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()

    assert exc_info.value.code == 1002


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"type": []}, id="array-type"),
        pytest.param({"type": {}}, id="object-type"),
        pytest.param({"type": None}, id="null-type"),
        pytest.param([], id="array-root"),
    ],
)
def test_authenticated_malformed_json_shape_closes_without_registering(
    payload: object,
) -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_json(payload)
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1002
        assert app.state.registry.registrations_accepted == 0
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as replacement:
            replacement.send_json(register_frame())
            assert replacement.receive_json()["type"] == "registered"


def test_characterized_contract_mismatch_is_a_permanent_1002_close() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_json({"version": 1, "type": "register", "client_id": "client-a", "relay_contract": 99})
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1002

        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_json(register_frame())
            assert ws.receive_json()["type"] == "registered"
            ws.send_json({"version": 1, "type": "capabilities", "admin": True, "relay_contract": 99, "client_version": "0.1.0"})
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1002

    # A contract-mismatched capabilities frame never updates the registry.
    assert app.state.registry.client_admin is False


def test_characterized_frame_bounds_and_protocol_errors_close_the_socket() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        # Binary frame: 1002.
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_bytes(b"{}")
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1002

        # Oversized text frame: 1009.
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_text("x" * (app.state.settings.max_ws_message_bytes + 1))
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1009

        # Malformed JSON: 1002.
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_text("{not json")
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1002

        # Deep JSON beyond the parser limit: 1002.
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_text("{" * 1100 + "}" * 1100)
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1002


def test_characterized_duplicate_connection_closes_1013_distinctly() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as first:
            first.send_json(register_frame())
            assert first.receive_json()["type"] == "registered"
            with client.websocket_connect(
                "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
            ) as duplicate:
                duplicate.send_json(register_frame())
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    duplicate.receive_json()

    assert exc_info.value.code == 1013
    assert exc_info.value.reason == "client already connected"


def test_characterized_result_frames_require_a_known_request_correlation() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_json(register_frame())
            assert ws.receive_json()["type"] == "registered"
            ws.send_json(
                {"version": 2, "type": "result", "request_id": "unknown", "result": {}}
            )
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()

    assert exc_info.value.code == 1002
    assert app.state.registry.pending_count == 0


def test_characterized_capabilities_and_heartbeat_reach_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings())
    heartbeat_handled = threading.Event()
    capabilities_handled = threading.Event()
    original_heartbeat = app.state.registry.heartbeat
    original_set_capabilities = app.state.registry.set_capabilities

    async def observed_heartbeat(*args: object) -> None:
        await original_heartbeat(*args)
        heartbeat_handled.set()

    async def observed_set_capabilities(*args: object) -> None:
        await original_set_capabilities(*args)
        capabilities_handled.set()

    monkeypatch.setattr(app.state.registry, "heartbeat", observed_heartbeat)
    monkeypatch.setattr(
        app.state.registry, "set_capabilities", observed_set_capabilities
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_json(register_frame())
            assert ws.receive_json()["type"] == "registered"
            before = app.state.registry.last_heartbeat
            ws.send_json({"version": 2, "type": "heartbeat"})
            assert heartbeat_handled.wait(timeout=2)
            assert app.state.registry.last_heartbeat > before
            ws.send_json(capabilities_frame())
            assert capabilities_handled.wait(timeout=2)
            assert app.state.registry.client_admin is True


def test_characterized_disconnect_cleanup_then_reconnection() -> None:
    """A closed socket frees the single client slot for a fresh connection."""
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as first:
            first.send_json(register_frame())
            assert first.receive_json()["type"] == "registered"
            first.send_json(capabilities_frame())
            # The capabilities frame has been processed once the registry
            # announces it; the socket then closes cleanly.
            deadline = time.monotonic() + 2
            while not app.state.registry.client_admin:
                assert time.monotonic() < deadline, "capabilities not processed"
                time.sleep(0.01)

        assert app.state.registry.client_admin is False

        # Reconnection: the registry accepted the disconnect and releases the
        # slot; a new socket can register the same client identity again.
        deadline = time.monotonic() + 2
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with client.websocket_connect(
                    "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
                ) as second:
                    second.send_json(register_frame())
                    assert second.receive_json()["type"] == "registered"
                break
            except WebSocketDisconnect as exc:  # pragma: no cover - timing path
                last_error = exc
                if exc.code != 1013:
                    raise
                time.sleep(0.05)
        else:  # pragma: no cover - timing path
            pytest.fail(f"reconnection never succeeded: {last_error}")


# ---------------------------------------------------------------------------
# Part 2: two listener factories, one registry
# ---------------------------------------------------------------------------


def test_composite_app_mounts_both_listener_apps_on_one_registry() -> None:
    """The composite exposes the two listener apps and hands both its registry."""
    app = create_app(settings())
    mcp_app = app.state.mcp_app
    ws_app = app.state.ws_app

    assert mcp_app.state.registry is app.state.registry
    assert ws_app.state.registry is app.state.registry
    assert mcp_app.state.mcp is app.state.mcp


def test_mcp_listener_serves_mcp_only() -> None:
    """The MCP app answers /mcp and never serves the /ws socket."""
    from mcp_relay.server import create_mcp_app

    registry = RelayRegistry(
        client_id="client-a", client_token='client-secret-synthetic-credential-0000000000000000'
    )
    mcp_app = create_mcp_app(registry, settings=settings())
    assert mcp_app.state.registry is registry

    # No WebSocket route exists on the MCP listener.
    assert all(getattr(route, "path", None) != "/ws" for route in mcp_app.routes)

    with TestClient(mcp_app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws"):
                pass  # pragma: no cover - never reaches a register flow
        response = client.post(
            "/mcp",
            headers={
                "Authorization": 'Bearer control-secret-synthetic-credential-0000000000000000',
                "Host": "127.0.0.1:8000",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert exc_info.value.code == 1000  # unmatched route, never a register flow
    assert response.status_code != 401


def test_ws_listener_serves_ws_only() -> None:
    """The WS app answers /ws and never serves the /mcp facade."""
    from mcp_relay.ws_server import create_ws_app

    registry = RelayRegistry(client_id="client-a", client_token='client-secret-synthetic-credential-0000000000000000')
    ws_app = create_ws_app(
        registry,
        client_token='client-secret-synthetic-credential-0000000000000000',
        max_ws_message_bytes=settings().max_ws_message_bytes,
    )
    assert ws_app.state.registry is registry

    # No MCP invocation surface exists on the WS listener.
    assert all(
        not str(getattr(route, "path", "")).startswith("/mcp")
        for route in ws_app.routes
    )

    with TestClient(ws_app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": 'Bearer control-secret-synthetic-credential-0000000000000000',
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_json(register_frame())
            registered = ws.receive_json()

    assert response.status_code == 404
    assert "jsonrpc" not in response.text
    assert registered["type"] == "registered"


@pytest.mark.parametrize("app_name", ["mcp_app", "ws_app"])
def test_listener_apps_publish_no_docs_or_openapi(app_name: str) -> None:
    app = create_app(settings())
    listener = getattr(app.state, app_name)

    assert listener.docs_url is None
    assert listener.redoc_url is None
    assert listener.openapi_url is None


def test_ws_listener_rejects_the_mcp_token() -> None:
    """The WS listener authenticates Client tokens only."""
    from mcp_relay.ws_server import create_ws_app

    registry = RelayRegistry(client_id="client-a", client_token='client-secret-synthetic-credential-0000000000000000')
    ws_app = create_ws_app(
        registry,
        client_token='client-secret-synthetic-credential-0000000000000000',
        max_ws_message_bytes=settings().max_ws_message_bytes,
    )
    with TestClient(ws_app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws", headers={"Authorization": 'Bearer control-secret-synthetic-credential-0000000000000000'}
            ):
                pytest.fail("MCP token upgraded on Client listener")

    assert exc_info.value.code == 1008


def test_direct_shared_registry_registration_flows_into_fixed_facade() -> None:
    """The MCP facade reflects a Client registered directly on the shared
    registry (the same object the WS listener authenticates sockets against);
    the full WS→registry→facade path is exercised in
    tests/test_server_listeners.py."""

    async def scenario() -> None:
        registry = RelayRegistry(client_id="client-a", client_token='client-secret-synthetic-credential-0000000000000000')
        from mcp_relay.server import create_mcp_app

        mcp_app = create_mcp_app(registry, settings=settings())
        mcp = mcp_app.state.mcp

        async with Client(mcp) as session:
            before_tools = [tool.name for tool in await session.list_tools()]
            before = await session.call_tool("relay_status", {})
            assert before.structured_content["client"]["connected"] is False

        # A Client registers through the shared registry (the same object the
        # WS listener authenticates sockets against).
        socket = _RecordingSocket()
        await registry.register(
            socket,
            Register(
                version=1, type="register", client_id="client-a", relay_contract=RELAY_CONTRACT
            ),
        )
        await registry.set_capabilities(
            socket, Capabilities(**capabilities_frame())  # type: ignore[arg-type]
        )

        async with Client(mcp) as session:
            during = await session.call_tool("relay_status", {})
            assert during.structured_content["client"]["connected"] is True
            assert during.structured_content["client"]["version"] == "0.2.0"
            assert during.structured_content["client"]["admin"] is True
            assert "relay_mcp_add" in [tool.name for tool in await session.list_tools()]

        await registry.disconnect(socket, reason="closed:1000")

        async with Client(mcp) as session:
            after = await session.call_tool("relay_status", {})
            after_tools = [
                tool.name for tool in await session.list_tools()
            ]
            assert after.structured_content["client"]["connected"] is False
            last_disconnect = after.structured_content["client"]["last_disconnect"]
            assert last_disconnect["reason"] == "closed:1000"

        # Reconnection works; without capabilities no admin tool is listed.
        socket_again = _RecordingSocket()
        await registry.register(
            socket_again,
            Register(
                version=1, type="register", client_id="client-a", relay_contract=RELAY_CONTRACT
            ),
        )
        async with Client(mcp) as session:
            reconnected = await session.call_tool("relay_status", {})
            assert reconnected.structured_content["client"]["connected"] is True
            assert [tool.name for tool in await session.list_tools()] == (
                before_tools
            )

        assert before_tools == after_tools

    asyncio.run(scenario())


class _RecordingSocket:
    def __init__(self) -> None:
        self.frames: list[object] = []

    async def send_json(self, message: object) -> None:
        self.frames.append(message)


# Slice 2: bounded WS admission and registration deadline.

def _app():
    registry = RelayRegistry(client_id="client-a", client_token="synthetic-secret")
    return ws_server.create_ws_app(
        registry, client_token="synthetic-secret", max_ws_message_bytes=4096
    )


def _connect(client: TestClient):
    return client.websocket_connect(
        "/ws", headers={"Authorization": "Bearer synthetic-secret"}
    )


def _register(ws):
    ws.send_json({"version": 1, "type": "register", "client_id": "client-a", "relay_contract": RELAY_CONTRACT})
    return ws.receive_json()


def test_32_idle_authenticated_sockets_fill_cap_and_release_on_close() -> None:
    app = _app()
    with TestClient(app) as client, ExitStack() as sockets:
        opened = [sockets.enter_context(_connect(client)) for _ in range(32)]
        with pytest.raises(WebSocketDisconnect):
            with _connect(client):
                pytest.fail("33rd socket upgraded")
        # An unauthenticated attempt cannot reserve a slot either.
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws"):
                pytest.fail("unauthenticated socket upgraded")
        opened[-1].close()
        # Releasing the closed context waits for server-side cleanup.
        sockets.pop_all().close()
        with _connect(client) as replacement:
            assert _register(replacement)["type"] == "registered"


def test_registration_deadline_is_absolute_and_does_not_evict_registered_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws_server, "_REGISTER_TIMEOUT_SECONDS", 0.12)
    app = _app()
    with TestClient(app) as client:
        with _connect(client) as idle:
            time.sleep(0.16)
            with pytest.raises(WebSocketDisconnect) as expired:
                idle.receive_json()
            assert expired.value.code == 1008
        with _connect(client) as registered:
            assert _register(registered)["type"] == "registered"
            time.sleep(0.16)
            registered.send_json({"version": 2, "type": "heartbeat"})
            # A still-registered socket prevents a second registration.
            with _connect(client) as duplicate:
                duplicate.send_json({"version": 1, "type": "register", "client_id": "client-a", "relay_contract": RELAY_CONTRACT})
                with pytest.raises(WebSocketDisconnect) as rejected:
                    duplicate.receive_json()
                assert rejected.value.code == 1013


def test_registration_must_finish_before_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws_server, "_REGISTER_TIMEOUT_SECONDS", 0.12)
    app = _app()
    started = threading.Event()
    original_register = app.state.registry.register

    async def slow_register(*args):
        started.set()
        await asyncio.sleep(0.3)
        return await original_register(*args)

    monkeypatch.setattr(app.state.registry, "register", slow_register)
    with TestClient(app) as client, _connect(client) as ws:
        ws.send_json({"version": 1, "type": "register", "client_id": "client-a", "relay_contract": RELAY_CONTRACT})
        assert started.wait(timeout=1)
        with pytest.raises(WebSocketDisconnect) as expired:
            ws.receive_json()
        assert expired.value.code == 1008
    assert app.state.registry.registrations_accepted == 0


def test_stalled_registration_acknowledgement_is_bounded_and_releases_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws_server, "_REGISTER_TIMEOUT_SECONDS", 0.12)
    app = _app()
    original_send = app.state.registry.send
    started = threading.Event()

    async def blocked_send(*args):
        started.set()
        await asyncio.Event().wait()
        return await original_send(*args)

    monkeypatch.setattr(app.state.registry, "send", blocked_send)
    with TestClient(app) as client:
        with _connect(client) as ws:
            ws.send_json({"version": 1, "type": "register", "client_id": "client-a", "relay_contract": RELAY_CONTRACT})
            assert started.wait(timeout=1)
            with pytest.raises(WebSocketDisconnect) as expired:
                ws.receive_json()
            assert expired.value.code == 1008
        assert app.state._ws_active == 0
        monkeypatch.setattr(app.state.registry, "send", original_send)
        with _connect(client) as replacement:
            assert _register(replacement)["type"] == "registered"


def test_post_registration_timeout_is_not_reported_as_registration_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app()

    async def timed_out_heartbeat(*args):
        raise TimeoutError("post-registration work timed out")

    monkeypatch.setattr(app.state.registry, "heartbeat", timed_out_heartbeat)
    with TestClient(app) as client:
        with pytest.raises(TimeoutError, match="post-registration work timed out"):
            with _connect(client) as ws:
                assert _register(ws)["type"] == "registered"
                ws.send_json({"version": 2, "type": "heartbeat"})
                ws.receive_json()
        assert app.state._ws_active == 0


def test_cancelled_or_failed_accept_releases_admission_slot() -> None:
    async def scenario(fail_accept: bool) -> None:
        app = _app()
        route = next(route for route in app.routes if getattr(route, "path", None) == "/ws")
        accepted = asyncio.Event()
        pending = asyncio.Event()
        sent: list[dict] = []

        async def receive():
            if not accepted.is_set():
                accepted.set()
                return {"type": "websocket.connect"}
            pending.set()
            await asyncio.Event().wait()

        async def send(message):
            sent.append(message)
            if fail_accept and message["type"] == "websocket.accept":
                raise RuntimeError("accept failed")

        scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": "/ws", "raw_path": b"/ws", "query_string": b"", "root_path": "", "headers": [(b"authorization", b"Bearer synthetic-secret")], "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 80), "subprotocols": []}
        task = asyncio.create_task(route.app(scope, receive, send))
        if fail_accept:
            with pytest.raises(RuntimeError, match="accept failed"):
                await task
        else:
            await asyncio.wait_for(pending.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert app.state._ws_active == 0

    asyncio.run(scenario(False))
    asyncio.run(scenario(True))
