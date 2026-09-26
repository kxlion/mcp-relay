"""Frame refusals carry the frame-oversized diagnostic (client + server)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp_relay import diagnostics
from mcp_relay.client import ClientSettings, RelayClient
from mcp_relay.json_bounds import MAX_WS_MESSAGE_BYTES


class _Socket:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.sent: list[dict[str, object]] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        if self._replies:
            return self._replies.pop(0)
        await asyncio.sleep(3600)
        raise AssertionError("no reply queued")


def test_outbound_frame_over_limit_is_refused_with_category_log(
    tmp_path: Path,
) -> None:
    """An outbound frame above MAX_WS_MESSAGE_BYTES is refused locally and
    the refusal is diagnosable via the frame-oversized category."""

    log_path = tmp_path / "client.log"
    diagnostics.set_log_file(log_path)
    try:
        socket = _Socket([])

        async def scenario() -> None:
            client = RelayClient(
                ClientSettings(
                    server_url="ws://localhost/ws",
                    client_id="d",
                    client_token='client-synthetic-credential-0000000000000000',
                    workspace=tmp_path,
                ),
            )
            with __import__("pytest").raises(ValueError, match="outbound message"):
                await client._send(
                    socket,
                    {"padding": "x" * (MAX_WS_MESSAGE_BYTES + 1)},
                )

        asyncio.run(scenario())
    finally:
        diagnostics.set_log_file(None)
    file_text = log_path.read_text(encoding="utf-8")
    assert "category=frame-oversized direction=outbound" in file_text


def test_inbound_oversized_frame_is_refused_with_category_log(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "client.log"
    diagnostics.set_log_file(log_path)
    try:
        socket = _Socket([])

        async def scenario() -> None:
            client = RelayClient(
                ClientSettings(
                    server_url="ws://localhost/ws",
                    client_id="d",
                    client_token='client-synthetic-credential-0000000000000000',
                    workspace=tmp_path,
                ),
            )
            oversized = "x" * (MAX_WS_MESSAGE_BYTES + 1)
            socket._replies.append(oversized)
            with __import__("pytest").raises(ValueError, match="invalid server frame"):
                await client._receive(socket)

        asyncio.run(scenario())
    finally:
        diagnostics.set_log_file(None)
    file_text = log_path.read_text(encoding="utf-8")
    assert "category=frame-oversized direction=inbound" in file_text
