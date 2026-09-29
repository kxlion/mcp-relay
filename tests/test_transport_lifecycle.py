"""FastMCP client transport lifecycle against real servers.

The historic owner-task internals are deleted; what must survive is the
observable lifecycle: a failed open leaves no usable transport, close is
terminal and idempotent, and real stdio children are reaped.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mcp_relay.mcp_hub import AliasLaunch, FastMcpClientTransport
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


def _stdio_launch(script: Path) -> AliasLaunch:
    return AliasLaunch(
        alias="probe",
        transport="stdio",
        argv=[sys.executable, str(script)],
        url=None,
        env={},
        cwd=None,
    )


def _pid_writing_script(tmp_path: Path, body: str) -> tuple[Path, Path]:
    script = tmp_path / "server_pid.py"
    pid_file = tmp_path / "child.pid"
    script.write_text(
        "import os\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        + body,
        encoding="utf-8",
    )
    return script, pid_file


def _assert_child_reaped(pid_file: Path) -> None:
    child_pid = int(pid_file.read_text())
    assert not process_exists(child_pid)


@pytest.mark.parametrize(
    "mode", ["ok", "initialize_never_answers", "garbage_inventory"]
)
def test_open_failure_and_close_boundaries_reap_the_child(
    tmp_path: Path, mode: str
) -> None:
    body = _MINI_SERVER
    if mode == "initialize_never_answers":
        body = body.replace(
            'if method == "initialize":',
            'if method == "initialize":\n        continue',
        )
    elif mode == "garbage_inventory":
        body = body.replace('result = {"tools": TOOLS}', 'result = {"tools": 42}')
    script, pid_file = _pid_writing_script(tmp_path, body)
    transport = FastMcpClientTransport(
        _stdio_launch(script), init_timeout_seconds=2.0
    )

    async def scenario() -> None:
        opened_ok = mode == "ok"
        try:
            tools = await asyncio.wait_for(transport.list_tools(), timeout=10)
            assert opened_ok
            assert [tool.name for tool in tools.tools] == ["echo"]
        except Exception:
            assert not opened_ok
        finally:
            await asyncio.wait_for(transport.close(), timeout=10)
        # Terminal and idempotent in every mode.
        with pytest.raises(RuntimeError):
            await transport.list_tools()
        await transport.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    _assert_child_reaped(pid_file)
