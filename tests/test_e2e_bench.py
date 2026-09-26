"""Portable Linux/Windows E2E: real server + client, real npm MCP server.

Integration fixture for the CI E2E bench. It boots the real ``mcp-relay
server`` and ``mcp-relay client`` as subprocesses in an isolated temporary
home, then — through the authenticated MCP facade — exercises EVERY fixed
surface command: ``relay_server_status``, ``relay_client_status``,
``relay_mcp_list``, ``relay_mcp_add`` (the ``fs`` alias), ``relay_mcp_modify``,
``relay_mcp_enable``/``relay_mcp_disable`` and ``relay_mcp_delete``, plus a
real file round trip against ``@modelcontextprotocol/server-filesystem``
(pinned) spawned via node from a pre-warmed npx cache:

list_directory -> write_file -> read_text_file (identical content)
-> get_file_info.

The bench never skips: a missing node/npx or a failed cache warm-up is a
hard failure. Spawned processes are terminated and verified gone, and the
listener ports are verified released.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
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

MCP_TOKEN = "bench-mcp-token-e2e-synthetic-credential-0000000000000000"
CLIENT_TOKEN = "bench-client-token-e2e-synthetic-credential-0000000000000000"
ALIAS = "fs"
FS_PACKAGE = "@modelcontextprotocol/server-filesystem"
FS_VERSION = "2026.8.31"
REGISTRATION_LINE = "authenticated registration succeeded"
FIXED_SURFACE = {
    "relay_server_status",
    "relay_registry_search",
    "relay_client_status",
    "relay_mcp_list",
    "relay_mcp_command",
    "relay_mcp_add",
    "relay_mcp_modify",
    "relay_mcp_delete",
    "relay_mcp_enable",
    "relay_mcp_disable",
}


def _npx_cache_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    # Dedicated per-run npm cache: the warm-up step fills it and the alias
    # spawn reads from it offline; nothing leaks into the runner's cache.
    cache = tmp_path / "npx-cache"
    cache.mkdir()
    return cache


def _npx_env(cache: pathlib.Path) -> dict[str, str]:
    env = dict(os.environ)
    env["npm_config_cache"] = str(cache)
    return env


def _resolve_fs_entrypoint(cache: pathlib.Path) -> pathlib.Path:
    """Locate the warmed package's JS entry inside the npx cache."""
    matches = sorted(cache.glob("_npx/*/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js"))
    if not matches:
        raise AssertionError(
            f"{FS_PACKAGE} entry point not found in warmed npx cache {cache}"
        )
    return matches[0].resolve()


def _node_executable() -> str:
    node = shutil_which("node")
    if node is None:
        raise AssertionError("node is not on the PATH; the fs E2E bench cannot run")
    return node


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


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


def _relay_command() -> list[str]:
    """Command prefix to run the mcp-relay CLI under test.

    When MCP_RELAY_BIN is set (CI install-smoke), use the installed binary
    produced by the one-line installer — proving the packaged entry point.
    Otherwise fall back to the checkout module (local dev).
    """
    override = os.environ.get("MCP_RELAY_BIN")
    if override:
        return [override]
    return [sys.executable, "-m", "mcp_relay.cli"]


def _run_cli(home: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    return subprocess.run(
        [*_relay_command(), *args],
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


def _wait_for_log(path: pathlib.Path, needle: str, deadline: float = 90.0) -> None:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if path.exists() and needle in path.read_text(errors="replace"):
            return
        time.sleep(0.2)
    raise AssertionError(f"{needle!r} not found in {path} within {deadline}s")


def _foreign_cmdlines() -> list[str]:
    """Command lines of leftover relay / node bench processes (portable)."""
    cmdlines: list[str] = []
    proc = pathlib.Path("/proc")
    if proc.is_dir():  # POSIX: direct /proc scan
        for entry in proc.iterdir():
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
    # Windows: PowerShell CIM query (wmic is deprecated).
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | "
            "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.stdout.splitlines()


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    # terminate(): SIGTERM on POSIX, TerminateProcess on Windows.
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _structured(payload: Any) -> Any:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, dict) and "structuredContent" in payload:
        return payload["structuredContent"]
    return payload


def test_real_filesystem_e2e_bench(tmp_path: pathlib.Path) -> None:
    # Preflight: imports plus the pinned npm fixture server. No skips here —
    # a missing toolchain or failed warm-up is a hard bench failure.
    import fastmcp  # noqa: F401
    import mcp  # noqa: F401

    node = _node_executable()
    npx = shutil_which("npx")
    if npx is None:
        raise AssertionError("npx is not on the PATH; the fs E2E bench cannot run")
    cache = _npx_cache_dir(tmp_path)
    warm = subprocess.run(
        # Install the pinned package into the cache without running the
        # server binary (it treats every argument as an allowed directory).
        [npx, "-y", "-p", f"{FS_PACKAGE}@{FS_VERSION}", "-c", "echo warmed"],
        env=_npx_env(cache),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert warm.returncode == 0, (
        f"npx cache warm-up failed for {FS_PACKAGE}@{FS_VERSION} "
        f"(rc={warm.returncode}): {warm.stdout}{warm.stderr}"
    )
    fs_entry = _resolve_fs_entrypoint(cache)

    home = tmp_path / "home"
    mcp_port, client_port = _free_port(), _free_port()
    fs_root = tmp_path / "fs-root"
    fs_root.mkdir()
    config_path = _seed_home(home)

    # Current init/validate sequence (server settings are environment-only).
    import mcp_relay.config as config

    config.init_config(config_path, "client", env={})
    validate = _run_cli(home, "config", "validate")
    assert validate.returncode == 0, f"validate: {validate.stdout}{validate.stderr}"

    # Flat client YAML: top-level mcp_servers, seeded EMPTY — the fs alias is
    # added at runtime through relay_mcp_add (admin CRUD proof).
    raw = yaml.safe_load(config_path.read_text())
    assert set(raw) == {"identity", "relay_url", "workspace", "mcp_servers", "admin"}
    raw["relay_url"] = f"ws://127.0.0.1:{client_port}/ws"
    raw["mcp_servers"] = {}
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    config_path.chmod(0o600)
    # Alias spawn env: PATH from the operator reaches the spawned server.
    alias_env = home / ".mcp-relay" / "mcp" / f"{ALIAS}.env"
    alias_env.parent.mkdir(parents=True, exist_ok=True)
    alias_env.write_text(f"PATH={os.environ.get('PATH', '')}\n")
    alias_env.chmod(0o600)

    fs_command = [node, str(fs_entry), str(fs_root)]
    server_out = tmp_path / "server.out"
    client_out = tmp_path / "client.out"
    server_proc: subprocess.Popen[bytes] | None = None
    client_proc: subprocess.Popen[bytes] | None = None
    try:
        with server_out.open("wb") as server_log, client_out.open("wb") as client_log:
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
            server_proc = subprocess.Popen(
                [*_relay_command(), "server"],
                stdout=server_log,
                stderr=subprocess.STDOUT,
                env=env,
            )
            _wait_for_port(mcp_port)
            _wait_for_port(client_port)

            client_proc = subprocess.Popen(
                [*_relay_command(), "client"],
                stdout=client_log,
                stderr=subprocess.STDOUT,
                env=env,
            )
            _wait_for_log(home / ".mcp-relay" / "client.log", REGISTRATION_LINE)

            async def scenario() -> None:
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
                            tools = await session.list_tools()
                            names = {tool.name for tool in tools.tools}
                            assert len(names) == 10, names
                            assert names == FIXED_SURFACE, names

                            async def invoke(
                                tool: str, arguments: dict[str, Any]
                            ) -> Any:
                                result = await session.call_tool(tool, arguments)
                                assert result.is_error is False, f"{tool}: {result}"
                                return _structured(
                                    result.structured_content
                                    or getattr(result.content[0], "text", None)
                                )

                            # Control tools before any alias exists.
                            server_status = await invoke("relay_server_status", {})
                            assert server_status, server_status
                            client_status = await invoke("relay_client_status", {})
                            assert client_status["client"]["protocol"] == 1

                            listing = await invoke("relay_mcp_list", {})
                            assert listing["level"] == "servers"
                            assert listing["items"] == []

                            # relay_mcp_add: declare + start the fs alias.
                            add = await invoke(
                                "relay_mcp_add",
                                {"alias": ALIAS, "entry": {"command": fs_command}},
                            )
                            assert add["status"] == "running", add

                            listing = await invoke("relay_mcp_list", {})
                            assert listing["level"] == "servers"
                            assert [item["alias"] for item in listing["items"]] == [ALIAS]
                            assert listing["items"][0]["runtime_state"] == "running"
                            assert listing["items"][0]["transport"] == "stdio"
                            assert listing["items"][0]["entry"]["command"] == fs_command

                            # Discovery of the relayed fs_* tools.
                            tools_listing = await invoke(
                                "relay_mcp_list", {"alias": ALIAS}
                            )
                            assert tools_listing["level"] == "tools"
                            tool_names = {
                                item["name"] for item in tools_listing["items"]
                            }
                            assert {
                                "list_directory",
                                "write_file",
                                "read_text_file",
                                "get_file_info",
                            } <= tool_names, tool_names
                            revision = tools_listing["catalog_revision"]

                            async def relayed(
                                tool: str, arguments: dict[str, Any]
                            ) -> Any:
                                command = await session.call_tool(
                                    "relay_mcp_command",
                                    {
                                        "alias": ALIAS,
                                        "tool": tool,
                                        "arguments": arguments,
                                        "catalog_revision": revision,
                                    },
                                )
                                assert command.is_error is False, f"{tool}: {command}"
                                payload = _structured(
                                    command.structured_content
                                    or getattr(command.content[0], "text", None)
                                )
                                # The native passthrough may wrap the target
                                # tool's result as {"content": "<json str>"};
                                # keep plain-text content as-is.
                                while (
                                    isinstance(payload, dict)
                                    and set(payload) == {"content"}
                                    and isinstance(payload["content"], str)
                                ):
                                    inner = payload["content"]
                                    try:
                                        payload = _structured(inner)
                                    except ValueError:
                                        payload = inner
                                        break
                                return payload

                            # Real file round trip through the relay.
                            target = fs_root / "bench.txt"
                            content = "mcp-relay CI bench éè\nline2\n"
                            dir_listing = await relayed(
                                "list_directory", {"path": str(fs_root)}
                            )
                            assert "bench.txt" not in json.dumps(dir_listing)
                            await relayed(
                                "write_file", {"path": str(target), "content": content}
                            )
                            read_back = await relayed(
                                "read_text_file", {"path": str(target)}
                            )
                            text = (
                                read_back
                                if isinstance(read_back, str)
                                else json.dumps(read_back)
                            )
                            # The native result may arrive JSON-escaped.
                            round_trip = text
                            if text.lstrip().startswith("{"):
                                try:
                                    round_trip = _structured(text)["content"]
                                except (KeyError, TypeError, ValueError):
                                    pass
                            assert round_trip == content, read_back
                            info = await relayed("get_file_info", {"path": str(target)})
                            info_text = (
                                info if isinstance(info, str) else json.dumps(info)
                            )
                            match = re.search(r"size:\s*(\d+)", info_text)
                            assert match is not None, info
                            assert int(match.group(1)) == len(content.encode()), info

                            # relay_mcp_modify: replace the alias entry.
                            modified = await invoke(
                                "relay_mcp_modify",
                                {"alias": ALIAS, "entry": {"command": fs_command}},
                            )
                            assert modified["status"] == "running", modified

                            # enable/disable round trip.
                            disabled = await invoke("relay_mcp_disable", {"alias": ALIAS})
                            assert disabled["runtime_state"] == "disabled", disabled
                            enabled = await invoke("relay_mcp_enable", {"alias": ALIAS})
                            assert enabled["runtime_state"] == "running", enabled

                            # relay_mcp_delete with strict existence.
                            deleted = await invoke("relay_mcp_delete", {"alias": ALIAS})
                            assert deleted == {"alias": ALIAS, "status": "deleted"}
                            listing = await invoke("relay_mcp_list", {})
                            assert listing["items"] == []

            anyio.run(scenario)

    finally:
        _terminate(client_proc)
        _terminate(server_proc)

    # Cleanup verification: the processes we owned are gone (poll()), the
    # fs server (unique entry path under this run's tmp) left nothing behind
    # and both listener ports are released.
    assert server_proc is not None and server_proc.poll() is not None
    assert client_proc is not None and client_proc.poll() is not None
    leftovers = [
        line
        for line in _foreign_cmdlines()
        if str(fs_entry) in line
    ]
    assert leftovers == [], leftovers
    for port in (mcp_port, client_port):
        with socket.socket() as probe:
            probe.settimeout(0.5)
            assert probe.connect_ex(("127.0.0.1", port)) != 0, f"port {port} still open"
