"""Subprocess runtime-logging bench: real Server + Client, unified logs.

Integration proof for the unified logging contract (``docs/cli.md``
Logging section). It boots the real ``mcp-relay server`` (two Uvicorn
listeners) and ``mcp-relay client`` as subprocesses in an isolated
temporary ``HOME``, probes the MCP surface (2xx via a real MCP session
initialize, 401 without a token, 404 on an unknown path), then drives one
admin cycle (``relay_mcp_add`` of a synthetic stdio MCP server) and one
call of the published ``mini_echo`` tool through the facade. Finally it asserts the
contents of ``server.log`` and ``client.log``: access lines with the
status-derived levels, Uvicorn startup/shutdown lines for both listeners,
the session-manager line, admin and execution events — with no duplicated
records and no credential material anywhere.

A 500 response is deliberately **not** triggered: the closed relay surface
offers no backdoor-free way to force one, and simulating one would prove
nothing about the real runtime. The 5xx→ERROR mapping is covered by the
unit tests for ``_access_status_level``.

Spawned processes are terminated and verified gone, ports freed.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import signal
import socket
import subprocess
import sys
import time
from typing import Any

import anyio
import httpx2
import pytest
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.integration

MCP_TOKEN = "bench-mcp-token-logs-synthetic-credential-0000000000000000"
CLIENT_TOKEN = "bench-client-token-logs-synthetic-credential-0000000000000000"
ALIAS = "mini"
REGISTRATION_LINE = "authenticated registration succeeded"

_OPERATOR_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z "
    r"\[(DEBUG|INFO|WARNING|ERROR)\] "
)

# Real MCP stdio server (JSON-RPC over stdin/stdout) exposing one echo tool.
_MINI_MCP_SERVER = """\
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the text back",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
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
        text = str(request["params"]["arguments"].get("text", ""))
        result = {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        }
    else:
        continue
    if request.get("id") is None:
        continue
    response = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
    sys.stdout.write(json.dumps(response) + "\\n")
    sys.stdout.flush()
"""


def _free_port() -> int:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.close()
    return port


def _seed_home(home: pathlib.Path) -> pathlib.Path:
    relay_dir = home / ".mcp-relay"
    relay_dir.mkdir(parents=True)
    dotenv = relay_dir / ".env"
    dotenv.write_text(
        f"RELAY_MCP_TOKEN={MCP_TOKEN}\nRELAY_CLIENT_TOKEN={CLIENT_TOKEN}\n"
    )
    dotenv.chmod(0o600)
    return relay_dir / "config.yaml"


def _run_cli(home: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    # Windows resolves Path.home() from USERPROFILE, not HOME; set both so
    # the isolated home applies on every platform (same as test_local_e2e).
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    return subprocess.run(
        [sys.executable, "-m", "mcp_relay.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


def _wait_for_port(port: int, deadline: float = 30.0) -> None:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise AssertionError(f"no listener accepted on 127.0.0.1:{port} within {deadline}s")


def _wait_for_log(path: pathlib.Path, needle: str, deadline: float = 60.0) -> None:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if path.exists() and needle in path.read_text(errors="replace"):
            return
        time.sleep(0.2)
    raise AssertionError(f"{needle!r} not found in {path} within {deadline}s")


def _proc_cmdlines() -> list[str]:
    # /proc is Linux-only; no cross-platform equivalent is in the dependency
    # set, so leftover-process detection stays POSIX-scoped.
    if not pathlib.Path("/proc").is_dir():
        return []
    cmdlines: list[str] = []
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        cmdline = raw.replace(b"\x00", b" ").decode(errors="replace")
        if cmdline.strip():
            cmdlines.append(cmdline)
    return cmdlines


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _structured(payload: Any) -> Any:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return payload
    if isinstance(payload, dict) and "structuredContent" in payload:
        return payload["structuredContent"]
    return payload


def _line_with(text: str, needle: str) -> str:
    return next(line for line in text.splitlines() if needle in line)


def test_real_runtime_logging_bench(tmp_path: pathlib.Path) -> None:
    home = tmp_path / "home"
    mcp_port, client_port = _free_port(), _free_port()
    config_path = _seed_home(home)

    import mcp_relay.config as config

    config.init_config(config_path, "client", env={})
    validate = _run_cli(home, "config", "validate")
    assert validate.returncode == 0, f"validate: {validate.stdout}{validate.stderr}"

    # Client config: no YAML aliases — the alias is added through the
    # admin surface during the bench so the admin log path is exercised.
    raw = yaml.safe_load(config_path.read_text())
    assert set(raw) == {"identity", "relay_url", "workspace", "mcp_servers", "admin"}
    raw["admin"] = True
    raw["relay_url"] = f"ws://127.0.0.1:{client_port}/ws"
    raw["mcp_servers"] = {}
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    config_path.chmod(0o600)

    mini_server = tmp_path / "mini_mcp_server.py"
    mini_server.write_text(_MINI_MCP_SERVER, encoding="utf-8")

    env = dict(
        os.environ,
        HOME=str(home),
        USERPROFILE=str(home),
        RELAY_SERVER_MCP_HOST="127.0.0.1",
        RELAY_SERVER_MCP_PORT=str(mcp_port),
        RELAY_SERVER_CLIENT_HOST="127.0.0.1",
        RELAY_SERVER_CLIENT_PORT=str(client_port),
        RELAY_MCP_TOKEN=MCP_TOKEN,
        RELAY_CLIENT_TOKEN=CLIENT_TOKEN,
    )
    server_out = tmp_path / "server.out"
    client_out = tmp_path / "client.out"
    server_log = home / ".mcp-relay" / "server.log"
    client_log = home / ".mcp-relay" / "client.log"
    server_proc: subprocess.Popen[bytes] | None = None
    client_proc: subprocess.Popen[bytes] | None = None
    echo_result: dict[str, Any] = {}
    try:
        with server_out.open("wb") as s_out, client_out.open("wb") as c_out:
            server_proc = subprocess.Popen(
                [sys.executable, "-m", "mcp_relay.cli", "server"],
                stdout=s_out,
                stderr=subprocess.STDOUT,
                env=env,
            )
            _wait_for_port(mcp_port)
            _wait_for_port(client_port)

            client_proc = subprocess.Popen(
                [sys.executable, "-m", "mcp_relay.cli", "client"],
                stdout=c_out,
                stderr=subprocess.STDOUT,
                env=env,
            )
            _wait_for_log(client_log, REGISTRATION_LINE)

            async def scenario() -> None:
                # Raw HTTP probes: 401 (no token), 404 (unknown path).
                async with httpx2.AsyncClient(
                    base_url=f"http://127.0.0.1:{mcp_port}",
                ) as http:
                    unauth = await http.post(
                        "/mcp",
                        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                        headers={"Accept": "application/json, text/event-stream"},
                    )
                    assert unauth.status_code == 401, unauth.status_code
                    missing = await http.get("/definitely-not-a-relay-path")
                    assert missing.status_code == 404, missing.status_code

                # 2xx: a real MCP session (initialize → POST /mcp 200),
                # the admin cycle and one relayed execution.
                async with httpx2.AsyncClient(
                    base_url=f"http://127.0.0.1:{mcp_port}",
                    headers={"Authorization": f"Bearer {MCP_TOKEN}"},
                ) as http:
                    async with streamable_http_client(
                        f"http://127.0.0.1:{mcp_port}/mcp",
                        http_client=http,
                        terminate_on_close=True,
                    ) as (read_stream, write_stream):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()

                            async def invoke(tool: str, arguments: dict) -> dict:
                                result = await session.call_tool(tool, arguments)
                                assert result.is_error is False, result
                                if result.structured_content is not None:
                                    return result.structured_content
                                assert result.content
                                payload = json.loads(result.content[0].text)
                                if "structuredContent" in payload:
                                    return payload["structuredContent"]
                                return payload

                            added = await invoke(
                                "relay_mcp_add",
                                {
                                    "alias": ALIAS,
                                    "entry": {
                                        "command": [sys.executable, str(mini_server)]
                                    },
                                },
                            )
                            assert added["status"] == "running", added

                            for _ in range(300):
                                names = {t.name for t in (await session.list_tools()).tools}
                                if f"{ALIAS}_echo" in names:
                                    break
                                await anyio.sleep(0.1)
                            assert f"{ALIAS}_echo" in names, names

                            command = await session.call_tool(
                                f"{ALIAS}_echo", {"text": "log-bench"}
                            )
                            assert command.is_error is False, command.content
                            if command.structured_content is not None:
                                echo_result["payload"] = command.structured_content
                            else:
                                block = command.content[0]
                                echo_result["payload"] = getattr(
                                    block, "text", str(block)
                                )

            anyio.run(scenario)

            # The execution and admin events reached client.log, and the
            # 401/404 access lines reached server.log, all before teardown.
            _wait_for_log(client_log, "mcp.command start: request_id=")
            _wait_for_log(client_log, "mcp.command done: request_id=")
            _wait_for_log(client_log, "mcp.admin: operation=mcp.add")
            _wait_for_log(server_log, "POST /mcp HTTP/1.1 401")
            _wait_for_log(server_log, "GET /definitely-not-a-relay-path HTTP/1.1 404")

    finally:
        _terminate(client_proc)
        _terminate(server_proc)

    # The relayed result is the target tool's native result.
    payload = _structured(echo_result["payload"])
    assert "log-bench" in json.dumps(payload, default=str), payload

    # Bounded wait for the Server's shutdown lines to land in server.log.
    # POSIX only: Windows terminate() is TerminateProcess (see _terminate),
    # a hard kill that never reaches Uvicorn's graceful shutdown path, so
    # the shutdown lines can never be logged there.
    if sys.platform != "win32":
        _wait_for_log(server_log, "Application shutdown complete", deadline=20.0)

    server_text = server_log.read_text(errors="replace")
    client_text = client_log.read_text(errors="replace")

    # --- Uvicorn lifecycle: two listeners → two normal banners, not dups.
    assert server_text.count("Started server process") == 2, "one per listener"
    # serve(sockets=[...]) binds pre-created listeners, so uvicorn emits no
    # per-listener "Uvicorn running on" banner — startup is proven above.
    if sys.platform != "win32":
        # Windows terminate() is TerminateProcess: no graceful Uvicorn
        # shutdown, so the lifecycle lines never reach the log there.
        assert server_text.count("Shutting down") == 2
        assert server_text.count("Application shutdown complete") == 2

    # --- MCP session manager: exactly one startup line, at INFO.
    assert server_text.count("StreamableHTTP session manager started") == 1
    session_line = _line_with(server_text, "StreamableHTTP session manager started")
    assert "[INFO]" in session_line, session_line

    # --- Access lines with status-derived levels.
    ok_line = _line_with(server_text, "POST /mcp HTTP/1.1 200")
    assert "[INFO]" in ok_line, ok_line
    unauth_line = _line_with(server_text, "POST /mcp HTTP/1.1 401")
    assert "[WARNING]" in unauth_line, unauth_line
    missing_line = _line_with(server_text, "GET /definitely-not-a-relay-path HTTP/1.1 404")
    assert "[WARNING]" in missing_line, missing_line
    # No query strings survive into any access line (secret stripping).
    for line in server_text.splitlines():
        if "HTTP/1.1" in line:
            assert "?" not in line, line

    # --- Client events: start DEBUG, done INFO with isError, admin INFO.
    start_line = _line_with(client_text, "mcp.command start: request_id=")
    assert "[DEBUG]" in start_line, start_line
    done_line = _line_with(client_text, "mcp.command done: request_id=")
    assert "[INFO]" in done_line and "isError=false" in done_line, done_line
    admin_line = _line_with(client_text, "mcp.admin: operation=mcp.add")
    assert "[INFO]" in admin_line and "result=ok" in admin_line, admin_line
    assert f"alias={ALIAS}" in admin_line, admin_line

    # --- Shared format: every file line is the UTC-ms [LEVEL] operator line.
    for text in (server_text, client_text):
        for line in text.splitlines():
            assert _OPERATOR_LINE_RE.match(line), f"non-operator line: {line!r}"

    # --- No credentials or payloads anywhere.
    for text in (server_text, client_text):
        assert MCP_TOKEN not in text
        assert CLIENT_TOKEN not in text
        assert "log-bench" not in text  # tool arguments are never logged
        assert "Authorization" not in text
    for capture in (server_out, client_out):
        captured = capture.read_text(errors="replace")
        assert MCP_TOKEN not in captured
        assert CLIENT_TOKEN not in captured

    # --- Cleanup: no leftover relay processes, ports freed.
    leftovers = [line for line in _proc_cmdlines() if "mcp_relay.cli" in line]
    assert leftovers == [], leftovers
    # Windows may keep a killed process's listener in TIME_WAIT for a few
    # seconds; poll instead of failing on the first probe.
    for port in (mcp_port, client_port):
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                probe.settimeout(0.5)
                if probe.connect_ex(("127.0.0.1", port)) != 0:
                    break
            time.sleep(0.5)
        else:
            raise AssertionError(f"port {port} still open after 10s")
