"""E2E calibration: the heaviest known real result still passes.

cua-driver full-desktop payloads are the calibration target (~1.5 MiB
base64 screenshots + UIA inventory). 1.5 MiB must flow end-to-end through
the client answer path; just over 2 MiB is refused with the honest
result_too_large error visible to the caller.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest
from test_client import _Socket  # shared fake socket

from mcp_relay.client import ClientSettings, RelayClient
from mcp_relay.json_bounds import MAX_TOOL_RESULT_BYTES


class _Flood:
    async def call_tool(self, tool_name: str, arguments: dict) -> object:
        from mcp_relay.providers.base import bounded_result

        return bounded_result({"content": [{"type": "text", "text": "x" * arguments["size"]}]})


def _run_with_size(tmp_path: Path, size: int) -> dict[str, object]:
    from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog
    from mcp_relay.protocol import RELAY_CONTRACT
    from mcp_relay.provider_tools import ProviderToolDescriptor

    socket = _Socket(
        [
            json.dumps(
                {
                    "version": 1,
                    "type": "registered",
                    "client_id": "d",
                    "server_version": "0.1.0",
                    "relay_contract": RELAY_CONTRACT,
                }
            ),
            json.dumps(
                {
                    "version": 2,
                    "type": "invoke",
                    "request_id": "r",
                    "tool_name": "mcp.command",
                    "arguments": {"alias": "sample", "tool": "echo", "arguments": {"size": size}},
                }
            ),
        ]
    )

    async def scenario() -> None:
        catalog = ClientCatalog()
        catalog.update_alias(
            AliasCatalog(
                alias="sample",
                enabled=True,
                runtime_state="running",
                transport="stdio",
                catalog_available=True,
                error=None,
                descriptors=(
                    ProviderToolDescriptor(
                        provider_name="sample",
                        tool_name="echo",
                        description="d",
                        input_schema={"type": "object"},
                    ),
                ),
                provider=_Flood(),
            )
        )
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            catalog=catalog,
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(2000):
            if any(item.get("type") in {"result", "error"} for item in socket.sent):
                break
            await asyncio.sleep(0.002)
        client.stop()
        await task

    asyncio.run(scenario())
    for item in socket.sent:
        if item.get("type") in {"result", "error"}:
            return item
    raise AssertionError("no answer to invoke")


def test_real_calibrated_result_passes_end_to_end(tmp_path: Path) -> None:
    answer = _run_with_size(tmp_path, 1_500_000)
    assert answer["type"] == "result", answer


def test_oversized_result_is_refused_with_honest_code(tmp_path: Path) -> None:
    answer = _run_with_size(tmp_path, MAX_TOOL_RESULT_BYTES + 1024)
    assert answer["type"] == "error"
    assert answer["error"]["code"] == "result_too_large"  # type: ignore[index]


def test_inventory_descriptor_100k_is_still_refused(tmp_path: Path) -> None:
    # 64 KiB metadata bounds survive: a 100 KiB descriptor payload is
    # refused before any inventory announcement.
    from mcp_relay.json_bounds import JsonBoundsError, validate_json_bounds

    with pytest.raises(JsonBoundsError):
        validate_json_bounds({"blob": "x" * (100 * 1024)})
