from __future__ import annotations

import asyncio
import base64
import contextlib
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import mcp_relay.server as server_module
from mcp_relay.client import ClientSettings, RelayClient
from mcp_relay.config import ConfigError, load_server_runtime
from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog
from mcp_relay.output_models import ProviderToolResult
from mcp_relay.provider_tools import ProviderToolDescriptor
from mcp_relay.server import RelaySettings

_HOST = "127.0.0.1"
# A second loopback address every supported platform configures by default;
# macOS, unlike Linux and Windows, does not answer on all of 127.0.0.0/8.
_CLIENT_HOST = "::1"


def _settings(
    *,
    mcp_port: int,
    client_port: int,
    mcp_host: str = _HOST,
    client_host: str = _HOST,
) -> RelaySettings:
    return RelaySettings(
        client_id="client-a",
        client_token='client-secret-synthetic-credential-0000000000000000',
        mcp_token='mcp-secret-synthetic-credential-0000000000000000',
        mcp_bind_host=mcp_host,
        mcp_port=mcp_port,
        client_bind_host=client_host,
        client_port=client_port,
    )


def _listener(host: str = _HOST, port: int = 0) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen()
    return listener


def _candidate_ports(count: int = 2) -> tuple[int, ...]:
    listeners = [_listener() for _ in range(count)]
    try:
        return tuple(listener.getsockname()[1] for listener in listeners)
    finally:
        for listener in listeners:
            listener.close()


def test_reserves_both_configured_listener_sockets_before_serving() -> None:
    mcp_port, client_port = _candidate_ports()

    sockets = server_module._reserve_listener_sockets(
        _HOST, mcp_port, _HOST, client_port
    )
    try:
        assert tuple(sock.getsockname()[:2] for sock in sockets) == (
            (_HOST, mcp_port),
            (_HOST, client_port),
        )
        for port in (mcp_port, client_port):
            with pytest.raises(OSError):
                competitor = _listener(port=port)
                competitor.close()
    finally:
        for reserved in sockets:
            reserved.close()


def test_reserves_independent_addresses_even_on_the_same_port() -> None:
    """Each listener binds its own configured address, not one shared socket."""
    (shared_port,) = _candidate_ports(count=1)

    sockets = server_module._reserve_listener_sockets(
        _HOST, shared_port, _CLIENT_HOST, shared_port
    )
    try:
        assert tuple(sock.getsockname()[:2] for sock in sockets) == (
            (_HOST, shared_port),
            (_CLIENT_HOST, shared_port),
        )
    finally:
        for reserved in sockets:
            reserved.close()


@pytest.mark.parametrize("collision", ["mcp", "client"])
def test_listener_collision_is_explicit_and_releases_every_acquired_socket(
    collision: str,
) -> None:
    mcp_port, client_port = _candidate_ports()
    occupied_port = mcp_port if collision == "mcp" else client_port
    blocker = _listener(port=occupied_port)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            server_module._reserve_listener_sockets(
                _HOST, mcp_port, _HOST, client_port
            )

        message = str(exc_info.value)
        assert f"{collision.upper()} listener" in message
        assert f"{_HOST}:{occupied_port}" in message

        # A client-listener collision happens after the MCP socket was
        # acquired. Being able to bind the MCP port proves the failed atomic
        # reservation closed it.
        if collision == "client":
            rebound = _listener(port=mcp_port)
            rebound.close()
    finally:
        blocker.close()

    # The failed socket itself is closed too; there is no fallback listener.
    rebound = _listener(port=occupied_port)
    rebound.close()


def test_client_listener_collision_closes_mcp_socket_bound_to_a_different_address() -> None:
    """Both-or-neither holds even when the listeners use different addresses."""
    (shared_port,) = _candidate_ports(count=1)
    blocker = _listener(_CLIENT_HOST, shared_port)
    try:
        with pytest.raises(RuntimeError, match="CLIENT listener"):
            server_module._reserve_listener_sockets(
                _HOST, shared_port, _CLIENT_HOST, shared_port
            )
        # The MCP socket, bound to a different address, was released too.
        rebound = _listener(_HOST, shared_port)
        rebound.close()
    finally:
        blocker.close()


def test_two_uvicorn_servers_serve_concurrently_in_one_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_port, client_port = _candidate_ports()
    mcp_app = FastAPI()
    client_app = FastAPI()
    calls: list[tuple[uvicorn.Server, list[socket.socket], int, tuple[str, int]]] = []

    monkeypatch.setattr(
        server_module,
        "_create_listener_apps",
        lambda _settings: (mcp_app, client_app),
    )

    def forbidden_uvicorn_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the two-listener runtime must not call uvicorn.run")

    monkeypatch.setattr(uvicorn, "run", forbidden_uvicorn_run)

    async def scenario() -> None:
        both_started = asyncio.Event()
        loop_id = id(asyncio.get_running_loop())

        async def fake_serve(
            instance: uvicorn.Server,
            sockets: list[socket.socket] | None = None,
        ) -> None:
            assert sockets is not None and len(sockets) == 1
            sock = sockets[0]
            calls.append(
                (instance, sockets, id(asyncio.get_running_loop()), sock.getsockname()[:2])
            )
            if len(calls) == 2:
                both_started.set()
            await both_started.wait()
            if instance.config.app is mcp_app:
                return
            while not instance.should_exit:
                await asyncio.sleep(0)

        monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
        await server_module._serve_relay(
            _settings(mcp_port=mcp_port, client_port=client_port)
        )

        assert {call[2] for call in calls} == {loop_id}

    asyncio.run(scenario())

    assert len(calls) == 2
    by_app = {call[0].config.app: call for call in calls}
    assert by_app[mcp_app][3] == (_HOST, mcp_port)
    assert by_app[client_app][3] == (_HOST, client_port)
    assert by_app[mcp_app][0].config.host == _HOST
    assert by_app[mcp_app][0].config.port == mcp_port
    assert by_app[client_app][0].config.host == _HOST
    assert by_app[client_app][0].config.port == client_port
    assert isinstance(by_app[mcp_app][0], server_module._PrimaryUvicornServer)
    assert isinstance(by_app[client_app][0], server_module._SignalFreeUvicornServer)
    assert by_app[client_app][0].should_exit is True
    assert all(call[1][0].fileno() == -1 for call in calls)


def test_serve_relay_configures_each_uvicorn_listener_with_its_own_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two uvicorn.Config objects carry the respective configured hosts."""
    mcp_port, client_port = _candidate_ports()
    mcp_app = FastAPI()
    client_app = FastAPI()
    configs: list[uvicorn.Config] = []

    monkeypatch.setattr(
        server_module,
        "_create_listener_apps",
        lambda _settings: (mcp_app, client_app),
    )

    async def fake_serve(
        self: uvicorn.Server,
        sockets: list[socket.socket] | None = None,
    ) -> None:
        del self, sockets

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)

    original_primary = server_module._PrimaryUvicornServer
    original_signal_free = server_module._SignalFreeUvicornServer

    class RecordingPrimary(original_primary):
        def __init__(self, config: uvicorn.Config) -> None:
            super().__init__(config)
            configs.append(config)

    class RecordingSignalFree(original_signal_free):
        def __init__(self, config: uvicorn.Config) -> None:
            super().__init__(config)
            configs.append(config)

    monkeypatch.setattr(server_module, "_PrimaryUvicornServer", RecordingPrimary)
    monkeypatch.setattr(server_module, "_SignalFreeUvicornServer", RecordingSignalFree)

    async def capture() -> None:
        task = asyncio.create_task(
            server_module._serve_relay(
                _settings(
                    mcp_port=mcp_port,
                    client_port=client_port,
                    mcp_host="127.0.0.1",
                    client_host="0.0.0.0",
                )
            )
        )
        await asyncio.sleep(0)
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(capture())

    by_app = {config.app: config for config in configs}
    assert by_app[mcp_app].host == "127.0.0.1"
    assert by_app[mcp_app].port == mcp_port
    assert by_app[client_app].host == "0.0.0.0"
    assert by_app[client_app].port == client_port


def test_only_primary_uvicorn_server_owns_signal_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_by: list[uvicorn.Server] = []

    @contextmanager
    def observed_capture(instance: uvicorn.Server) -> Iterator[None]:
        captured_by.append(instance)
        yield

    monkeypatch.setattr(uvicorn.Server, "capture_signals", observed_capture)
    primary = server_module._PrimaryUvicornServer(
        uvicorn.Config(FastAPI(), log_config=None)
    )
    secondary = server_module._SignalFreeUvicornServer(
        uvicorn.Config(FastAPI(), log_config=None)
    )

    with primary.capture_signals(), secondary.capture_signals():
        pass

    assert captured_by == [primary]


def test_server_failure_stops_peer_and_releases_both_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExpectedFailure(RuntimeError):
        pass

    mcp_port, client_port = _candidate_ports()
    mcp_app = FastAPI()
    client_app = FastAPI()
    servers: dict[object, uvicorn.Server] = {}
    served_sockets: list[socket.socket] = []
    peer_saw_exit = False

    monkeypatch.setattr(
        server_module,
        "_create_listener_apps",
        lambda _settings: (mcp_app, client_app),
    )

    async def scenario() -> None:
        both_started = asyncio.Event()

        async def fake_serve(
            instance: uvicorn.Server,
            sockets: list[socket.socket] | None = None,
        ) -> None:
            nonlocal peer_saw_exit
            assert sockets is not None and len(sockets) == 1
            servers[instance.config.app] = instance
            served_sockets.extend(sockets)
            if len(servers) == 2:
                both_started.set()
            await both_started.wait()
            if instance.config.app is mcp_app:
                raise ExpectedFailure("MCP listener failed")
            while not instance.should_exit:
                await asyncio.sleep(0)
            peer_saw_exit = True

        monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
        with pytest.raises(ExpectedFailure, match="MCP listener failed"):
            await server_module._serve_relay(
                _settings(mcp_port=mcp_port, client_port=client_port)
            )

    asyncio.run(scenario())

    assert peer_saw_exit is True
    assert servers[client_app].should_exit is True
    assert len(served_sockets) == 2
    assert all(sock.fileno() == -1 for sock in served_sockets)


def test_both_server_failures_are_reported_after_draining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class McpListenerFailure(RuntimeError):
        pass

    mcp_failure = McpListenerFailure("MCP listener failed distinctly")
    client_failure = SystemExit("client listener failed distinctly")
    mcp_port, client_port = _candidate_ports()
    mcp_app = FastAPI()
    client_app = FastAPI()
    failures: dict[object, BaseException] = {
        mcp_app: mcp_failure,
        client_app: client_failure,
    }

    monkeypatch.setattr(
        server_module,
        "_create_listener_apps",
        lambda _settings: (mcp_app, client_app),
    )

    async def scenario() -> None:
        both_started = asyncio.Event()
        started_apps: set[object] = set()

        async def fake_serve(
            instance: uvicorn.Server,
            sockets: list[socket.socket] | None = None,
        ) -> None:
            assert sockets is not None and len(sockets) == 1
            started_apps.add(instance.config.app)
            if len(started_apps) == 2:
                both_started.set()
            await both_started.wait()
            raise failures[instance.config.app]

        monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
        with pytest.raises(BaseExceptionGroup) as exc_info:
            await server_module._serve_relay(
                _settings(mcp_port=mcp_port, client_port=client_port)
            )

        group = exc_info.value
        assert type(group) is BaseExceptionGroup
        assert len(group.exceptions) == 2
        assert group.exceptions[0] is mcp_failure
        assert group.exceptions[1] is client_failure
        assert tuple(str(exc) for exc in group.exceptions) == (
            "MCP listener failed distinctly",
            "client listener failed distinctly",
        )

    asyncio.run(scenario())


class _ProbeProvider:
    """Route provider for the ``probe`` alias, observable and optionally blocking."""

    def __init__(self, *, block: bool = False) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls: list[str] = []
        self._block = block

    async def call_tool(self, name: str, arguments: dict[str, object]) -> ProviderToolResult:
        self.calls.append(name)
        self.started.set()
        try:
            if self._block:
                await asyncio.Event().wait()
            return ProviderToolResult(
                content=[], structuredContent={"probe": "through-both-listeners"}
            )
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def _probe_catalog(provider: _ProbeProvider) -> ClientCatalog:
    catalog = ClientCatalog()
    catalog.update_alias(
        AliasCatalog(
            alias="probe",
            enabled=True,
            runtime_state="running",
            transport="stdio",
            catalog_available=True,
            error=None,
            descriptors=(
                ProviderToolDescriptor(
                    provider_name="probe",
                    tool_name="status",
                    description="Dual-listener runtime probe",
                    input_schema={"type": "object"},
                ),
            ),
            provider=provider,
        )
    )
    return catalog


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 5.0
) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _assert_ports_released(host: str, *ports: int) -> None:
    # On Windows a venv python.exe is a launcher: killing it ends the real
    # interpreter, which holds the exclusive listeners, a moment later.
    deadline = time.monotonic() + 5
    for port in ports:
        while True:
            try:
                rebound = _listener(host, port)
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
                continue
            rebound.close()
            break


def _server_process(
    config_path: Path,
    *,
    mcp_port: int,
    client_port: int,
    client_host: str = _HOST,
    output: int | TextIO = subprocess.PIPE,
) -> subprocess.Popen[str]:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)
    environment = os.environ.copy()
    for name in (
        "RELAY_SERVER_MCP_HOST",
        "RELAY_SERVER_MCP_PORT",
        "RELAY_SERVER_CLIENT_HOST",
        "RELAY_SERVER_CLIENT_PORT",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "LOG_LEVEL": "INFO",
            "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
            "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
            "RELAY_SERVER_MCP_HOST": _HOST,
            "RELAY_SERVER_MCP_PORT": str(mcp_port),
            "RELAY_SERVER_CLIENT_HOST": client_host,
            "RELAY_SERVER_CLIENT_PORT": str(client_port),
        }
    )
    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mcp_relay.cli",
            "server",
        ],
        cwd=config_path.parent,
        env={
            **environment,
            "HOME": str(config_path.parent.parent),
            "USERPROFILE": str(config_path.parent.parent),
        },
        stdout=output,
        stderr=output,
        text=True,
        start_new_session=os.name == "posix",
        creationflags=creationflags,
    )


def _http_status(host: str, port: int, path: str) -> int | None:
    connection = http.client.HTTPConnection(host, port, timeout=0.1)
    try:
        connection.request("GET", path)
        return connection.getresponse().status
    except OSError:
        return None
    finally:
        connection.close()


def _wait_for_process_listeners(
    process: subprocess.Popen[str], *ports: int, timeout: float = 5.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                "relay process exited before both listeners started: "
                f"stdout={stdout!r}, stderr={stderr!r}"
            )
        if (
            _http_status(_HOST, ports[0], "/mcp") == 401
            and _http_status(_HOST, ports[1], "/") == 404
        ):
            return
        time.sleep(0.01)
    pytest.fail(f"relay process did not serve the requested ports {ports!r}")


def _wait_for_process_group_exit(process_group: int, *, timeout: float = 2.0) -> None:
    if os.name == "nt":
        # os.killpg does not exist on Windows; the process.kill() fallback
        # above is the Windows shutdown path.  # pragma: no cover - POSIX only
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    pytest.fail(f"server process group {process_group} survived shutdown")


@pytest.mark.integration
def test_real_uvicorn_ws_authentication_precedes_upgrade(tmp_path: Path) -> None:
    """The loopback Uvicorn listener must not answer 101 for bad credentials."""
    mcp_port, client_port = _candidate_ports()
    process = _server_process(
        tmp_path / "config.yaml", mcp_port=mcp_port, client_port=client_port
    )
    process_group = process.pid
    handshake = {
        "Connection": "Upgrade",
        "Upgrade": "websocket",
        "Sec-WebSocket-Key": base64.b64encode(b"synthetic-key-16").decode("ascii"),
        "Sec-WebSocket-Version": "13",
    }
    try:
        _wait_for_process_listeners(process, mcp_port, client_port)
        for auth_headers, expected_status in (
            ({}, 403),
            ({"Authorization": 'Basic client-secret-synthetic-credential-0000000000000000'}, 403),
            ({"Authorization": "Bearer wrong"}, 403),
            ({"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}, 101),
        ):
            connection = http.client.HTTPConnection(_HOST, client_port, timeout=2)
            try:
                connection.request("GET", "/ws", headers={**handshake, **auth_headers})
                response = connection.getresponse()
                assert response.status == expected_status
                assert 'client-secret-synthetic-credential-0000000000000000' not in response.reason
            finally:
                connection.close()
    finally:
        if process.poll() is None:
            if os.name == "posix":
                os.killpg(process_group, signal.SIGKILL)
            else:  # pragma: no cover - Windows CI
                process.kill()
        process.communicate(timeout=2)
    _assert_ports_released(_HOST, mcp_port, client_port)


@pytest.mark.integration
def test_real_uvicorn_caps_accepted_idle_ws_before_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full listener refuses the 33rd authenticated upgrade with HTTP 403."""
    if sys.platform == "linux":
        # Reproduce small Windows-like pipe buffers on Linux too. Undrained
        # subprocess logs must not block the server during the 32 handshakes.
        from functools import partial

        monkeypatch.setattr(
            subprocess, "Popen", partial(subprocess.Popen, pipesize=4096)
        )
    mcp_port, client_port = _candidate_ports()
    # A pipe read only at teardown can fill and block Uvicorn's event loop.
    # Keep the INFO logs on disk instead; the child owns its inherited handle.
    with (tmp_path / "server.log").open("w", encoding="utf-8") as log:
        process = _server_process(
            tmp_path / "config.yaml",
            mcp_port=mcp_port,
            client_port=client_port,
            output=log,
        )
    opened: list[socket.socket] = []

    def handshake() -> tuple[socket.socket, int]:
        conn = socket.create_connection((_HOST, client_port), timeout=2)
        conn.settimeout(2)
        request = (
            "GET /ws HTTP/1.1\r\n"
            f"Host: {_HOST}:{client_port}\r\n"
            "Connection: Upgrade\r\nUpgrade: websocket\r\n"
            "Sec-WebSocket-Key: c3ludGhldGljLWtleS0xNg==\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Authorization: Bearer client-secret-synthetic-credential-0000000000000000\r\n\r\n"
        )
        try:
            conn.sendall(request.encode("ascii"))
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = conn.recv(4096)
                assert chunk, "connection closed before HTTP upgrade response"
                response += chunk
                assert len(response) < 8192
            return conn, int(response.split(b" ", 2)[1])
        except BaseException:
            conn.close()
            raise

    try:
        _wait_for_process_listeners(process, mcp_port, client_port)
        for _ in range(32):
            conn, status = handshake()
            opened.append(conn)
            assert status == 101
        refused, status = handshake()
        refused.close()
        assert status == 403
        opened.pop().close()
        deadline = time.monotonic() + 2
        while True:
            replacement, status = handshake()
            if status == 101:
                opened.append(replacement)
                break
            replacement.close()
            assert status == 403 and time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        for conn in opened:
            conn.close()
        if process.poll() is None:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - Windows CI
                process.kill()
        process.communicate(timeout=2)
    _assert_ports_released(_HOST, mcp_port, client_port)


def _nonloopback_address() -> str | None:
    """Find a real non-loopback local address, or None when unavailable."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Never sends a packet: connect() on a UDP socket only picks a route.
        probe.connect(("8.8.8.8", 53))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    if not address or address.startswith("127."):
        return None
    return address


@pytest.mark.integration
def test_real_dual_listener_runtime_relays_on_exact_reserved_ports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def scenario() -> None:
        mcp_port, client_port = _candidate_ports()
        capability = _ProbeProvider()
        listener_apps: list[tuple[FastAPI, FastAPI]] = []
        runtime_servers: list[uvicorn.Server] = []
        reserved_sockets: list[socket.socket] = []
        original_create = server_module._create_listener_apps
        original_pair = server_module._serve_uvicorn_pair
        original_reserve = server_module._reserve_listener_sockets

        def capture_apps(settings: RelaySettings) -> tuple[FastAPI, FastAPI]:
            apps = original_create(settings)
            listener_apps.append(apps)
            return apps

        def capture_reservation(
            mcp_host: str,
            configured_mcp_port: int,
            client_host: str,
            configured_client_port: int,
        ) -> tuple[socket.socket, socket.socket]:
            assert (mcp_host, configured_mcp_port) == (_HOST, mcp_port)
            assert (client_host, configured_client_port) == (_HOST, client_port)
            sockets = original_reserve(
                mcp_host, configured_mcp_port, client_host, configured_client_port
            )
            reserved_sockets.extend(sockets)
            return sockets

        async def capture_pair(
            mcp_server: uvicorn.Server,
            client_server: uvicorn.Server,
            mcp_socket: socket.socket,
            client_socket: socket.socket,
        ) -> None:
            runtime_servers.extend((mcp_server, client_server))
            await original_pair(mcp_server, client_server, mcp_socket, client_socket)

        monkeypatch.setattr(server_module, "_create_listener_apps", capture_apps)
        monkeypatch.setattr(
            server_module, "_reserve_listener_sockets", capture_reservation
        )
        monkeypatch.setattr(server_module, "_serve_uvicorn_pair", capture_pair)

        baseline_tasks = set(asyncio.all_tasks())
        runtime_task = asyncio.create_task(
            server_module._serve_relay(
                _settings(mcp_port=mcp_port, client_port=client_port)
            ),
            name="test-dual-listener-runtime",
        )
        client = RelayClient(
            ClientSettings(
                server_url=f"ws://{_HOST}:{client_port}/ws",
                client_id="client-a",
                client_token='client-secret-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            catalog=_probe_catalog(capability),
        )
        client_task = asyncio.create_task(client.run(), name="test-relay-client")
        try:
            await _wait_until(
                lambda: bool(listener_apps)
                and listener_apps[0][0].state.registry.catalog_tool("probe_status")
                is not None
            )
            mcp_app, client_app = listener_apps[0]
            registry = mcp_app.state.registry
            assert registry is client_app.state.registry
            assert [item.config.port for item in runtime_servers] == [
                mcp_port,
                client_port,
            ]
            assert tuple(sock.getsockname()[1] for sock in reserved_sockets) == (
                mcp_port,
                client_port,
            )

            headers = {"Authorization": 'Bearer mcp-secret-synthetic-credential-0000000000000000'}
            async with httpx2.AsyncClient(headers=headers) as http:
                async with streamable_http_client(
                    f"http://{_HOST}:{mcp_port}/mcp",
                    http_client=http,
                    terminate_on_close=True,
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        published = {tool.name for tool in (await session.list_tools()).tools}
                        assert "probe_status" in published
                        result = await session.call_tool("probe_status", {})

            assert result.is_error is False
            payload = result.structured_content
            if payload is None:
                payload = json.loads(getattr(result.content[0], "text"))
            if "structuredContent" in payload:
                payload = payload["structuredContent"]
            assert payload == {"probe": "through-both-listeners"}
            assert capability.calls == ["status"]
            assert len(registry._recently_completed) == 1
            assert registry.pending_count == 0
            snapshot = await registry.status_snapshot()
            assert snapshot.connected is True
            assert snapshot.published_tools == 1
        finally:
            client.stop()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(client_task, timeout=2)
            if not runtime_task.done():
                runtime_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(runtime_task, timeout=2)

        assert client_task.done()
        assert runtime_task.done()
        assert all(sock.fileno() == -1 for sock in reserved_sockets)
        _assert_ports_released(_HOST, mcp_port, client_port)
        await _wait_until(
            lambda: not [
                task
                for task in asyncio.all_tasks()
                if task not in baseline_tasks and not task.done()
            ],
            timeout=2,
        )

    asyncio.run(scenario())


@pytest.mark.integration
def test_nonloopback_address_reaches_only_the_wildcard_client_listener(
    tmp_path: Path,
) -> None:
    """MCP binds loopback; the wildcard client listener serves other interfaces.

    Uses a real subprocess server: the MCP listener on 127.0.0.1 must NOT be
    reachable via a non-loopback local address, while the client listener on
    0.0.0.0 must be. Skipped (honestly) when no non-loopback address exists.
    """
    nonloopback = _nonloopback_address()
    if nonloopback is None:
        pytest.skip(
            "no non-loopback local address available; per-interface isolation "
            "cannot be exercised on this host"
        )
    mcp_port, client_port = _candidate_ports()
    process = _server_process(
        tmp_path / "config.yaml",
        mcp_port=mcp_port,
        client_port=client_port,
        client_host="0.0.0.0",
    )
    process_group = process.pid
    try:
        _wait_for_process_listeners(process, mcp_port, client_port)
        # The wildcard client listener accepts connections via the real
        # non-loopback interface.
        assert _http_status(nonloopback, client_port, "/") == 404
        # The MCP listener is loopback-only and must not answer there.
        assert _http_status(nonloopback, mcp_port, "/mcp") is None
    finally:
        if process.poll() is None:
            if os.name == "posix":
                os.killpg(process_group, signal.SIGKILL)
            else:  # pragma: no cover - exercised on Windows CI
                process.kill()
            process.communicate(timeout=2)
    _assert_ports_released(_HOST, mcp_port, client_port)
    _wait_for_process_group_exit(process_group)


_SIGNAL_CASES = [
    pytest.param(
        getattr(signal, name, None),
        id=name,
        marks=[
            pytest.mark.skipif(
                not hasattr(signal, name),
                reason=f"{name} does not exist on this platform",
            ),
            pytest.mark.skipif(
                name == "SIGINT" and os.name == "nt",
                reason="subprocess.send_signal(SIGINT) is unsupported on Windows "
                "(only SIGTERM, CTRL_C_EVENT and CTRL_BREAK_EVENT); real "
                "CTRL_C_EVENT delivery to a process group is not portable",
            ),
        ],
    )
    for name in ("SIGINT", "SIGTERM")
]


@pytest.mark.integration
@pytest.mark.parametrize("shutdown_signal", _SIGNAL_CASES)
def test_real_process_signal_stops_both_listeners_without_survivors(
    shutdown_signal: int | None, tmp_path: Path
) -> None:
    assert shutdown_signal is not None
    mcp_port, client_port = _candidate_ports()
    process = _server_process(
        tmp_path / "config.yaml", mcp_port=mcp_port, client_port=client_port
    )
    process_group = process.pid
    try:
        _wait_for_process_listeners(process, mcp_port, client_port)
        process.send_signal(shutdown_signal)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            if os.name == "posix":
                os.killpg(process_group, signal.SIGKILL)
            else:  # pragma: no cover - exercised on Windows CI
                process.kill()
            process.communicate(timeout=2)

    if os.name == "posix":
        assert process.returncode == 0
        assert stderr.count("Application shutdown complete.") == 2
    else:  # Windows signal delivery may use TerminateProcess.
        assert process.returncode is not None
    output = stdout + stderr
    assert len(output.encode()) < 64 * 1024
    assert "Traceback" not in output
    _assert_ports_released(_HOST, mcp_port, client_port)
    _wait_for_process_group_exit(process_group)


@pytest.mark.parametrize("cancel_twice", [False, True], ids=["single", "double"])
def test_first_cancellation_drains_fully_second_cancels_the_drainage(
    cancel_twice: bool,
) -> None:
    """The first cancellation drains both listeners; a second interrupts the
    unshielded gather, cancelling its children without leaving survivors."""

    async def scenario() -> None:
        baseline_tasks = set(asyncio.all_tasks())
        allow_drain = asyncio.Event()
        events: list[str] = []

        class FakeServer:
            def __init__(self, name: str) -> None:
                self.name = name
                self.started = asyncio.Event()
                self.draining = asyncio.Event()
                self.exit_requested = asyncio.Event()

            @property
            def should_exit(self) -> bool:
                return self.exit_requested.is_set()

            @should_exit.setter
            def should_exit(self, value: bool) -> None:
                if value:
                    self.exit_requested.set()
                else:
                    self.exit_requested.clear()

            async def serve(self, sockets: list[socket.socket] | None = None) -> None:
                self.started.set()
                await self.exit_requested.wait()
                self.draining.set()
                try:
                    await allow_drain.wait()
                    events.append(f"{self.name}:drained")
                except asyncio.CancelledError:
                    events.append(f"{self.name}:drain-interrupted")
                    raise

        # Each parametrized scenario owns fresh servers, events and sockets.
        servers = (FakeServer("mcp"), FakeServer("client"))
        with _listener() as mcp_socket, _listener() as client_socket:
            pair_task = asyncio.create_task(
                server_module._serve_uvicorn_pair(
                    servers[0],  # type: ignore[arg-type]
                    servers[1],  # type: ignore[arg-type]
                    mcp_socket,
                    client_socket,
                )
            )
            try:
                async with asyncio.timeout(2):
                    for server in servers:
                        await server.started.wait()
                assert all(not server.should_exit for server in servers)
                assert not pair_task.done()
                assert pair_task.cancel()

                async with asyncio.timeout(2):
                    for server in servers:
                        await server.draining.wait()
                assert events == []
                assert not pair_task.done()
                if cancel_twice:
                    assert pair_task.cancel()
                else:
                    allow_drain.set()

                # wait() bounds completion without injecting another cancellation.
                _, pending = await asyncio.wait({pair_task}, timeout=2)
                assert not pending
                with pytest.raises(asyncio.CancelledError):
                    await pair_task
                assert pair_task.cancelled()
                outcome = "drain-interrupted" if cancel_twice else "drained"
                assert sorted(events) == sorted(
                    [f"mcp:{outcome}", f"client:{outcome}"]
                )
                assert not (set(asyncio.all_tasks()) - baseline_tasks)
            finally:
                # Also release/cancel children if a readiness or behavior assertion
                # fails, so asyncio.run cannot hang waiting for a blocked fake.
                allow_drain.set()
                for server in servers:
                    server.should_exit = True
                remaining = set(asyncio.all_tasks()) - baseline_tasks
                for task in remaining:
                    task.cancel()
                if remaining:
                    done, pending = await asyncio.wait(remaining, timeout=2)
                    for task in done:
                        if not task.cancelled():
                            task.exception()
                    assert not pending, "listener tasks survived test cleanup"

    asyncio.run(scenario())


@pytest.mark.integration
def test_listener_failure_cancels_inflight_call_and_cleans_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class ListenerFailure(RuntimeError):
        pass

    async def scenario() -> None:
        mcp_port, client_port = _candidate_ports()
        capability = _ProbeProvider(block=True)
        original_client_server = server_module._SignalFreeUvicornServer
        original_pair = server_module._serve_uvicorn_pair
        runtime_servers: list[uvicorn.Server] = []
        inner_client_tasks: list[asyncio.Task[None]] = []

        class FailingClientServer(original_client_server):
            async def serve(
                self, sockets: list[socket.socket] | None = None
            ) -> None:
                inner = asyncio.create_task(
                    super().serve(sockets=sockets), name="test-real-client-listener"
                )
                trigger = asyncio.create_task(
                    capability.started.wait(), name="test-listener-failure-trigger"
                )
                inner_client_tasks.append(inner)
                try:
                    done, _ = await asyncio.wait(
                        {inner, trigger}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if inner in done:
                        trigger.cancel()
                        await asyncio.gather(trigger, return_exceptions=True)
                        await inner
                        return
                    self.should_exit = True
                    try:
                        await asyncio.wait_for(inner, timeout=2)
                    except TimeoutError:
                        self.force_exit = True
                        inner.cancel()
                        await asyncio.gather(inner, return_exceptions=True)
                    raise ListenerFailure("induced client listener failure")
                finally:
                    if not trigger.done():
                        trigger.cancel()
                    if not inner.done():
                        inner.cancel()
                    await asyncio.gather(inner, trigger, return_exceptions=True)

        async def capture_pair(
            mcp_server: uvicorn.Server,
            client_server: uvicorn.Server,
            mcp_socket: socket.socket,
            client_socket: socket.socket,
        ) -> None:
            runtime_servers.extend((mcp_server, client_server))
            await original_pair(mcp_server, client_server, mcp_socket, client_socket)

        monkeypatch.setattr(
            server_module, "_SignalFreeUvicornServer", FailingClientServer
        )
        monkeypatch.setattr(server_module, "_serve_uvicorn_pair", capture_pair)

        baseline_tasks = set(asyncio.all_tasks())
        runtime_task = asyncio.create_task(
            server_module._serve_relay(
                _settings(mcp_port=mcp_port, client_port=client_port)
            ),
            name="test-failing-dual-listener-runtime",
        )
        client = RelayClient(
            ClientSettings(
                server_url=f"ws://{_HOST}:{client_port}/ws",
                client_id="client-a",
                client_token='client-secret-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            catalog=_probe_catalog(capability),
        )
        client_task = asyncio.create_task(client.run(), name="test-blocked-relay-client")

        async def call_status() -> object:
            headers = {"Authorization": 'Bearer mcp-secret-synthetic-credential-0000000000000000'}
            async with httpx2.AsyncClient(headers=headers) as http:
                async with streamable_http_client(
                    f"http://{_HOST}:{mcp_port}/mcp",
                    http_client=http,
                    terminate_on_close=True,
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        return await session.call_tool("probe_status", {})

        call_task: asyncio.Task[object] | None = None
        outcome: object | BaseException | None = None
        try:
            await _wait_until(
                lambda: bool(runtime_servers)
                and runtime_servers[0].started
                and runtime_servers[1].started
                and runtime_servers[0].config.app.state.registry.catalog_tool(
                    "probe_status"
                )
                is not None
            )
            call_task = asyncio.create_task(
                call_status(), name="test-inflight-mcp-call"
            )
            try:
                outcome = await asyncio.wait_for(call_task, timeout=5)
            except (Exception, asyncio.CancelledError) as exc:
                outcome = exc
            with pytest.raises(ListenerFailure, match="induced client listener failure"):
                await asyncio.wait_for(runtime_task, timeout=5)
            await asyncio.wait_for(capability.cancelled.wait(), timeout=2)
        finally:
            if call_task is not None and not call_task.done():
                call_task.cancel()
                await asyncio.gather(call_task, return_exceptions=True)
            client.stop()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(client_task, timeout=2)
            if not runtime_task.done():
                runtime_task.cancel()
            await asyncio.gather(runtime_task, return_exceptions=True)

        assert outcome is not None
        assert not isinstance(outcome, TimeoutError)
        assert isinstance(outcome, BaseException) or getattr(outcome, "is_error") is True
        assert call_task is not None and call_task.done()
        assert capability.calls
        assert capability.cancelled.is_set()
        assert all(task.done() for task in inner_client_tasks)
        assert all(not instance.server_state.connections for instance in runtime_servers)
        assert all(not instance.server_state.tasks for instance in runtime_servers)
        assert client_task.done()
        assert runtime_task.done()
        _assert_ports_released(_HOST, mcp_port, client_port)
        await _wait_until(
            lambda: not [
                task
                for task in asyncio.all_tasks()
                if task not in baseline_tasks and not task.done()
            ],
            timeout=2,
        )

    asyncio.run(scenario())


def test_identical_listener_addresses_are_rejected_consistently(tmp_path: Path) -> None:
    """load_server_runtime (config path) and RelaySettings (env path) agree."""
    port = _candidate_ports(count=1)[0]
    config_path = tmp_path / ".mcp-relay" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("", encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)

    env = {
        "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
        "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
        "RELAY_SERVER_MCP_HOST": _HOST,
        "RELAY_SERVER_MCP_PORT": str(port),
        "RELAY_SERVER_CLIENT_HOST": _HOST,
        "RELAY_SERVER_CLIENT_PORT": str(port),
    }
    with pytest.raises(ConfigError, match="invalid relay server configuration"):
        load_server_runtime(config_path, env=env)
    with pytest.raises(ValueError, match="invalid relay server configuration"):
        RelaySettings.from_environment(env)
