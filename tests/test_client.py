from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest

from mcp_relay.client import (
    HEARTBEAT_INTERVAL_SECONDS,
    RECONNECT_MIN_SECONDS,
    ClientSettings,
    ConfigurationError,
    RelayClient,
    _private_local_path,
    _read_client_id_file,
    _run_with_signal_handlers,
    check_connection,
    main,
    safe_server_target,
)
from mcp_relay.output_models import ProviderToolResult
from mcp_relay.provider_tools import ProviderToolDescriptor
from mcp_relay.providers.base import (
    ProviderResultTooLargeError,
    ProviderTimeoutError,
    ProviderToolError,
)


def _canonical_client_environment(
    tmp_path: Path, *, url: str = "wss://relay.example.test/ws"
) -> tuple[dict[str, str], Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    token_file = tmp_path / ".env"
    token_file.write_text('RELAY_CLIENT_TOKEN=canonical-client-secret-synthetic-credential-0000000000000000\n', encoding="utf-8")
    token_file.chmod(0o600)
    return (
        {
            "RELAY_URL": url,
            "RELAY_CLIENT_TOKEN": 'canonical-client-secret-synthetic-credential-0000000000000000',
            "RELAY_CLIENT_WORKSPACE": str(workspace),
        },
        token_file,
    )


def _load_canonical_client_settings(environment: dict[str, str]) -> ClientSettings:
    try:
        return ClientSettings.from_environment(environment)
    except ConfigurationError as exc:
        pytest.fail(f"canonical Client environment was rejected: {exc}")
    raise AssertionError("unreachable")




def test_windows_identity_state_does_not_require_posix_mode_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / ".mcp-relay"
    state_dir.mkdir()
    state_dir.chmod(0o755)
    identity_path = state_dir / "client-id"
    identity_path.write_text("windows-client\n", encoding="utf-8")
    identity_path.chmod(0o644)

    monkeypatch.setattr(os, "name", "nt")

    assert _private_local_path(state_dir, directory=True).st_mode
    assert _read_client_id_file(identity_path) == "windows-client"


def test_client_settings_validate_url_workspace_and_mask_secret(tmp_path: Path) -> None:
    settings = ClientSettings(
        server_url="ws://127.0.0.1:8765/ws",
        client_id="client-a",
        client_token='secret-token-synthetic-credential-0000000000000000',
        workspace=tmp_path,
    )
    assert settings.workspace == tmp_path.resolve()
    assert 'secret-token-synthetic-credential-0000000000000000' not in repr(settings)
    with pytest.raises(ConfigurationError):
        ClientSettings(
            server_url="http://relay.example/ws",
            client_id="client-a",
            client_token='secret-token-synthetic-credential-0000000000000000',
            workspace=tmp_path,
        )


def test_client_configuration_debug_diagnostics_redact_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    with pytest.raises(ConfigurationError):
        ClientSettings(
            server_url="ws://127.0.0.1/ws",
            client_id="client-a",
            client_token='secret-token-synthetic-credential-0000000000000000',
            workspace=tmp_path,
            unexpected="secret-value",
        )

    diagnostic = capsys.readouterr().err
    assert "client configuration rejected fields: unexpected" in diagnostic
    assert 'secret-token-synthetic-credential-0000000000000000' not in diagnostic
    assert "secret-value" not in diagnostic


def test_canonical_client_environment_uses_token_and_redacts_secret(
    tmp_path: Path,
) -> None:
    environment, token_file = _canonical_client_environment(tmp_path)
    settings = _load_canonical_client_settings(environment)

    assert settings.server_url == environment["RELAY_URL"]
    assert settings.workspace == (tmp_path / "workspace").resolve()
    assert settings.client_token.get_secret_value() == 'canonical-client-secret-synthetic-credential-0000000000000000'
    assert 'canonical-client-secret-synthetic-credential-0000000000000000' not in repr(settings)
    assert token_file.is_file()
    assert not token_file.is_symlink()
    if os.name != "nt":
        assert token_file.stat().st_mode & 0o777 == 0o600


def test_generated_client_id_is_stable_across_configuration_reloads(tmp_path: Path) -> None:
    environment, _ = _canonical_client_environment(tmp_path)
    first = _load_canonical_client_settings(environment)
    second = _load_canonical_client_settings(environment)

    first_id = getattr(first, "client_id", None)
    second_id = getattr(second, "client_id", None)
    assert isinstance(first_id, str) and first_id
    assert first_id == second_id


def test_existing_client_id_is_preserved_instead_of_silently_replaced(
    tmp_path: Path,
) -> None:
    environment, _ = _canonical_client_environment(tmp_path)
    environment["RELAY_CLIENT_ID"] = "provisioned-client-1"

    settings = _load_canonical_client_settings(environment)

    assert getattr(settings, "client_id", None) == "provisioned-client-1"


@pytest.mark.parametrize(
    "url",
    [
        "ws://127.0.0.1:8000/ws",
        "ws://relay-server:8000/ws",
        "wss://relay.example.com/ws",
    ],
)
def test_client_accepts_syntactically_valid_ws_and_wss_urls(
    tmp_path: Path, url: str
) -> None:
    environment, _ = _canonical_client_environment(tmp_path, url=url)
    settings = _load_canonical_client_settings(environment)
    assert settings.server_url == url


def test_unknown_transport_environment_is_rejected_for_a_valid_ws_url(
    tmp_path: Path,
) -> None:
    environment, _ = _canonical_client_environment(
        tmp_path, url="ws://192.168.1.20:8000/ws"
    )
    environment["RELAY_" + "TRANSPORT_" + "POLICY"] = "legacy"
    with pytest.raises(ValueError, match="unknown Relay environment variable"):
        _load_canonical_client_settings(environment)


@pytest.mark.parametrize(
    "url",
    [
        "http://relay.example.com",
        "ws:///ws",
        "ws://user:password@relay.example.com/ws",
        "ws://relay.example.com/ws#fragment",
        "ws://relay.example.com:not-a-port/ws",
    ],
)
def test_client_rejects_structurally_invalid_relay_urls(
    tmp_path: Path, url: str
) -> None:
    environment, _ = _canonical_client_environment(tmp_path, url=url)
    with pytest.raises(ConfigurationError):
        ClientSettings.from_environment(environment)


def test_client_settings_has_no_transport_policy_field(tmp_path: Path) -> None:
    field_name = "allow_" + "insecure_ws"
    assert field_name not in ClientSettings.model_fields
    with pytest.raises(ConfigurationError):
        ClientSettings(
            server_url="ws://192.168.1.20:8000/ws",
            client_id="client-a",
            client_token='secret-token-synthetic-credential-0000000000000000',
            workspace=tmp_path,
            **{field_name: True},
        )


def test_configuration_failures_never_echo_client_token(tmp_path: Path) -> None:
    secret = 'CLIENT_TOKEN_SENTINEL-synthetic-credential-0000000000000000'
    invalid_values = [
        {"server_url": "http://relay.example/ws"},
        {"client_id": "bad space"},
        {"client_token": ""},
        {"workspace": tmp_path / "missing"},
        {"reconnect_min_seconds": 2, "reconnect_max_seconds": 1},
        {"stdout_limit": 48 * 1024, "stderr_limit": 48 * 1024},
    ]
    base = {
        "server_url": "ws://localhost/ws",
        "client_id": "client-a",
        "client_token": secret,
        "workspace": tmp_path,
    }
    for invalid in invalid_values:
        with pytest.raises(ConfigurationError) as error:
            ClientSettings(**(base | invalid))
        assert str(error.value) == "invalid client configuration"
        assert secret not in str(error.value)
    with pytest.raises(ConfigurationError) as error:
        ClientSettings.model_validate(base | {"client_token": ""})
    assert secret not in str(error.value)


def test_environment_and_cli_configuration_errors_never_echo_client_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = 'CLIENT_TOKEN_SENTINEL-synthetic-credential-0000000000000000'
    env = {
        "MCP_RELAY_SERVER_URL": "ws://relay.example/ws",
        "MCP_RELAY_CLIENT_ID": "client-a",
        "MCP_RELAY_WORKSPACE": str(tmp_path),
        "MCP_RELAY_CLIENT_TOKEN": secret,
    }
    with pytest.raises(ConfigurationError) as error:
        ClientSettings.from_environment(env)
    assert secret not in str(error.value)

    monkeypatch.setattr("mcp_relay.client.os.environ", env)
    monkeypatch.setattr(sys, "argv", ["mcp-relay-client"])
    with pytest.raises(SystemExit):
        main()
    assert secret not in capsys.readouterr().err


class _Connection(AbstractAsyncContextManager["_Socket"]):
    def __init__(self, socket: _Socket | None = None, error: Exception | None = None) -> None:
        self.socket = socket
        self.error = error

    async def __aenter__(self) -> _Socket:
        if self.error is not None:
            raise self.error
        assert self.socket is not None
        return self.socket

    async def __aexit__(self, *_: object) -> None:
        return None


def test_protocol_incompatible_close_stops_automatic_reconnection(
    tmp_path: Path,
) -> None:
    """A 1002 protocol_incompatible close is permanent: no retry loop."""

    connections = iter(
        [
            _Connection(
                _ContractRejectingSocket(
                    [json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 2})]
                )
            ),
            _Connection(error=AssertionError("must not reconnect")),
        ]
    )
    client = RelayClient(
        ClientSettings(
            server_url="ws://localhost/ws",
            client_id="d",
            client_token='client-synthetic-credential-0000000000000000',
            workspace=tmp_path,
        ),
        connector=lambda *a, **k: next(connections),
    )

    async def no_delay(_: float) -> None:
        client.stop()

    client._sleep_or_stop = no_delay  # type: ignore[method-assign]
    asyncio.run(client.run())
    # The single connection attempt ended with the permanent close; the
    # client stopped instead of retrying (a second attempt would raise the
    # AssertionError planted in the second connector answer).


def test_backoff_does_not_reset_after_registered_session_disconnects(tmp_path: Path) -> None:
    class DisconnectingSocket(_Socket):
        async def recv(self) -> str:
            if not self.inbound.empty():
                return await super().recv()
            raise ConnectionError("disconnected")

    async def scenario() -> None:
        connections = iter(
            [
                _Connection(error=ConnectionError("before handshake")),
                _Connection(error=ConnectionError("before handshake")),
                _Connection(
                    DisconnectingSocket(
                        [json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 2})]
                    )
                ),
            ]
        )
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            connector=lambda *_, **__: next(connections),
        )
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            if delay == HEARTBEAT_INTERVAL_SECONDS:
                await client._stop_event.wait()
                return
            delays.append(delay)
            if len(delays) == 3:
                client.stop()

        client._sleep_or_stop = record_sleep  # type: ignore[method-assign]
        await client.run()
        assert delays == [
            RECONNECT_MIN_SECONDS,
            RECONNECT_MIN_SECONDS * 2,
            RECONNECT_MIN_SECONDS * 4,
        ]

    asyncio.run(scenario())


def test_backoff_resets_only_after_stable_registered_session(tmp_path: Path) -> None:
    class DisconnectingSocket(_Socket):
        async def recv(self) -> str:
            if not self.inbound.empty():
                return await super().recv()
            clock[0] += 30
            raise ConnectionError("disconnected")

    async def scenario() -> None:
        connections = iter(
            [
                _Connection(error=ConnectionError("before handshake")),
                _Connection(DisconnectingSocket([json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 2})])),
            ]
        )
        client = RelayClient(
            ClientSettings(server_url="ws://localhost/ws", client_id="d", client_token='client-synthetic-credential-0000000000000000', workspace=tmp_path),
            connector=lambda *_, **__: next(connections),
            monotonic=lambda: clock[0],
        )
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            if delay == HEARTBEAT_INTERVAL_SECONDS:
                await client._stop_event.wait()
                return
            delays.append(delay)
            if len(delays) == 2:
                client.stop()

        client._sleep_or_stop = record_sleep  # type: ignore[method-assign]
        await client.run()
        assert delays == [RECONNECT_MIN_SECONDS, RECONNECT_MIN_SECONDS]

    clock = [0.0]
    asyncio.run(scenario())


def test_websocket_connections_send_bearer_only_in_handshake_options(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        seen: dict[str, object] = {}

        def connector(*_: object, **kwargs: object) -> _Connection:
            seen.update(kwargs)
            raise ConnectionError("unused")

        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-handshake-secret-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            connector=connector,
        )

        async def stop_after_retry(_: float) -> None:
            client.stop()

        client._sleep_or_stop = stop_after_retry  # type: ignore[method-assign]
        await client.run()
        assert seen["additional_headers"] == {
            "Authorization": 'Bearer client-handshake-secret-synthetic-credential-0000000000000000'
        }
        assert seen["proxy"] is None
        assert 'client-handshake-secret-synthetic-credential-0000000000000000' not in json.dumps(
            {key: value for key, value in seen.items() if key != "additional_headers"}
        )

    asyncio.run(scenario())


def test_websocket_connections_disable_proxy_even_with_hostile_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        seen: dict[str, object] = {}

        def connector(*_: object, **kwargs: object) -> _Connection:
            seen.update(kwargs)
            client.stop()
            return _Connection(error=ConnectionError("unused"))

        client = RelayClient(ClientSettings(server_url="ws://localhost/ws", client_id="d", client_token='client-synthetic-credential-0000000000000000', workspace=tmp_path), connector=connector)
        await client.run()
        assert seen["proxy"] is None

    monkeypatch.setenv("HTTPS_PROXY", "http://hostile.invalid:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://hostile.invalid:8080")
    asyncio.run(scenario())


def test_stopped_client_does_not_sleep(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            )
        )
        client.stop()

        async def unexpected_sleep(_: float) -> None:
            raise AssertionError("a stopped client must not sleep")

        client._sleep_or_stop = unexpected_sleep  # type: ignore[method-assign]
        await client.run()

    asyncio.run(scenario())


class _Socket:
    def __init__(self, inbound: list[str]) -> None:
        self.inbound = asyncio.Queue()
        for item in inbound:
            self.inbound.put_nowait(item)
        self.sent: list[dict[str, object]] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        return await self.inbound.get()


class _ContractRejectingSocket(_Socket):
    """A socket whose recv raises the 1002 protocol_incompatible close."""

    async def recv(self) -> str:
        if not self.inbound.empty():
            return await super().recv()
        import websockets

        raise websockets.exceptions.ConnectionClosed(
            websockets.frames.Close(1002, "protocol_incompatible"), None
        )


def test_safe_server_target_drops_userinfo_path_and_query() -> None:
    assert safe_server_target(
        "wss://user:secret@relay.example.test:8443/ws?token=secret"
    ) == "wss://relay.example.test:8443"


def test_authenticated_connection_check_reuses_register_without_token(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        socket = _Socket(
            [json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 2})]
        )
        observed_options: dict[str, object] = {}

        class Connection(AbstractAsyncContextManager[_Socket]):
            async def __aenter__(self) -> _Socket:
                return socket

            async def __aexit__(self, *_: object) -> None:
                return None

        def connector(*_: object, **kwargs: object) -> Connection:
            observed_options.update(kwargs)
            return Connection()

        await check_connection(
            ClientSettings(
                server_url="ws://localhost:8765/ws",
                client_id="d",
                client_token='secret-token-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            connector=connector,
        )
        assert socket.sent == [{"version": 1, "type": "register", "client_id": "d", "relay_contract": 2}]
        assert 'secret-token-synthetic-credential-0000000000000000' not in json.dumps(socket.sent)
        headers = observed_options.get("additional_headers")
        assert isinstance(headers, dict)
        assert headers["Authorization"] == 'Bearer secret-token-synthetic-credential-0000000000000000'

    asyncio.run(scenario())


def test_client_register_frame_contains_no_client_token(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket = _Socket(
            [json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 2})]
        )
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='secret-token-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            )
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if len(socket.sent) >= 2:
                break
            await asyncio.sleep(0)
        client.stop()
        await task

        register = socket.sent[0]
        assert register["type"] == "register"
        assert "token" not in register
        assert "client_token" not in register
        assert 'secret-token-synthetic-credential-0000000000000000' not in json.dumps(register)

    asyncio.run(scenario())




def test_signal_handlers_stop_client_and_wait_for_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Loop:
        handlers: list[object] = []

        def add_signal_handler(self, signum: object, callback: object) -> None:
            self.handlers.append((signum, callback))

    async def scenario() -> None:
        client = RelayClient(ClientSettings(server_url="ws://localhost/ws", client_id="d", client_token='client-synthetic-credential-0000000000000000', workspace=tmp_path))
        completed = False

        async def run() -> None:
            nonlocal completed
            callback = Loop.handlers[0][1]
            callback()  # type: ignore[operator]
            assert client._stop_event.is_set()
            completed = True

        client.run = run  # type: ignore[method-assign]
        await _run_with_signal_handlers(client)
        assert completed

    Loop.handlers = []
    monkeypatch.setattr("mcp_relay.client.asyncio.get_running_loop", lambda: Loop())
    asyncio.run(scenario())


def test_signal_handlers_are_optional_on_windows_event_loops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class WindowsLoop:
        def add_signal_handler(self, signum: object, callback: object) -> None:
            raise NotImplementedError

    async def scenario() -> None:
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            )
        )
        completed = False

        async def run() -> None:
            nonlocal completed
            completed = True

        client.run = run  # type: ignore[method-assign]
        await _run_with_signal_handlers(client)
        assert completed

    monkeypatch.setattr(
        "mcp_relay.client.asyncio.get_running_loop", lambda: WindowsLoop()
    )
    asyncio.run(scenario())


def _first_capabilities_frame(socket: object) -> dict[str, object] | None:
    for message in socket.sent:  # type: ignore[attr-defined]
        if message.get("type") == "capabilities":
            return message
    return None




_COMMAND_DESCRIPTOR = ProviderToolDescriptor(
    provider_name="sample",
    tool_name="click",
    description="click",
    input_schema={"type": "object", "additionalProperties": False},
)






_REGISTERED = json.dumps(
    {
        "version": 1,
        "type": "registered",
        "client_id": "d",
        "server_version": "0.1.0",
        "relay_contract": 2,
    }
)







# ---------------------------------------------------------------------------
# Session behavior of the dynamic surface
# ---------------------------------------------------------------------------

from mcp_relay.control import Control  # noqa: E402
from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog  # noqa: E402
from mcp_relay.protocol import RELAY_CONTRACT  # noqa: E402


class _Tool:
    """A route provider for the ``sample`` alias with scripted behavior."""

    def __init__(self, behavior: object = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.behavior = behavior
        self.release = asyncio.Event()

    async def call_tool(self, name: str, arguments: dict[str, object]) -> ProviderToolResult:
        self.calls.append((name, dict(arguments)))
        if self.behavior == "block":
            while True:
                try:
                    await self.release.wait()
                    break
                except asyncio.CancelledError:
                    # Swallow cancellation like a misbehaving provider.
                    self.behavior = "swallowed"
                    await asyncio.sleep(0.05)
                    break
        if isinstance(self.behavior, Exception):
            raise self.behavior
        if isinstance(self.behavior, ProviderToolResult):
            return self.behavior
        return ProviderToolResult(
            content=[{"type": "text", "text": "ok"}],
            structuredContent={"echo": arguments.get("text")},
        )


def _session_client(tmp_path: Path, provider: _Tool | None = None, *, admin: bool = False) -> RelayClient:
    catalog = ClientCatalog()
    if provider is not None:
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
                        tool_name="click",
                        description="click",
                        input_schema={"type": "object"},
                    ),
                ),
                provider=provider,
            )
        )
    control = Control(hub=None, catalog=catalog, client_version="0.2.0", admin_enabled=admin)
    return RelayClient(
        ClientSettings(
            server_url="ws://localhost/ws",
            client_id="d",
            client_token="client-synthetic-credential-0000000000000000",
            workspace=tmp_path,
        ),
        control=control,
        catalog=catalog,
    )


def _registered(contract: int = RELAY_CONTRACT, server_version: str = "0.1.0") -> str:
    return json.dumps(
        {
            "version": 1,
            "type": "registered",
            "client_id": "d",
            "server_version": server_version,
            "relay_contract": contract,
        }
    )


def _invoke(request_id: str, tool_name: str = "mcp.command", **arguments: object) -> str:
    if tool_name == "mcp.command" and not arguments:
        arguments = {"alias": "sample", "tool": "click", "arguments": {"text": "hi"}}
    return json.dumps(
        {
            "version": 2,
            "type": "invoke",
            "request_id": request_id,
            "tool_name": tool_name,
            "arguments": arguments,
        }
    )


async def _until(socket: _Socket, predicate: Callable[[list[dict[str, object]]], bool]) -> None:
    for _ in range(500):
        if predicate(socket.sent):
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"frames never matched: {socket.sent}")


def _frames(socket: _Socket, frame_type: str) -> list[dict[str, object]]:
    return [frame for frame in socket.sent if frame.get("type") == frame_type]


async def _session(client: RelayClient, socket: _Socket) -> asyncio.Task[None]:
    task = asyncio.create_task(client.run_session(socket))
    await _until(socket, lambda sent: any(f.get("type") == "catalog" for f in sent))
    return task


async def _stop(client: RelayClient, task: asyncio.Task[None]) -> None:
    client.stop()
    await asyncio.wait_for(task, timeout=5)


def test_handshake_sends_token_free_register_capabilities_then_catalog(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = _session_client(tmp_path, _Tool(), admin=True)
        socket = _Socket([_registered()])
        task = await _session(client, socket)
        await _stop(client, task)
        register, capabilities, catalog = socket.sent[:3]
        assert register == {
            "version": 1,
            "type": "register",
            "client_id": "d",
            "relay_contract": RELAY_CONTRACT,
        }
        assert "synthetic-credential" not in json.dumps(socket.sent)
        assert capabilities["type"] == "capabilities"
        assert capabilities["admin"] is True
        assert capabilities["relay_contract"] == RELAY_CONTRACT
        assert catalog["type"] == "catalog"
        assert [tool["name"] for tool in catalog["tools"]] == ["sample_click"]

    asyncio.run(scenario())


def test_catalog_changes_are_pushed_once_and_duplicates_are_skipped(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = _session_client(tmp_path, _Tool())
        socket = _Socket([_registered()])
        task = await _session(client, socket)
        client.catalog_changed()
        client.catalog_changed()
        await asyncio.sleep(0.2)
        assert len(_frames(socket, "catalog")) == 1
        client.catalog.remove_alias("sample")
        client.catalog_changed()
        await _until(socket, lambda _: len(_frames(socket, "catalog")) == 2)
        assert _frames(socket, "catalog")[-1]["tools"] == []
        await _stop(client, task)

    asyncio.run(scenario())


def test_an_empty_catalog_is_not_sent_at_session_start(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = _session_client(tmp_path, _Tool())
        client.catalog.remove_alias("sample")
        socket = _Socket([_registered()])
        task = asyncio.create_task(client.run_session(socket))
        await _until(socket, lambda _: bool(_frames(socket, "capabilities")))
        await asyncio.sleep(0.2)
        assert _frames(socket, "catalog") == []
        await _stop(client, task)

    asyncio.run(scenario())


def test_mcp_command_runs_once_and_returns_the_native_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = _Tool()
        client = _session_client(tmp_path, provider)
        socket = _Socket([_registered(), _invoke("r1")])
        task = await _session(client, socket)
        await _until(socket, lambda _: bool(_frames(socket, "result")))
        await _stop(client, task)
        assert provider.calls == [("click", {"text": "hi"})]
        [result] = _frames(socket, "result")
        assert result["request_id"] == "r1"
        assert result["result"]["structuredContent"] == {"echo": "hi"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("invoke", "behavior", "code", "state"),
    [
        (_invoke("r1", alias="nope", tool="click", arguments={}), None, "alias_unknown", "not_started"),
        (_invoke("r1", alias="sample", tool="zap", arguments={}), None, "tool_unknown", "not_started"),
        (_invoke("r1", alias="sample", tool="click"), None, "invalid_arguments", "not_started"),
        (_invoke("r1"), ProviderTimeoutError("slow"), "timeout", "unknown"),
        (_invoke("r1"), ProviderToolError("boom"), "execution_failed", "unknown"),
        (_invoke("r1"), ProviderResultTooLargeError("RELAY_MAX_X: 1 < payload: 2 bytes"), "result_too_large", "unknown"),
        (_invoke("r1", "mcp.add", alias="x", entry={}), None, "permission_denied", "not_started"),
        (_invoke("r1", "mcp.nope"), None, "invalid_arguments", "not_started"),
    ],
)
def test_failures_become_closed_error_frames(
    tmp_path: Path, invoke: str, behavior: object, code: str, state: str
) -> None:
    async def scenario() -> None:
        client = _session_client(tmp_path, _Tool(behavior))
        socket = _Socket([_registered(), invoke])
        task = await _session(client, socket)
        await _until(socket, lambda _: bool(_frames(socket, "error")))
        await _stop(client, task)
        [error] = _frames(socket, "error")
        assert (error["error"]["code"], error["error"]["execution_state"]) == (code, state)

    asyncio.run(scenario())


def test_status_is_answered_as_structured_content(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = _session_client(tmp_path, _Tool())
        socket = _Socket([_registered(), _invoke("s1", "client.status")])
        task = await _session(client, socket)
        await _until(socket, lambda _: bool(_frames(socket, "result")))
        await _stop(client, task)
        result = _frames(socket, "result")[0]["result"]
        status = result["structuredContent"]
        assert status["version"] == "0.2.0"
        assert status["mcp_servers"][0]["alias"] == "sample"
        assert json.loads(result["content"][0]["text"]) == status

    asyncio.run(scenario())


def test_one_action_at_a_time_and_cancel_suppresses_the_late_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = _Tool("block")
        client = _session_client(tmp_path, provider)
        socket = _Socket([_registered(), _invoke("r1"), _invoke("r2")])
        task = await _session(client, socket)
        await _until(socket, lambda _: bool(_frames(socket, "error")))
        [busy] = _frames(socket, "error")
        assert busy["request_id"] == "r2"
        assert busy["error"]["code"] == "busy"
        socket.inbound.put_nowait(
            json.dumps({"version": 2, "type": "cancel", "request_id": "r1", "reason": "timeout"})
        )
        await asyncio.sleep(0.3)
        await _stop(client, task)
        assert provider.calls == [("click", {"text": "hi"})]
        assert _frames(socket, "result") == []
        assert [frame["request_id"] for frame in _frames(socket, "error")] == ["r2"]

    asyncio.run(scenario())


def test_contract_mismatch_in_registered_is_permanent(tmp_path: Path) -> None:
    from mcp_relay.client import ProtocolIncompatibleError

    async def scenario() -> None:
        client = _session_client(tmp_path)
        with pytest.raises(ProtocolIncompatibleError):
            await client.run_session(_Socket([_registered(contract=RELAY_CONTRACT + 1)]))

    asyncio.run(scenario())


def test_command_logs_name_only_known_identifiers_and_never_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def scenario() -> None:
        client = _session_client(tmp_path, _Tool())
        socket = _Socket(
            [
                _registered(),
                _invoke("r1", alias="sample", tool="click", arguments={"text": "SECRET_ARG"}),
                _invoke("r2", alias="SECRET_ALIAS", tool="click", arguments={}),
            ]
        )
        task = await _session(client, socket)
        await _until(socket, lambda _: bool(_frames(socket, "error")))
        await _stop(client, task)

    asyncio.run(scenario())
    output = capsys.readouterr().err
    assert "mcp.command done: request_id=r1 alias=sample tool=click isError=false" in output
    assert "mcp.command failed: request_id=r2 code=alias_unknown" in output
    assert "SECRET" not in output


def test_initial_reconciliation_runs_in_background_and_stops_on_close(tmp_path: Path) -> None:
    class SlowHub:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = False
            self.closed = False

        async def reconcile_all(self) -> None:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        def publish_catalog(self, catalog: object) -> None:
            pass

        async def aclose(self) -> None:
            self.closed = True

    async def scenario() -> None:
        hub = SlowHub()
        client = _session_client(tmp_path)
        client.hub = hub  # type: ignore[assignment]
        client.start_initial_reconciliation()
        socket = _Socket([_registered()])
        task = asyncio.create_task(client.run_session(socket))
        await _until(socket, lambda _: bool(_frames(socket, "capabilities")))
        assert hub.started.is_set()
        await _stop(client, task)
        await client.aclose()
        assert hub.cancelled and hub.closed

    asyncio.run(scenario())


def test_built_client_resolves_registry_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_relay.client import build_client
    from mcp_relay.config import init_config, mcp_entry_add

    token = "client-synthetic-credential-0000000000000000"
    config_path = tmp_path / "config.yaml"
    init_config(config_path, "client", env={"RELAY_CLIENT_TOKEN": token})
    mcp_entry_add(config_path, "reg", {"source": "io.example/ghost"}, None)
    lookups: list[tuple[str, str | None]] = []

    async def lookup(source: str, *, version: str | None = None) -> None:
        lookups.append((source, version))
        return None

    monkeypatch.setattr("mcp_relay.client.lookup_registry_server", lookup)
    client = build_client(
        ClientSettings(
            server_url="ws://localhost/ws",
            client_id="d",
            client_token=token,
            workspace=tmp_path,
        ),
        config_path=config_path,
    )

    async def scenario() -> None:
        assert client.hub is not None
        await client.hub.reconcile_all()
        assert lookups and lookups[0] == ("io.example/ghost", None)
        assert client.hub.last_error("reg")["code"] == "spawn_failed"
        await client.aclose()

    asyncio.run(scenario())
