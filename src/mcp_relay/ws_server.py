"""Relay WebSocket listener: the /ws surface and Client authentication only.

This module owns the Server-to-Client WebSocket handler extracted from the
legacy single-surface factory. It serves exactly one surface — ``/ws`` — and
authenticates exactly one channel: the Relay Client Bearer token. MCP
invocation lives on the separate MCP listener app built by
:func:`mcp_relay.server.create_mcp_app`; both listener apps receive the SAME
``RelayRegistry`` instance so a socket registered here is immediately visible
to the fixed MCP facade.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from .auth import credentials_match
from .diagnostics import debug as _debug_log
from .protocol import (
    MAX_TOKEN_LENGTH,
    RELAY_CONTRACT,
    Capabilities,
    ClientError,
    ClientResult,
    Heartbeat,
    Progress,
    Register,
    parse_client_message,
)
from .registry import (
    AuthenticationError,
    ClientAlreadyConnectedError,
    LateResponseError,
    RelayRegistry,
    UnknownRequestError,
)

_MAX_WS_CONNECTIONS = 32
_REGISTER_TIMEOUT_SECONDS = 10.0


def _debug_client_frame(category: str) -> None:
    _debug_log(f"server client frame diagnostic: category={category}")


class _SerializedWebSocket:
    """One async write gate for every outbound frame on a WS connection."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._write_lock = asyncio.Lock()

    async def send_json(self, message: object) -> None:
        async with self._write_lock:
            await self._websocket.send_json(message)

    async def close(self, *, code: int, reason: str) -> None:
        async with self._write_lock:
            await self._websocket.close(code=code, reason=reason)


def _websocket_bearer_matches(websocket: WebSocket, expected: str) -> bool:
    """Validate exactly one bounded ASCII Bearer header without exposing it."""
    values = [
        value
        for name, value in websocket.scope.get("headers", [])
        if name.lower() == b"authorization"
    ]
    if len(values) != 1 or len(values[0]) > len(b"Bearer ") + MAX_TOKEN_LENGTH:
        return False
    try:
        supplied = values[0].decode("ascii")
    except UnicodeDecodeError:
        return False
    return credentials_match(supplied, expected)


def create_ws_app(
    registry: RelayRegistry,
    *,
    client_token: str,
    max_ws_message_bytes: int,
) -> FastAPI:
    """Create the WebSocket listener app for an existing registry.

    The caller owns the registry: the same instance must back the MCP
    listener app so that the fixed facade dispatches to sockets accepted
    here. The app serves only ``/ws`` and publishes no docs or OpenAPI.
    """
    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.registry = registry
    app.state._ws_active = 0

    @app.websocket("/ws")
    async def client_socket(websocket: WebSocket) -> None:
        if not _websocket_bearer_matches(websocket, f"Bearer {client_token}"):
            # Closing before accept denies the HTTP upgrade (403 in Uvicorn).
            await websocket.close(code=1008, reason="authentication failed")
            return
        # Check and reserve without an await, so concurrent handlers on this
        # event loop cannot oversubscribe the per-app cap.
        if app.state._ws_active >= _MAX_WS_CONNECTIONS:
            await websocket.close(code=1013, reason="connection limit reached")
            return
        app.state._ws_active += 1
        connection = _SerializedWebSocket(websocket)
        registered = False
        disconnect_reason: str | None = None
        try:
            await websocket.accept()
            deadline = asyncio.get_running_loop().time() + _REGISTER_TIMEOUT_SECONDS
            while True:
                try:
                    async with asyncio.timeout_at(deadline if not registered else None):
                        frame = await websocket.receive()
                    if frame["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect(frame.get("code", 1000))
                    if frame.get("bytes") is not None:
                        await connection.close(
                            code=1002, reason="binary frames are not allowed"
                        )
                        return
                    text = frame.get("text")
                    if not isinstance(text, str):
                        await connection.close(code=1002, reason="invalid protocol message")
                        return
                    if len(text.encode("utf-8")) > max_ws_message_bytes:
                        _debug_log(
                            "provider frame failure: category=frame-oversized direction=inbound"
                        )
                        await connection.close(code=1009, reason="message too large")
                        return
                    raw = json.loads(text)
                    message = parse_client_message(raw)
                except (ValueError, RecursionError, ValidationError):
                    _debug_client_frame("protocol-parse")
                    await connection.close(
                        code=1002, reason="invalid protocol message"
                    )
                    return
                # Contract check before anything else: an incompatible
                # register/capabilities frame is a permanent error — close
                # with 1002 and a safe diagnostic; never register the client.
                if isinstance(message, (Register, Capabilities)):
                    if message.relay_contract != RELAY_CONTRACT:
                        _debug_client_frame("contract-mismatch")
                        await connection.close(
                            code=1002, reason="protocol_incompatible"
                        )
                        return
                if not registered:
                    if not isinstance(message, Register):
                        await connection.close(
                            code=1002, reason="register required first"
                        )
                        return
                    try:
                        async with asyncio.timeout_at(deadline):
                            reply = await registry.register(connection, message)
                            await registry.send(connection, reply.model_dump(mode="json"))
                            registered = True
                    except AuthenticationError:
                        _debug_client_frame("register-auth")
                        await connection.close(code=1008, reason="authentication failed")
                        return
                    except ClientAlreadyConnectedError:
                        _debug_client_frame("register-duplicate")
                        await connection.close(code=1013, reason="client already connected")
                        return
                    continue
                try:
                    if isinstance(message, Capabilities):
                        await registry.set_capabilities(connection, message)
                    elif isinstance(message, Heartbeat):
                        await registry.heartbeat(connection)
                    elif isinstance(message, ClientResult):
                        await registry.handle_result(message)
                    elif isinstance(message, ClientError):
                        await registry.handle_error(message)
                    elif isinstance(message, Progress):
                        await registry.handle_progress(message)
                    else:
                        await connection.close(
                            code=1002, reason="unexpected client message"
                        )
                        return
                except LateResponseError:
                    await connection.close(
                        code=1002, reason="late or duplicate response"
                    )
                    return
                except (AuthenticationError, UnknownRequestError):
                    await connection.close(
                        code=1002, reason="invalid request correlation"
                    )
                    return
        except TimeoutError:
            if registered:
                raise
            await connection.close(code=1008, reason="registration timeout")
        except WebSocketDisconnect as exc:
            disconnect_reason = f"closed:{getattr(exc, 'code', None) or 1000}"
        finally:
            try:
                await registry.disconnect(connection, reason=disconnect_reason)
            finally:
                app.state._ws_active -= 1

    return app
