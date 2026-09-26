from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import cast

import pytest

import mcp_relay.json_bounds as json_bounds
from mcp_relay.client import (
    HEARTBEAT_INTERVAL_SECONDS,
    RECONNECT_MIN_SECONDS,
    ClientSettings,
    ConfigurationError,
    RelayClient,
    _private_local_path,
    _read_client_id_file,
    _run_client,
    _run_with_signal_handlers,
    check_connection,
    main,
    safe_server_target,
)
from mcp_relay.json_bounds import MAX_TOOL_RESULT_BYTES, JsonValue
from mcp_relay.output_models import ProviderTextContent, ProviderToolResult
from mcp_relay.protocol import InvokeMessage
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


def test_client_refuses_direct_third_party_dispatch(tmp_path: Path) -> None:
    """Third-party targets go through mcp.command only, never direct routes."""
    descriptor = ProviderToolDescriptor(
        provider_name="custom",
        tool_name="echo",
        description="echo text",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )

    class Provider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Mapping[str, JsonValue]]] = []
            self.closed = False

        async def list_tools(self) -> list[ProviderToolDescriptor]:
            return [descriptor]

        async def call_tool(
            self, tool_name: str, arguments: Mapping[str, JsonValue]
        ) -> ProviderToolResult:
            self.calls.append((tool_name, arguments))
            return ProviderToolResult(
                content=[{"type": "text", "text": "ok"}],
                structuredContent={"echo": arguments["text"]},
            )

        async def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        provider = Provider()
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[],

            provider_clients={"custom": provider},
        )
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "echo",
                        "tool_name": "custom.echo",
                        "arguments": {"text": "hello"},
                    }
                ),
            ]
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if socket.sent and socket.sent[-1].get("type") == "error":
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task
        await client.aclose()
        # The provider was never contacted directly from a dynamic route.
        assert provider.calls == []
        error_frame = socket.sent[-1]
        assert error_frame["type"] == "error"
        assert error_frame["error"]["execution_state"] == "not_started"

    asyncio.run(scenario())


def test_dynamic_inventory_controls_websocket_routing(
    tmp_path: Path,
) -> None:
    """Selected catalog routes execute; unselected tools are refused.

    The old guarantee survives under the fixed facade: third-party calls are
    dispatched through the client catalog (mcp.command) and never bypass it.
    """
    descriptor = ProviderToolDescriptor(
        provider_name="sample",
        tool_name="click",
        description="click",
        input_schema={"type": "object", "additionalProperties": False},
    )

    class Provider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Mapping[str, JsonValue]]] = []
            self.closed = False

        async def list_tools(self) -> list[ProviderToolDescriptor]:
            return [descriptor]

        async def call_tool(
            self, tool_name: str, arguments: Mapping[str, JsonValue]
        ) -> ProviderToolResult:
            self.calls.append((tool_name, arguments))
            return ProviderToolResult(
                content=[], structured_content={"clicked": True}
            )

        async def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        provider = Provider()
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[],
        )
        from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog

        catalog = ClientCatalog()
        catalog.update_alias(
            AliasCatalog(
                alias="sample",
                enabled=True,
                runtime_state="running",
                transport="stdio",
                entry={"command": ["/bin/sample"]},
                last_error=None,
                catalog_available=True,
                discovery_error=None,
                descriptors=(descriptor,),
                provider=provider,
            )
        )
        client.catalog = catalog
        revision = catalog.revision
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "click",
                        "tool_name": "mcp.command",
                        "arguments": {
                            "alias": "sample",
                            "tool": "click",
                            "arguments": {},
                            "catalog_revision": revision,
                        },
                    }
                ),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "unselected",
                        "tool_name": "mcp.command",
                        "arguments": {
                            "alias": "sample",
                            "tool": "type_text",
                            "arguments": {},
                            "catalog_revision": revision,
                        },
                    }
                ),
            ]
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(200):
            if len(provider.calls) >= 1 and any(
                message.get("request_id") == "unselected"
                and message.get("type") == "error"
                for message in socket.sent
            ):
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task
        await client.aclose()

        assert provider.calls == [("click", {})]
        assert any(
            message.get("request_id") == "click"
            and message.get("type") == "result"
            for message in socket.sent
        )
        assert any(
            message.get("request_id") == "unselected"
            and message.get("type") == "error"
            and message.get("error", {}).get("code") == "tool_unknown"
            for message in socket.sent
        )

    asyncio.run(scenario())


def test_client_relays_bounded_arguments_without_schema_validation(
    tmp_path: Path,
) -> None:
    """The driver remains the sole validator.

    Arguments that do not match the declared schema but stay within transport
    bounds reach the local provider; the upstream result is returned.
    """
    descriptor = ProviderToolDescriptor(
        provider_name="custom",
        tool_name="echo",
        description="echo text",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )

    class Provider:
        def __init__(self) -> None:
            self.calls = 0
            self.seen: Mapping[str, JsonValue] | None = None

        async def list_tools(self) -> list[ProviderToolDescriptor]:
            return [descriptor]

        async def call_tool(
            self, tool_name: str, arguments: Mapping[str, JsonValue]
        ) -> ProviderToolResult:
            self.calls += 1
            self.seen = arguments
            return ProviderToolResult(content=[])

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        provider = Provider()
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[],
        )
        from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog

        catalog = ClientCatalog()
        catalog.update_alias(
            AliasCatalog(
                alias="custom",
                enabled=True,
                runtime_state="running",
                transport="stdio",
                entry={"command": ["/bin/custom"]},
                last_error=None,
                catalog_available=True,
                discovery_error=None,
                descriptors=(descriptor,),
                provider=provider,
            )
        )
        client.catalog = catalog
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "nonconforming",
                        "tool_name": "mcp.command",
                        "arguments": {
                            "alias": "custom",
                            "tool": "echo",
                            "arguments": {},
                            "catalog_revision": catalog.revision,
                        },
                    }
                ),
            ]
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if any(item["type"] == "result" for item in socket.sent):
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task
        assert provider.calls == 1
        assert provider.seen == {}
        assert socket.sent[-1]["type"] == "result"
        assert socket.sent[-1]["request_id"] == "nonconforming"

    asyncio.run(scenario())


def test_client_keeps_provider_qualified_internal_names(tmp_path: Path) -> None:
    """A lookalike tool name (``sample.ping``) never routes to internal ops.

    With dynamic routes gone, the guarantee moves to the catalog: a tool
    whose exact upstream name looks like a wire operation is still only a
    catalog entry under its alias, reachable solely via mcp.command.
    """
    from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog
    from mcp_relay.relay_tools import WIRE_OPERATION_NAMES

    async def scenario() -> None:
        lookalike = ProviderToolDescriptor(
            provider_name="custom", tool_name="sample.ping",
            description="Synthetic lookalike", input_schema={"type": "object", "additionalProperties": False},
        )
        provider = _RecordingLookalikeProvider()
        catalog = ClientCatalog()
        catalog.update_alias(
            AliasCatalog(
                alias="custom",
                enabled=True,
                runtime_state="running",
                transport="stdio",
                entry={"command": ["/bin/custom"]},
                last_error=None,
                catalog_available=True,
                discovery_error=None,
                descriptors=(lookalike,),
                provider=provider,
            )
        )
        # The exact tool name resolves under its alias; it never shadows the
        # fixed wire operations, which are a separate closed namespace.
        descriptor, route = catalog.snapshot.route("custom", "sample.ping")
        assert descriptor.name == "sample.ping"
        assert route is provider
        assert "sample.ping" not in WIRE_OPERATION_NAMES

    asyncio.run(scenario())


class _RecordingLookalikeProvider:
    async def call_tool(self, *_):  # pragma: no cover - never invoked here
        raise AssertionError("not invoked")


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


def test_client_main_starts_without_native_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = ClientSettings(server_url="ws://localhost/ws", client_id="d", client_token='client-synthetic-credential-0000000000000000', workspace=tmp_path)
    observed = []
    async def run(client):
        observed.append(client._announcement_tools)
    monkeypatch.setattr(ClientSettings, "from_environment", lambda **_: settings)
    monkeypatch.setattr("mcp_relay.client._run_with_signal_handlers", run)
    main([])
    assert observed == [()]


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


def test_client_reports_operator_lifecycle_at_info_without_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class DisconnectingSocket(_Socket):
        async def recv(self) -> str:
            if self.inbound.empty():
                raise ConnectionError("socket lost")
            return await super().recv()

    async def scenario() -> None:
        socket = DisconnectingSocket(
            [json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1})]
        )
        client = RelayClient(
            ClientSettings(
                server_url="wss://relay.example.test/ws?token=secret",
                client_id="d",
                client_token='secret-token-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            connector=lambda *_, **__: _Connection(socket),
        )

        async def stop_after_delay(delay: float) -> None:
            assert delay == RECONNECT_MIN_SECONDS
            client.stop()

        client._sleep_or_stop = stop_after_delay  # type: ignore[method-assign]
        await client.run()

    asyncio.run(scenario())
    output = capsys.readouterr().err
    for phrase in (
        "connection attempt",
        "WebSocket connection established",
        "authenticated registration succeeded",
        "capabilities announced",
        "Relay disconnected; reconnecting",
        f"retrying in {RECONNECT_MIN_SECONDS:g}s",
    ):
        assert phrase in output
    assert 'secret-token-synthetic-credential-0000000000000000' not in output
    assert "token=secret" not in output
    assert "wss://relay.example.test" in output


def test_client_reports_executed_tool_at_info_without_request_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def scenario() -> None:
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "sensitive-request-id",
                        "tool_name": "sample.ping",
                        "arguments": {},
                    }
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
            capabilities=[_Capability("sample.ping", result={"pong": True})],
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if any(message.get("type") == "result" for message in socket.sent):
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task

    asyncio.run(scenario())
    output = capsys.readouterr().err
    assert "[INFO] Executing tool: sample.ping" in output
    assert "sensitive-request-id" not in output


def test_client_does_not_log_tool_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Arguments are relayed within bounds; only the tool name is logged,
    never the argument payload."""
    async def scenario() -> None:
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "rejected-request",
                        "tool_name": "sample.ping",
                        "arguments": {"secret": "rejected-secret"},
                    }
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
            capabilities=[_Capability("sample.ping", result={"pong": True})],
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if any(message.get("type") == "result" for message in socket.sent):
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task

    asyncio.run(scenario())
    output = capsys.readouterr().err
    assert "[INFO] Executing tool: sample.ping" in output
    assert "rejected-request" not in output
    assert "rejected-secret" not in output


def test_client_logs_dynamic_tool_after_validation_without_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    descriptor = ProviderToolDescriptor(
        provider_name="custom",
        tool_name="echo",
        description="echo",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )

    class Provider:
        async def list_tools(self) -> list[ProviderToolDescriptor]:
            return [descriptor]

        async def call_tool(
            self, tool_name: str, arguments: Mapping[str, JsonValue]
        ) -> ProviderToolResult:
            assert tool_name == "echo"
            assert arguments == {"value": "dynamic-secret"}
            return ProviderToolResult(content=[], structuredContent={"ok": True})

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        provider = Provider()
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[],
        )
        from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog

        catalog = ClientCatalog()
        catalog.update_alias(
            AliasCatalog(
                alias="custom",
                enabled=True,
                runtime_state="running",
                transport="stdio",
                entry={"command": ["/bin/custom"]},
                last_error=None,
                catalog_available=True,
                discovery_error=None,
                descriptors=(descriptor,),
                provider=provider,
            )
        )
        client.catalog = catalog
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "dynamic-sensitive-request",
                        "tool_name": "mcp.command",
                        "arguments": {
                            "alias": "custom",
                            "tool": "echo",
                            "arguments": {"value": "dynamic-secret"},
                            "catalog_revision": catalog.revision,
                        },
                    }
                ),
            ]
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if any(message.get("type") == "result" for message in socket.sent):
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task
        await client.aclose()

    asyncio.run(scenario())
    output = capsys.readouterr().err
    # The precise done event carries validated identifiers only.
    assert (
        "[INFO] mcp.command done: request_id=dynamic-sensitive-request "
        "alias=custom tool=echo isError=false"
    ) in output
    assert "dynamic-secret" not in output
    assert "Executing tool: mcp.command" not in output


def test_protocol_incompatible_close_stops_automatic_reconnection(
    tmp_path: Path,
) -> None:
    """A 1002 protocol_incompatible close is permanent: no retry loop."""

    connections = iter(
        [
            _Connection(
                _ContractRejectingSocket(
                    [json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1})]
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
                        [json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1})]
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
                _Connection(DisconnectingSocket([json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1})])),
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


def test_client_reregisters_and_invokes_after_socket_loss(
    tmp_path: Path,
) -> None:
    class DisconnectingSocket(_Socket):
        async def recv(self) -> str:
            if self.inbound.empty():
                raise ConnectionError("socket lost")
            return await super().recv()

    async def scenario() -> None:
        first = DisconnectingSocket(
            [json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1})]
        )
        second = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "after-reconnect",
                        "tool_name": "sample.ping",
                        "arguments": {},
                    }
                ),
            ]
        )
        connections = iter([_Connection(first), _Connection(second)])
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[_Capability("sample.ping", result={"pong": True})],
            connector=lambda *_, **__: next(connections),
        )

        async def no_delay(_: float) -> None:
            await asyncio.sleep(0)

        client._sleep_or_stop = no_delay  # type: ignore[method-assign]
        task = asyncio.create_task(client.run())
        for _ in range(100):
            if any(
                message.get("request_id") == "after-reconnect"
                for message in second.sent
            ):
                break
            await asyncio.sleep(0.001)
        client.stop()
        await asyncio.wait_for(task, timeout=1)

        assert [message["type"] for message in first.sent][:2] == [
            "register",
            "capabilities",
        ]
        assert [message["type"] for message in second.sent][:2] == [
            "register",
            "capabilities",
        ]
        results = [
            message
            for message in second.sent
            if message.get("request_id") == "after-reconnect"
        ]
        assert len(results) == 1
        assert results[0]["type"] == "result"
        assert results[0]["result"]["structuredContent"] == {"pong": True}
        assert results[0]["result"]["isError"] is False

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
            [json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1})]
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
        assert socket.sent == [{"version": 1, "type": "register", "client_id": "d", "relay_contract": 1}]
        assert 'secret-token-synthetic-credential-0000000000000000' not in json.dumps(socket.sent)
        headers = observed_options.get("additional_headers")
        assert isinstance(headers, dict)
        assert headers["Authorization"] == 'Bearer secret-token-synthetic-credential-0000000000000000'

    asyncio.run(scenario())


def test_client_register_frame_contains_no_client_token(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket = _Socket(
            [json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1})]
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


class _Capability:
    def __init__(
        self,
        name: str,
        *,
        result: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.tools = frozenset({name})
        self.result = result or {"capability": name}
        self.error = error
        self.invocations: list[InvokeMessage] = []
        self.closed = 0
        self.unavailable = asyncio.Event()

    async def start(self) -> None:
        return None

    async def list_tools(self) -> list[ProviderToolDescriptor]:
        return [
            ProviderToolDescriptor(
                provider_name=name.split(".", 1)[0],
                tool_name=name.split(".", 1)[1],
                description="Synthetic generic capability",
                input_schema={
                    "type": "object",
                    "properties": ({"command_id": {"enum": ["pwd"]}} if name.endswith(".inspect") else {}),
                    "additionalProperties": False,
                },
            ) for name in sorted(self.tools)
        ]

    async def wait_unavailable(self) -> None:
        await self.unavailable.wait()

    async def invoke(
        self, message: InvokeMessage
    ) -> dict[str, object]:
        self.invocations.append(message)
        if self.error is not None:
            raise self.error
        return self.result

    async def aclose(self) -> None:
        self.closed += 1


def test_capability_start_failure_never_opens_websocket_or_advertises(tmp_path: Path) -> None:
    class FailingCapability(_Capability):
        async def start(self) -> None:
            raise RuntimeError("not ready")

    async def scenario() -> None:
        opened = 0

        def connector(*_: object, **__: object) -> _Connection:
            nonlocal opened
            opened += 1
            return _Connection(_Socket([]))

        capability = FailingCapability("sample.ping")
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[capability],
            connector=connector,
        )
        async def stop_after_retry(_: float) -> None:
            client.stop()
        client._sleep_or_stop = stop_after_retry  # type: ignore[method-assign]
        await client.run()
        assert opened == 0

    asyncio.run(scenario())


def test_unique_multi_tool_capability_closes_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        capability = _Capability("sample.ping")
        capability.tools = frozenset({"sample.ping", "sample.inspect"})
        client = RelayClient(
            ClientSettings(server_url="ws://localhost/ws", client_id="d", client_token='client-synthetic-credential-0000000000000000', workspace=tmp_path),
            capabilities=[capability],
        )
        await asyncio.gather(client.aclose(), client.aclose())
        assert capability.closed == 1

    asyncio.run(scenario())


def test_run_loop_retries_capability_start_after_unavailable(tmp_path: Path) -> None:
    class RestartingCapability(_Capability):
        starts = 0

        async def start(self) -> None:
            self.starts += 1
            if self.starts == 1:
                raise RuntimeError("temporarily unavailable")

    async def scenario() -> None:
        capability = RestartingCapability("sample.ping")
        client: RelayClient

        def connector(*_: object, **__: object) -> _Connection:
            client.stop()
            return _Connection(error=ConnectionError("done"))

        client = RelayClient(
            ClientSettings(server_url="ws://localhost/ws", client_id="d", client_token='client-synthetic-credential-0000000000000000', workspace=tmp_path),
            capabilities=[capability], connector=connector,
        )

        async def no_delay(_: float) -> None:
            return None

        client._sleep_or_stop = no_delay  # type: ignore[method-assign]
        await client.run()
        assert capability.starts == 2

    asyncio.run(scenario())


def test_default_client_advertises_the_fixed_relay_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every client announces exactly the fixed wire operations, no dynamic tools."""
    from mcp_relay.relay_tools import WIRE_OPERATION_NAMES

    monkeypatch.setattr("mcp_relay.client.package_version", lambda: "0.1.0")

    async def scenario() -> None:
        socket = _Socket(
            [json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1})]
        )
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            )
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if len(socket.sent) >= 2:
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task
        assert socket.sent[1] == {
            "version": 1,
            "type": "capabilities",
            "tools": sorted(WIRE_OPERATION_NAMES),
            "relay_contract": 1,
            "client_version": "0.1.0",
        }

    asyncio.run(scenario())


def test_each_generic_invocation_adapts_and_returns_bounded_provider_result(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        ping = _Capability("sample.ping")
        second = _Capability("sample.inspect")
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "ping",
                        "tool_name": "sample.ping",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "exec",
                        "tool_name": "sample.inspect",
                        "arguments": {"command_id": "pwd"},
                    }
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
            capabilities=[ping, second],
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if len(ping.invocations) == len(second.invocations) == 1:
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task
        assert [message.request_id for message in ping.invocations] == ["ping"]
        assert [message.request_id for message in second.invocations] == ["exec"]
        results = [message for message in socket.sent if message["type"] == "result"]
        assert results == [
            {
                "version": 2,
                "type": "result",
                "request_id": "ping",
                "result": {
                    "content": [],
                    "structuredContent": {"capability": "sample.ping"},
                    "isError": False,
                },
            },
            {
                "version": 2,
                "type": "result",
                "request_id": "exec",
                "result": {
                    "content": [],
                    "structuredContent": {"capability": "sample.inspect"},
                    "isError": False,
                },
            },
        ]

    asyncio.run(scenario())


def test_malformed_and_duplicate_capabilities_are_rejected(tmp_path: Path) -> None:
    settings = ClientSettings(
        server_url="ws://localhost/ws",
        client_id="d",
        client_token='client-synthetic-credential-0000000000000000',
        workspace=tmp_path,
    )
    with pytest.raises(ValueError, match="unsupported local capability"):
        RelayClient(settings, capabilities=[_Capability("invalid name")])
    assert RelayClient(settings, capabilities=[])._capabilities == {}
    with pytest.raises(ValueError, match="duplicate local capability"):
        RelayClient(
            settings,
            capabilities=[_Capability("sample.ping"), _Capability("sample.ping")],
        )


def test_capability_exception_becomes_safe_client_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        capability = _Capability(
            "sample.ping", error=RuntimeError("sensitive capability detail")
        )
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "r",
                        "tool_name": "sample.ping",
                        "arguments": {},
                    }
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
            capabilities=[capability],
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if any(message["type"] == "error" for message in socket.sent):
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task
        assert socket.sent[-1] == {
            "version": 2,
            "type": "error",
            "request_id": "r",
            "error": {
                "code": "client_error",
                "message": "local action failed",
                "execution_state": "not_started",
            },
        }

    asyncio.run(scenario())


def test_invocation_failure_debug_log_includes_bounded_exception_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario() -> None:
        error = RuntimeError("boom line one\nboom line two " + "x" * 250)
        capability = _Capability("sample.ping", error=error)
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "r",
                        "tool_name": "sample.ping",
                        "arguments": {},
                    }
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
            capabilities=[capability],
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if any(message["type"] == "error" for message in socket.sent):
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task
        assert socket.sent[-1] == {
            "version": 2,
            "type": "error",
            "request_id": "r",
            "error": {
                "code": "client_error",
                "message": "local action failed",
                "execution_state": "not_started",
            },
        }

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    asyncio.run(scenario())
    diagnostic = capsys.readouterr().err
    failure_lines = [
        line
        for line in diagnostic.splitlines()
        if "client invocation failed" in line
    ]
    assert len(failure_lines) == 1
    line = failure_lines[0]
    assert "tool=sample.ping" in line
    assert "exception=RuntimeError: boom line one boom line two" in line
    assert "x" * 201 not in line


def test_unknown_tool_errors_safely_and_bounded_arguments_are_relayed(
    tmp_path: Path,
) -> None:
    """An unknown tool fails safely; bounded arguments that do not match the
    capability's declared schema are relayed — the capability remains the
    sole validator of its own contract."""
    async def scenario() -> None:
        capability = _Capability("sample.inspect")
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "nonconforming",
                        "tool_name": "sample.inspect",
                        "arguments": {"command_id": "arbitrary"},
                    }
                ),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "unknown",
                        "tool_name": "provider.unconfigured",
                        "arguments": {},
                    }
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
            capabilities=[capability],
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(200):
            if any(item["type"] == "result" for item in socket.sent) and any(
                item["type"] == "error" for item in socket.sent
            ):
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task
        assert [message.request_id for message in capability.invocations] == [
            "nonconforming"
        ]
        assert capability.invocations[0].arguments == {"command_id": "arbitrary"}
        results = [item for item in socket.sent if item["type"] == "result"]
        errors = [item for item in socket.sent if item["type"] == "error"]
        assert [item["request_id"] for item in results] == ["nonconforming"]
        assert results[0]["result"]["structuredContent"] == {
            "capability": "sample.inspect"
        }
        assert errors == [
            {
                "version": 2,
                "type": "error",
                "request_id": "unknown",
                "error": {
                    "code": "client_error",
                    "message": "local action failed",
                    "execution_state": "not_started",
                },
            }
        ]

    asyncio.run(scenario())


def test_oversized_generic_result_becomes_safe_v2_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        capability = _Capability(
            "sample.ping",
            # Raw dict on purpose: the block is individually over-bound, so
            # constructing the model directly would fail; bounded_result()
            # must be the one refusing it (category=result-oversized).
            result={
                "content": [
                    {"type": "text", "text": "x" * (MAX_TOOL_RESULT_BYTES + 1)}
                ]
            },
        )
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "r",
                        "tool_name": "sample.ping",
                        "arguments": {},
                    }
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
            capabilities=[capability],
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if any(item["type"] == "error" for item in socket.sent):
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task
        # The oversized result is refused with the honest closed code, not
        # an opaque client_error: the caller can tell a size refusal from a
        # local crash. The message names the binding bound and the measured
        # payload (honest-refusal contract, 2026-09-09).
        error_frame = socket.sent[-1]
        assert error_frame["version"] == 2
        assert error_frame["type"] == "error"
        assert error_frame["request_id"] == "r"
        assert error_frame["error"]["code"] == "result_too_large"
        assert error_frame["error"]["message"].startswith("tool 'sample.ping':")
        assert "RELAY_MAX_TOOL_RESULT_BYTES" in error_frame["error"]["message"]
        assert "payload: " in error_frame["error"]["message"]
        assert error_frame["error"]["execution_state"] == "unknown"

    asyncio.run(scenario())


def test_serialized_result_frame_overflow_becomes_honest_size_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid result that outgrows its frame reports the binding frame bound."""
    result_bound = 65536
    frame_bound = 65537
    monkeypatch.setattr(json_bounds, "MAX_TOOL_RESULT_BYTES", result_bound)
    monkeypatch.setattr(json_bounds, "MAX_WS_MESSAGE_BYTES", frame_bound)
    request_id = "r" * 128

    result = ProviderToolResult(
        content=[ProviderTextContent(type="text", text="x" * 65481)]
    )
    result_payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert len(
        json.dumps(
            result_payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ) == result_bound
    frame_payload = {
        "version": 2,
        "type": "result",
        "request_id": request_id,
        "result": result_payload,
    }
    frame_size = len(
        json.dumps(
            frame_payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    )
    assert frame_size > frame_bound

    class ExactBoundCapability(_Capability):
        async def invoke(self, message: InvokeMessage) -> dict[str, object]:
            self.invocations.append(message)
            # CapabilityProviderClient deliberately accepts an already-bounded
            # ProviderToolResult at runtime; the protocol keeps LocalCapability's
            # public annotation as its raw JSON shape.
            return cast(dict[str, object], result)

    async def scenario() -> None:
        capability = ExactBoundCapability("sample.ping")
        socket = _Socket(
            [
                json.dumps(
                    {
                        "version": 1,
                        "type": "registered",
                        "client_id": "d",
                        "server_version": "0.1.0",
                        "relay_contract": 1,
                    }
                ),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": request_id,
                        "tool_name": "sample.ping",
                        "arguments": {},
                    }
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
            capabilities=[capability],
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if any(
                item.get("request_id") == request_id
                and item.get("type") in {"result", "error"}
                for item in socket.sent
            ):
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task

        terminal = [
            item for item in socket.sent if item.get("request_id") == request_id
        ]
        assert len(terminal) == 1
        assert terminal[0]["type"] == "error"
        error = terminal[0]["error"]
        assert isinstance(error, dict)
        assert error == {
            "code": "result_too_large",
            "message": (
                "tool 'sample.ping': RELAY_MAX_WS_MESSAGE_BYTES: "
                f"{frame_bound} < payload: {frame_size} bytes"
            ),
            "execution_state": "unknown",
        }

    asyncio.run(scenario())


def test_generic_provider_failure_preserves_safe_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps({"version": 2, "type": "invoke", "request_id": "r", "tool_name": "sample.inspect", "arguments": {"command_id": "pwd"}}),
            ]
        )
        client = RelayClient(
            ClientSettings(server_url="ws://localhost/ws", client_id="d", client_token='client-synthetic-credential-0000000000000000', workspace=tmp_path),
            capabilities=[_Capability("sample.inspect", error=RuntimeError("sensitive runner detail"))],
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if any(message["type"] == "error" for message in socket.sent):
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task
        assert socket.sent[-1] == {
            "version": 2,
            "type": "error",
            "request_id": "r",
            "error": {
                "code": "client_error",
                "message": "local action failed",
                "execution_state": "not_started",
            },
        }

    asyncio.run(scenario())


def test_cancellation_reaches_active_capability_and_suppresses_late_result(
    tmp_path: Path,
) -> None:
    class BlockingCapability(_Capability):
        cancelled = False

        async def invoke(
            self, message: InvokeMessage
        ) -> dict[str, object]:
            self.invocations.append(message)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def scenario() -> None:
        second = BlockingCapability("sample.inspect")
        ping = _Capability("sample.ping", result={"pong": True})
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps({"version": 2, "type": "invoke", "request_id": "old", "tool_name": "sample.inspect", "arguments": {"command_id": "pwd"}}),
                json.dumps({"version": 2, "type": "cancel", "request_id": "old", "reason": "stop"}),
                json.dumps({"version": 2, "type": "invoke", "request_id": "new", "tool_name": "sample.ping", "arguments": {}}),
            ]
        )
        client = RelayClient(
            ClientSettings(server_url="ws://localhost/ws", client_id="d", client_token='client-synthetic-credential-0000000000000000', workspace=tmp_path),
            capabilities=[ping, second],
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if any(message.get("request_id") == "new" for message in socket.sent):
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task
        assert second.cancelled
        assert [message.get("request_id") for message in socket.sent if message["type"] == "result"] == ["new"]

    asyncio.run(scenario())


def test_only_one_capability_action_runs_at_a_time(tmp_path: Path) -> None:
    class BlockingCapability(_Capability):
        started = asyncio.Event()
        release = asyncio.Event()

        async def invoke(
            self, message: InvokeMessage
        ) -> dict[str, object]:
            self.invocations.append(message)
            self.started.set()
            await self.release.wait()
            return self.result

    async def scenario() -> None:
        blocking = BlockingCapability("sample.ping")
        queued = _Capability("sample.inspect")
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps({"version": 2, "type": "invoke", "request_id": "first", "tool_name": "sample.ping", "arguments": {}}),
                json.dumps({"version": 2, "type": "invoke", "request_id": "second", "tool_name": "sample.inspect", "arguments": {"command_id": "pwd"}}),
            ]
        )
        client = RelayClient(
            ClientSettings(server_url="ws://localhost/ws", client_id="d", client_token='client-synthetic-credential-0000000000000000', workspace=tmp_path),
            capabilities=[blocking, queued],
        )
        task = asyncio.create_task(client.run_session(socket))
        await blocking.started.wait()
        for _ in range(100):
            if any(message["type"] == "error" for message in socket.sent):
                break
            await asyncio.sleep(0.001)
        assert queued.invocations == []
        assert socket.sent[-1] == {
            "version": 2,
            "type": "error",
            "request_id": "second",
            "error": {
                "code": "busy",
                "message": "an action is already running",
                "execution_state": "not_started",
            },
        }
        blocking.release.set()
        client.stop()
        await task

    asyncio.run(scenario())


def test_client_shutdown_awaits_every_capability_close_after_partial_failure(
    tmp_path: Path,
) -> None:
    class ClosingCapability(_Capability):
        def __init__(self, name: str, *, fail: bool = False) -> None:
            super().__init__(name)
            self.fail = fail

        async def aclose(self) -> None:
            await asyncio.sleep(0)
            self.closed += 1
            if self.fail:
                raise RuntimeError("close failed")

    async def scenario() -> None:
        first = ClosingCapability("sample.ping", fail=True)
        second = ClosingCapability("sample.inspect")
        client: RelayClient

        def connector(*_: object, **__: object) -> _Connection:
            client.stop()
            return _Connection(error=ConnectionError("partial startup failure"))

        client = RelayClient(
            ClientSettings(server_url="ws://localhost/ws", client_id="d", client_token='client-synthetic-credential-0000000000000000', workspace=tmp_path),
            capabilities=[first, second],
            connector=connector,
        )
        await client.run()
        assert first.closed == 1
        assert second.closed == 1

    asyncio.run(scenario())


def test_client_close_defers_cancellation_and_is_shared(tmp_path: Path) -> None:
    class BlockingCapability(_Capability):
        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.close_started = asyncio.Event()
            self.close_allowed = asyncio.Event()

        async def aclose(self) -> None:
            self.closed += 1
            self.close_started.set()
            await self.close_allowed.wait()

    async def scenario() -> None:
        shared = BlockingCapability("sample.ping")
        shared.tools = frozenset({"sample.ping", "sample.inspect"})  # type: ignore[assignment]
        client = RelayClient(
            ClientSettings(server_url="ws://localhost/ws", client_id="d", client_token='client-synthetic-credential-0000000000000000', workspace=tmp_path),
            capabilities=[shared],
        )
        first = asyncio.create_task(client.aclose())
        await shared.close_started.wait()
        second = asyncio.create_task(client.aclose())
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()
        shared.close_allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        await client.aclose()
        assert shared.closed == 1

    asyncio.run(scenario())


def test_client_handshake_capabilities_and_generic_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "r",
                        "tool_name": "sample.inspect",
                        "arguments": {"command_id": "pwd"},
                    }
                ),
            ]
        )
        settings = ClientSettings(
            server_url="ws://127.0.0.1:8765/ws",
            client_id="d",
            client_token='client-synthetic-credential-0000000000000000',
            workspace=tmp_path,
        )
        client = RelayClient(settings, capabilities=[_Capability("sample.inspect", result={"echo": "synthetic"})])
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if len(socket.sent) >= 3:
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task
        assert [message["type"] for message in socket.sent] == [
            "register",
            "capabilities",
            "result",
        ]
        result = socket.sent[-1]["result"]["structuredContent"]
        assert result == {"echo": "synthetic"}

    asyncio.run(scenario())


def test_client_isolates_an_alias_provider_that_becomes_unavailable(
    tmp_path: Path,
) -> None:
    """A failing third-party provider never kills the session.

    The in-flight invocation is cancelled without a result; the session
    keeps serving (the alias simply becomes non-executable).
    """
    descriptor = ProviderToolDescriptor(
        provider_name="custom",
        tool_name="wait",
        description="wait",
        input_schema={"type": "object", "additionalProperties": False},
    )

    class Provider:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.unavailable = asyncio.Event()
            self.cancelled = False

        async def list_tools(self) -> list[ProviderToolDescriptor]:
            return [descriptor]

        async def call_tool(
            self, tool_name: str, arguments: Mapping[str, JsonValue]
        ) -> ProviderToolResult:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("unreachable")

        async def wait_unavailable(self) -> None:
            await self.unavailable.wait()

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        provider = Provider()
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[],
        )
        from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog

        catalog = ClientCatalog()
        catalog.update_alias(
            AliasCatalog(
                alias="custom",
                enabled=True,
                runtime_state="running",
                transport="stdio",
                entry={"command": ["/bin/custom"]},
                last_error=None,
                catalog_available=True,
                discovery_error=None,
                descriptors=(descriptor,),
                provider=provider,
            )
        )
        client.catalog = catalog
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "offline",
                        "tool_name": "mcp.command",
                        "arguments": {
                            "alias": "custom",
                            "tool": "wait",
                            "arguments": {},
                            "catalog_revision": catalog.revision,
                        },
                    }
                ),
            ]
        )
        task = asyncio.create_task(client.run_session(socket))
        await asyncio.wait_for(provider.started.wait(), 1)
        provider.unavailable.set()
        # Give the session loop one cycle to isolate the provider failure.
        await asyncio.sleep(0.1)
        # The session never exited on its own: the failure was isolated.
        assert task.done() is False
        client.stop()
        await asyncio.wait_for(task, 2)
        await client.aclose()
        assert provider.cancelled
        assert not any(message.get("type") == "result" for message in socket.sent)

    asyncio.run(scenario())


def test_client_suppresses_result_from_provider_that_swallows_cancellation(
    tmp_path: Path,
) -> None:
    descriptor = ProviderToolDescriptor(
        provider_name="custom",
        tool_name="wait",
        description="wait",
        input_schema={"type": "object", "additionalProperties": False},
    )

    class NonCooperativeProvider:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.finished = asyncio.Event()

        async def list_tools(self) -> list[ProviderToolDescriptor]:
            return [descriptor]

        async def call_tool(
            self, tool_name: str, arguments: Mapping[str, JsonValue]
        ) -> ProviderToolResult:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                self.finished.set()
                return ProviderToolResult(
                    content=[ProviderTextContent(type="text", text="late")]
                )

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        provider = NonCooperativeProvider()
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[],
        )
        from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog

        catalog = ClientCatalog()
        catalog.update_alias(
            AliasCatalog(
                alias="custom",
                enabled=True,
                runtime_state="running",
                transport="stdio",
                entry={"command": ["/bin/custom"]},
                last_error=None,
                catalog_available=True,
                discovery_error=None,
                descriptors=(descriptor,),
                provider=provider,
            )
        )
        client.catalog = catalog
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": "late",
                        "tool_name": "mcp.command",
                        "arguments": {
                            "alias": "custom",
                            "tool": "wait",
                            "arguments": {},
                            "catalog_revision": catalog.revision,
                        },
                    }
                ),
            ]
        )
        task = asyncio.create_task(client.run_session(socket))
        await asyncio.wait_for(provider.started.wait(), 1)
        socket.inbound.put_nowait(
            json.dumps(
                {
                    "version": 2,
                    "type": "cancel",
                    "request_id": "late",
                    "reason": "stop",
                }
            )
        )
        await asyncio.wait_for(provider.cancelled.wait(), 1)
        await asyncio.wait_for(provider.finished.wait(), 1)
        client.stop()
        await task
        await client.aclose()
        assert not any(
            message.get("type") == "result" and message.get("request_id") == "late"
            for message in socket.sent
        )

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


def test_client_records_and_displays_server_package_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def scenario() -> None:
        socket = _Socket(
            [
                json.dumps(
                    {
                        "version": 1,
                        "type": "registered",
                        "client_id": "d",
                        "server_version": "0.1.0",
                        "relay_contract": 1,}
                )
            ]
        )
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='secret-token-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[_Capability("sample.ping", result={"pong": True})],
        )
        assert client.server_version == "unknown"
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(100):
            if any(message.get("type") == "capabilities" for message in socket.sent):
                break
            await asyncio.sleep(0.001)
        client.stop()
        await task
        assert client.server_version == "0.1.0"
        assert client.connection_metadata == {"server_version": "0.1.0"}

    asyncio.run(scenario())
    output = capsys.readouterr().err
    assert "Connected to Relay Server version 0.1.0" in output
    assert 'secret-token-synthetic-credential-0000000000000000' not in output


def _first_capabilities_frame(socket: object) -> dict[str, object] | None:
    for message in socket.sent:  # type: ignore[attr-defined]
        if message.get("type") == "capabilities":
            return message
    return None


async def _collect_capabilities_frame(socket: object) -> dict[str, object]:
    for _ in range(100):
        frame = _first_capabilities_frame(socket)
        if frame is not None:
            return frame
        await asyncio.sleep(0.001)
    raise AssertionError("client did not announce capabilities")


def test_client_announces_package_version_in_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mcp_relay.client.package_version", lambda: "0.2.0")

    async def scenario() -> None:
        socket = _Socket(
            [
                json.dumps(
                    {
                        "version": 1,
                        "type": "registered",
                        "client_id": "d",
                        "server_version": "0.1.0",
                        "relay_contract": 1,}
                )
            ]
        )
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='secret-token-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[_Capability("sample.ping", result={"pong": True})],
        )
        assert client.client_version == "0.2.0"
        task = asyncio.create_task(client.run_session(socket))
        frame = await _collect_capabilities_frame(socket)
        client.stop()
        await task
        assert frame["client_version"] == "0.2.0"

    asyncio.run(scenario())


def test_client_announces_unknown_when_version_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mcp_relay.client.package_version", lambda: None)

    async def scenario() -> None:
        socket = _Socket(
            [json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1})]
        )
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='secret-token-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[_Capability("sample.ping", result={"pong": True})],
        )
        assert client.client_version == "unknown"
        task = asyncio.create_task(client.run_session(socket))
        frame = await _collect_capabilities_frame(socket)
        client.stop()
        await task
        # Unresolvable package metadata is announced as the bounded "unknown".
        assert frame["client_version"] == "unknown"

    asyncio.run(scenario())


def test_client_warns_once_on_server_client_version_skew(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("mcp_relay.client.package_version", lambda: "0.2.0")

    async def scenario() -> None:
        socket = _Socket(
            [
                json.dumps(
                    {
                        "version": 1,
                        "type": "registered",
                        "client_id": "d",
                        "server_version": "0.1.0",
                        "relay_contract": 1,}
                )
            ]
        )
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='secret-token-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[_Capability("sample.ping", result={"pong": True})],
        )
        task = asyncio.create_task(client.run_session(socket))
        await _collect_capabilities_frame(socket)
        client.stop()
        await task

    asyncio.run(scenario())
    output = capsys.readouterr().err
    assert output.count("version skew detected") == 1
    assert "server 0.1.0" in output
    assert "client 0.2.0" in output
    assert 'secret-token-synthetic-credential-0000000000000000' not in output


def test_client_does_not_warn_when_versions_match_or_are_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("mcp_relay.client.package_version", lambda: "0.1.0")

    async def matching_versions() -> None:
        socket = _Socket(
            [
                json.dumps(
                    {
                        "version": 1,
                        "type": "registered",
                        "client_id": "d",
                        "server_version": "0.1.0",
                        "relay_contract": 1,}
                )
            ]
        )
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='secret-token-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[_Capability("sample.ping", result={"pong": True})],
        )
        task = asyncio.create_task(client.run_session(socket))
        await _collect_capabilities_frame(socket)
        client.stop()
        await task

    asyncio.run(matching_versions())
    output = capsys.readouterr().err
    assert "Connected to Relay Server version 0.1.0" in output
    assert "version skew detected" not in output

    monkeypatch.setattr("mcp_relay.client.package_version", lambda: "0.2.0")

    async def unknown_version_server() -> None:
        socket = _Socket(
            [json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "unknown", "relay_contract": 1})]
        )
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='secret-token-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[_Capability("sample.ping", result={"pong": True})],
        )
        task = asyncio.create_task(client.run_session(socket))
        await _collect_capabilities_frame(socket)
        client.stop()
        await task

    asyncio.run(unknown_version_server())
    output = capsys.readouterr().err
    assert "Connected to Relay Server version unknown" in output
    assert "version skew detected" not in output


def test_client_version_metadata_carries_no_secrets_or_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def scenario() -> None:
        socket = _Socket(
            [
                json.dumps(
                    {
                        "version": 1,
                        "type": "registered",
                        "client_id": "d",
                        "server_version": "0.1.0",
                        "relay_contract": 1,}
                )
            ]
        )
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='secret-token-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[],
        )
        task = asyncio.create_task(client.run_session(socket))
        await asyncio.sleep(0.05)
        client.stop()
        await task
        metadata = client.connection_metadata
        assert set(metadata) == {"server_version"}
        serialized = json.dumps(metadata)
        for forbidden in (
            'secret-token-synthetic-credential-0000000000000000',
            str(tmp_path),
            "workspace",
            "ws://localhost",
        ):
            assert forbidden not in serialized

    asyncio.run(scenario())
    output = capsys.readouterr().err
    assert "Connected to Relay Server version 0.1.0" in output
    assert 'secret-token-synthetic-credential-0000000000000000' not in output
    assert str(tmp_path) not in output

def test_capability_announcement_refreshes_and_rejects_impersonation(tmp_path: Path) -> None:
    """The announcement is the fixed op set; catalog refresh tracks providers.

    The old impersonation guarantee (a provider lying about its own name is
    rejected) now lives in the catalog publication path: descriptors whose
    ``provider_name`` does not match the alias are refused.
    """
    import asyncio

    from mcp_relay.client import ClientSettings, RelayClient
    from mcp_relay.mcp_catalog import ClientCatalog
    from mcp_relay.relay_tools import WIRE_OPERATION_NAMES

    class Provider:
        def __init__(self, tools=None):
            self.tools = tools if tools is not None else [
                ProviderToolDescriptor(
                    provider_name="sample", tool_name="echo",
                    description="Synthetic generic tool",
                    input_schema={"type": "object", "additionalProperties": False},
                )
            ]
            self.closed = 0

        async def list_tools(self):
            return self.tools

        async def call_tool(self, tool_name, arguments):
            return {"content": [], "structuredContent": {"tool": tool_name}}

        async def close(self):
            self.closed += 1

    async def scenario():
        provider = Provider()
        client = RelayClient(
            ClientSettings(server_url="ws://localhost/ws", client_id="test",
                          client_token='synthetic-token-synthetic-credential-0000000000000000', workspace=tmp_path),
        )
        try:
            await client._start_capabilities()
            # The announcement is the fixed operation set, invariant across
            # provider inventory changes.
            assert client._announcement_tools == tuple(sorted(WIRE_OPERATION_NAMES))

            catalog = ClientCatalog()
            client.catalog = catalog
            from mcp_relay.mcp_hub import McpHub  # noqa: F401  (module contract)

            def publish(record_provider, provider_name: str) -> None:
                from mcp_relay.mcp_catalog import AliasCatalog

                descriptors = []
                for descriptor in record_provider.tools:
                    assert descriptor.provider_name == provider_name, (
                        "provider identity mismatch"
                    )
                    descriptors.append(descriptor)
                catalog.update_alias(
                    AliasCatalog(
                        alias=provider_name,
                        enabled=True,
                        runtime_state="running",
                        transport="stdio",
                        entry={"command": ["/bin/x"]},
                        last_error=None,
                        catalog_available=True,
                        discovery_error=None,
                        descriptors=tuple(descriptors),
                        provider=record_provider,
                    )
                )

            publish(provider, "sample")
            assert [t["name"] for t in catalog.snapshot.tools_view("sample")] == ["echo"]

            provider.tools = [
                ProviderToolDescriptor(
                    provider_name="sample", tool_name="next",
                    description="Synthetic generic tool",
                    input_schema={"type": "object", "additionalProperties": False},
                )
            ]
            publish(provider, "sample")
            assert [t["name"] for t in catalog.snapshot.tools_view("sample")] == ["next"]

            impostor = Provider([
                ProviderToolDescriptor(
                    provider_name="other", tool_name="echo",
                    description="Synthetic generic tool",
                    input_schema={"type": "object", "additionalProperties": False},
                )
            ])
            with pytest.raises(AssertionError):
                publish(impostor, "sample")
        finally:
            await client.aclose()
        # The provider's lifecycle now belongs to the hub/catalog owner, not
        # the RelayClient: closing the client must not close the provider.
        assert provider.closed == 0

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Step 7B: the initial MCP reconciliation is decoupled from the control
# connection. The connection and heartbeat start independently of the
# (possibly long) reconciliation, and the reconciliation runs as a task
# owned by the client and stopped explicitly at shutdown — no orphan task.
# --------------------------------------------------------------------------


def test_initial_reconciliation_does_not_delay_connection_or_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session registers and heartbeats while reconciliation is pending."""
    monkeypatch.setattr("mcp_relay.client.HEARTBEAT_INTERVAL_SECONDS", 0.05)

    async def scenario() -> None:
        reconciliation_started = asyncio.Event()
        release = asyncio.Event()

        async def slow_reconciliation() -> None:
            reconciliation_started.set()
            await release.wait()

        socket = _Socket(
            [
                json.dumps(
                    {
                        "version": 1,
                        "type": "registered",
                        "client_id": "d",
                        "server_version": "0.1.0",
                        "relay_contract": 1,
                    }
                )
            ]
        )
        client = RelayClient(
            ClientSettings(
                server_url="wss://relay.example.test/ws",
                client_id="d",
                client_token='secret-token-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            connector=lambda *_, **__: _Connection(socket),
        )
        task = client.start_initial_reconciliation(slow_reconciliation)
        run_task = asyncio.create_task(client.run())
        try:
            for _ in range(200):
                if any(
                    message.get("type") == "heartbeat" for message in socket.sent
                ):
                    break
                await asyncio.sleep(0.01)
            # The reconciliation started but never finished, and the control
            # connection was registered and heartbeated anyway.
            assert reconciliation_started.is_set()
            assert not task.done()
            assert any(
                message.get("type") == "capabilities" for message in socket.sent
            )
            assert any(
                message.get("type") == "heartbeat" for message in socket.sent
            )
        finally:
            release.set()
            client.stop()
            await asyncio.wait_for(run_task, timeout=5)
            await client.aclose()
        # The owned reconciliation was released normally here; the
        # explicit-stop-at-shutdown contract is covered by
        # test_shutdown_stops_the_owned_reconciliation_task_without_orphans.
        assert task.done()

    asyncio.run(scenario())


def test_shutdown_stops_the_owned_reconciliation_task_without_orphans(
    tmp_path: Path,
) -> None:
    """aclose cancels and awaits the owned reconciliation task exactly once."""
    cancelled = asyncio.Event()

    async def reconciliation() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def scenario() -> None:
        client = RelayClient(
            ClientSettings(
                server_url="wss://relay.example.test/ws",
                client_id="d",
                client_token='secret-token-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            )
        )
        task = client.start_initial_reconciliation(reconciliation)
        await asyncio.sleep(0)
        assert not task.done()
        await client.aclose()
        assert task.cancelled()
        assert cancelled.is_set()
        pending = [
            item
            for item in asyncio.all_tasks()
            if item is not asyncio.current_task()
        ]
        assert task not in pending

    asyncio.run(scenario())


def test_run_client_starts_reconciliation_in_background_and_stops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_run_client attaches the reconciliation without awaiting it inline."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("client: {}\n", encoding="utf-8")
    order: list[str] = []
    captured: dict[str, object] = {}

    async def fake_initial_reconciliation(
        hub: object, catalog: object
    ) -> None:
        del hub, catalog
        order.append("reconciliation-started")
        await asyncio.Event().wait()

    async def fake_signal_run(client: RelayClient) -> None:
        del client
        order.append("session-started")

    original = RelayClient.start_initial_reconciliation

    def spy(client: RelayClient, reconciliation: object) -> object:
        order.append("reconciliation-attached")
        task = original(client, reconciliation)  # type: ignore[arg-type]
        captured["task"] = task
        return task

    monkeypatch.setattr(RelayClient, "start_initial_reconciliation", spy)
    monkeypatch.setattr(
        "mcp_relay.client._initial_reconciliation", fake_initial_reconciliation
    )
    monkeypatch.setattr(
        "mcp_relay.client._run_with_signal_handlers", fake_signal_run
    )
    monkeypatch.setattr("mcp_relay.client._refresh_catalog", lambda *args: None)

    settings = ClientSettings(
        server_url="ws://127.0.0.1:1/ws",
        client_id="d",
        client_token='client-synthetic-credential-0000000000000000',
        workspace=tmp_path,
    )
    asyncio.run(_run_client(settings, config_path=config_path))

    # Attached before the session starts, and the blocking reconciliation
    # never ran inline before the session (an inline await would have hung
    # forever and never reached "session-started").
    assert order[0] == "reconciliation-attached"
    assert order.index("session-started") < order.index(
        "reconciliation-started"
    )
    # Shutdown stopped the owned task explicitly: no orphan.
    task = captured["task"]
    assert isinstance(task, asyncio.Task)
    assert task.cancelled()


# ---------------------------------------------------------------------------
# Task 4: precise client-side mcp.command execution events
# ---------------------------------------------------------------------------

_COMMAND_DESCRIPTOR = ProviderToolDescriptor(
    provider_name="sample",
    tool_name="click",
    description="click",
    input_schema={"type": "object", "additionalProperties": False},
)


def _command_catalog(
    provider: object,
    *,
    alias: str = "sample",
    catalog_available: bool = True,
    with_provider: bool = True,
) -> object:
    from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog

    catalog = ClientCatalog()
    catalog.update_alias(
        AliasCatalog(
            alias=alias,
            enabled=True,
            runtime_state="running" if catalog_available else "stopped",
            transport="stdio",
            entry={"command": ["/bin/sample"]},
            last_error=None,
            catalog_available=catalog_available,
            discovery_error=None if catalog_available else "provider stopped",
            descriptors=(_COMMAND_DESCRIPTOR,),
            provider=provider if with_provider else None,
        )
    )
    return catalog


def _command_invoke(
    request_id: str,
    *,
    alias: str = "sample",
    tool: str = "click",
    arguments: object | None = None,
    revision: str = "THE_CATALOG_REVISION",
) -> str:
    return json.dumps(
        {
            "version": 2,
            "type": "invoke",
            "request_id": request_id,
            "tool_name": "mcp.command",
            "arguments": {
                "alias": alias,
                "tool": tool,
                "arguments": {} if arguments is None else arguments,
                "catalog_revision": revision,
            },
        }
    )


_REGISTERED = json.dumps(
    {
        "version": 1,
        "type": "registered",
        "client_id": "d",
        "server_version": "0.1.0",
        "relay_contract": 1,
    }
)


async def _run_command_session(
    client: RelayClient,
    socket: _Socket,
    *,
    ready: Callable[[list[dict[str, object]]], bool],
    provider_started: asyncio.Event | None = None,
    settle_seconds: float = 0.0,
) -> None:
    task = asyncio.create_task(client.run_session(socket))
    try:
        for _ in range(500):
            if provider_started is not None and provider_started.is_set():
                break
            if provider_started is None and ready(socket.sent):
                break
            await asyncio.sleep(0.001)
        if settle_seconds:
            await asyncio.sleep(settle_seconds)
    finally:
        client.stop()
        await task
        await client.aclose()


def _command_lines(output: str) -> list[str]:
    return [
        line
        for line in output.splitlines()
        if "mcp.command done" in line or "mcp.command failed" in line
    ]


def test_mcp_command_done_event_replaces_generic_executing_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A normal result logs one INFO done event with the isError flag."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    class Provider:
        async def list_tools(self) -> list[ProviderToolDescriptor]:
            return [_COMMAND_DESCRIPTOR]

        async def call_tool(
            self, tool_name: str, arguments: Mapping[str, JsonValue]
        ) -> ProviderToolResult:
            return ProviderToolResult(
                content=[{"type": "text", "text": "SECRET_RESULT_MARKER"}],
                structured_content={"clicked": True},
            )

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[],
        )
        client.catalog = _command_catalog(Provider())
        socket = _Socket(
            [
                _REGISTERED,
                _command_invoke(
                    "req-done",
                    arguments={"SECRET_ARG_MARKER": "x"},
                    revision=client.catalog.revision,
                ),
            ]
        )
        await _run_command_session(
            client,
            socket,
            ready=lambda sent: any(
                m.get("type") == "result" and m.get("request_id") == "req-done"
                for m in sent
            ),
        )

    asyncio.run(scenario())
    output = capsys.readouterr().err
    lines = _command_lines(output)
    assert len(lines) == 1, lines
    assert "[INFO] " in lines[0]
    assert "mcp.command done" in lines[0]
    assert "isError=false" in lines[0]
    # Validated identifiers only: no raw envelope, arguments, or results.
    assert "SECRET_ARG_MARKER" not in output
    assert "SECRET_RESULT_MARKER" not in output
    # The generic message is gone.
    assert "Executing tool: mcp.command" not in output


def test_mcp_command_native_iserror_is_a_distinguishable_done_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A native isError result is still received (INFO done), marked isError=true."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    class Provider:
        async def list_tools(self) -> list[ProviderToolDescriptor]:
            return [_COMMAND_DESCRIPTOR]

        async def call_tool(
            self, tool_name: str, arguments: Mapping[str, JsonValue]
        ) -> ProviderToolResult:
            return ProviderToolResult(
                content=[{"type": "text", "text": "SECRET_ERROR_MARKER"}],
                structured_content={"secret": "SECRET_STRUCTURED_MARKER"},
                is_error=True,
            )

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[],
        )
        client.catalog = _command_catalog(Provider())
        socket = _Socket(
            [
                _REGISTERED,
                _command_invoke("req-err", revision=client.catalog.revision),
            ]
        )
        await _run_command_session(
            client,
            socket,
            ready=lambda sent: any(
                m.get("type") == "result" and m.get("request_id") == "req-err"
                for m in sent
            ),
        )

    asyncio.run(scenario())
    output = capsys.readouterr().err
    lines = _command_lines(output)
    assert len(lines) == 1, lines
    assert "[INFO] " in lines[0]
    assert "mcp.command done" in lines[0]
    assert "isError=true" in lines[0]
    assert "error_source=provider" in lines[0]
    assert "SECRET_ERROR_MARKER" not in output
    assert "SECRET_STRUCTURED_MARKER" not in output
    # Received ≠ business success: no ERROR line for a native isError result.
    assert not any("[ERROR]" in line for line in lines)


@pytest.mark.parametrize(
    ("request_id", "expected_code", "expected_state", "setup"),
    [
        pytest.param(
            "req-no-catalog", "execution_failed", "not_started",
            "missing_catalog", id="missing-catalog",
        ),
        pytest.param(
            "req-bad-envelope", "invalid_arguments", "not_started",
            "invalid_arguments", id="invalid-arguments",
        ),
        pytest.param(
            "req-stale", "catalog_stale", "not_started",
            "stale", id="stale-catalogue",
        ),
        pytest.param(
            "req-unavailable", "alias_unavailable", "not_started",
            "unavailable", id="unavailable-alias",
        ),
        pytest.param(
            "req-timeout", "timeout", "unknown",
            "timeout", id="timeout",
        ),
        pytest.param(
            "req-failed", "execution_failed", "unknown",
            "execution_failed", id="execution-failed",
        ),
        pytest.param(
            "req-oversized", "result_too_large", "unknown",
            "oversized", id="oversized-result",
        ),
    ],
)
def test_mcp_command_failures_log_one_terminal_error_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    request: pytest.FixtureRequest,
    request_id: str,
    expected_code: str,
    expected_state: str,
    setup: str,
) -> None:
    """Every terminal command failure logs exactly one sanitized ERROR event."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    started = asyncio.Event()

    class OkProvider:
        async def list_tools(self) -> list[ProviderToolDescriptor]:
            return [_COMMAND_DESCRIPTOR]

        async def call_tool(
            self, tool_name: str, arguments: Mapping[str, JsonValue]
        ) -> ProviderToolResult:
            started.set()
            return ProviderToolResult(content=[])

        async def close(self) -> None:
            return None

    class TimeoutProvider(OkProvider):
        async def call_tool(
            self, tool_name: str, arguments: Mapping[str, JsonValue]
        ) -> ProviderToolResult:
            started.set()
            raise ProviderTimeoutError("SECRET_TIMEOUT_DETAIL")

    class FailedProvider(OkProvider):
        async def call_tool(
            self, tool_name: str, arguments: Mapping[str, JsonValue]
        ) -> ProviderToolResult:
            started.set()
            raise ProviderToolError("SECRET_FAILURE_DETAIL")

    class OversizedProvider(OkProvider):
        async def call_tool(
            self, tool_name: str, arguments: Mapping[str, JsonValue]
        ) -> ProviderToolResult:
            started.set()
            raise ProviderResultTooLargeError(
                "SECRET_OVERSIZED_DETAIL: 999999 bytes"
            )

    provider: object = OkProvider()
    catalog_available = True
    with_provider = True
    revision = "the-revision"
    invoke_arguments: object = {"SECRET_ARG_MARKER": "x"}
    if setup == "timeout":
        provider = TimeoutProvider()
    elif setup == "execution_failed":
        provider = FailedProvider()
    elif setup == "oversized":
        provider = OversizedProvider()
    elif setup == "stale":
        revision = "a-stale-revision"
    elif setup == "unavailable":
        catalog_available = False
        with_provider = False
    elif setup == "invalid_arguments":
        invoke_arguments = "not-an-object-SECRET_ARG_MARKER"

    async def scenario() -> None:
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[],
        )
        if setup != "missing_catalog":
            client.catalog = _command_catalog(  # type: ignore[assignment]
                provider,
                catalog_available=catalog_available,
                with_provider=with_provider,
            )
        socket = _Socket(
            [
                _REGISTERED,
                _command_invoke(
                    request_id,
                    arguments=invoke_arguments,
                    revision=(
                        client.catalog.revision
                        if client.catalog is not None
                        and revision == "the-revision"
                        else revision
                    ),
                ),
            ]
        )
        await _run_command_session(
            client,
            socket,
            ready=lambda sent: any(
                m.get("type") == "error" and m.get("request_id") == request_id
                for m in sent
            ),
        )

    asyncio.run(scenario())
    output = capsys.readouterr().err
    lines = _command_lines(output)
    assert len(lines) == 1, lines
    assert "[ERROR] " in lines[0]
    assert "mcp.command failed" in lines[0]
    if setup == "missing_catalog":
        assert " alias=" not in lines[0]
        assert " tool=" not in lines[0]
    else:
        assert "alias=sample tool=click" in lines[0]
    assert f"code={expected_code}" in lines[0]
    assert f"execution_state={expected_state}" in lines[0]
    # Closed codes and states only: no raw envelope, exception detail, or args.
    for marker in (
        "SECRET_ARG_MARKER",
        "SECRET_TIMEOUT_DETAIL",
        "SECRET_FAILURE_DETAIL",
        "SECRET_OVERSIZED_DETAIL",
    ):
        assert marker not in output, marker


@pytest.mark.parametrize(
    ("alias", "tool"),
    [
        ("sample\nSECRET_ALIAS", "click"),
        ("sample tool=SECRET_ALIAS", "click"),
        ("sample", "click\nSECRET_TOOL"),
        ("sample", "click alias=SECRET_TOOL"),
        ("SECRET_API_TOKEN", "click"),
        ("sample", "SECRET_TOOL_TOKEN"),
    ],
)
def test_mcp_command_failed_identifiers_reject_log_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    alias: str,
    tool: str,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    async def scenario() -> None:
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[],
        )
        if alias == "SECRET_API_TOKEN" or tool == "SECRET_TOOL_TOKEN":
            # Prove unrecognized names are omitted even when a real catalog
            # exists; a syntactically valid caller-supplied name may be a key.
            client.catalog = _command_catalog(object())  # type: ignore[assignment]
        socket = _Socket(
            [_REGISTERED, _command_invoke("req-malformed", alias=alias, tool=tool)]
        )
        await _run_command_session(
            client,
            socket,
            ready=lambda sent: any(
                item.get("type") == "error"
                and item.get("request_id") == "req-malformed"
                for item in sent
            ),
        )

    asyncio.run(scenario())
    output = capsys.readouterr().err
    assert "[ERROR] mcp.command failed:" in output
    assert "SECRET_ALIAS" not in output
    assert "SECRET_API_TOKEN" not in output
    assert "SECRET_TOOL" not in output
    assert "\nSECRET" not in output


def test_mcp_command_cancellation_never_logs_done_or_late_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cancellation is distinct: no fake done, no late-result event."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    started = asyncio.Event()
    release = asyncio.Event()

    class SwallowingProvider:
        async def list_tools(self) -> list[ProviderToolDescriptor]:
            return [_COMMAND_DESCRIPTOR]

        async def call_tool(
            self, tool_name: str, arguments: Mapping[str, JsonValue]
        ) -> ProviderToolResult:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # A provider that swallows cancellation must not yield a
                # late result — and the client must not log one either.
                return ProviderToolResult(
                    content=[{"type": "text", "text": "LATE_RESULT_MARKER"}]
                )
            return ProviderToolResult(content=[])

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token='client-synthetic-credential-0000000000000000',
                workspace=tmp_path,
            ),
            capabilities=[],
        )
        client.catalog = _command_catalog(SwallowingProvider())
        socket = _Socket(
            [
                _REGISTERED,
                _command_invoke("req-cancel", revision=client.catalog.revision),
                json.dumps(
                    {
                        "version": 2,
                        "type": "cancel",
                        "request_id": "req-cancel",
                        "reason": "operator requested",
                    }
                ),
            ]
        )
        await _run_command_session(
            client,
            socket,
            ready=lambda sent: True,
            provider_started=started,
            settle_seconds=0.1,
        )
        release.set()

    asyncio.run(scenario())
    output = capsys.readouterr().err
    lines = _command_lines(output)
    assert lines == [], lines
    assert "LATE_RESULT_MARKER" not in output
