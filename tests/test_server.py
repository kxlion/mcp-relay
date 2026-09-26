from __future__ import annotations

import copy
import io
import logging
import logging.config
import re
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import mcp_relay.json_bounds as json_bounds
import mcp_relay.server as server_module
from mcp_relay.server import (
    RelaySettings,
    _classify_bind_address,
    create_app,
    main,
)
from mcp_relay.version import package_version


def settings() -> RelaySettings:
    return RelaySettings(
        client_id="client-a", client_token='client-secret-synthetic-credential-0000000000000000', mcp_token='control-secret-synthetic-credential-0000000000000000'
    )


def test_uvicorn_logging_config_routes_mcp_to_unified_handlers() -> None:
    from uvicorn.config import LOGGING_CONFIG

    before = copy.deepcopy(LOGGING_CONFIG)
    configured = server_module._uvicorn_logging_config()

    # mcp goes through the unified stderr handler plus the file bridge.
    assert configured["loggers"]["mcp"] == {
        "handlers": ["relay", "relay_file_bridge"],
        "level": "INFO",
        "propagate": False,
    }
    # uvicorn and relay runtime loggers share the same diagnostics bridge.
    # uvicorn/uvicorn.error are capped at INFO: their TRACE records are raw
    # handshake header dumps (Bearer credentials) and must never reach a
    # sink; mcp_relay stays at DEBUG (file sink receives every level).
    assert configured["loggers"]["uvicorn"] == {
        "handlers": ["relay", "relay_file_bridge"],
        "level": "INFO",
        "propagate": False,
    }
    assert configured["loggers"]["uvicorn.error"] == {
        "handlers": ["relay", "relay_file_bridge"],
        "level": "INFO",
        "propagate": False,
    }
    assert configured["loggers"]["mcp_relay"] == {
        "handlers": ["relay", "relay_file_bridge"],
        "level": "DEBUG",
        "propagate": False,
    }
    assert configured["loggers"]["uvicorn.access"] == {
        "handlers": ["relay", "relay_file_bridge"],
        "level": "DEBUG",
        "propagate": False,
        "filters": ["relay_access_level"],
    }
    assert configured["filters"]["relay_access_level"]["()"] is (
        server_module._AccessLevelFilter
    )
    relay_handler = configured["handlers"]["relay"]
    assert relay_handler["formatter"] == "relay"
    assert relay_handler["level"] == server_module._console_level()
    assert configured["formatters"]["relay"]["()"] == (
        "mcp_relay.server.UnifiedOperatorFormatter"
    )
    bridge_spec = configured["handlers"]["relay_file_bridge"]
    assert bridge_spec["()"] is server_module._DiagnosticsFileBridge
    assert bridge_spec["formatter"] == "relay"
    # The stock uvicorn handlers and formatters stay untouched.
    assert configured["handlers"]["default"] == before["handlers"]["default"]
    assert configured is not LOGGING_CONFIG
    assert configured["loggers"] is not LOGGING_CONFIG["loggers"]
    assert LOGGING_CONFIG == before


def test_diagnostics_file_bridge_writes_formatted_lines_into_the_file_sink(
    tmp_path: Path,
) -> None:
    from mcp_relay import diagnostics as diagnostics_module

    log = tmp_path / "server.log"
    diagnostics_module.set_log_file(log)
    try:
        bridge = server_module._DiagnosticsFileBridge()
        bridge.setFormatter(server_module.UnifiedOperatorFormatter())
        record = logging.LogRecord(
            "mcp.test", logging.INFO, __file__, 1, "bridged %s", ("event",), None
        )
        bridge.emit(record)
    finally:
        diagnostics_module.set_log_file(None)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines == [f"{lines[0].split(' [')[0]} [INFO] bridged event"]


def test_mcp_startup_log_uses_the_unified_operator_formatter_once() -> None:
    message = "StreamableHTTP session manager started"
    mcp_logger = logging.getLogger("mcp")
    old_handlers = mcp_logger.handlers[:]
    old_level = mcp_logger.level
    old_propagate = mcp_logger.propagate
    captured = io.StringIO()
    configured_handlers: list[logging.Handler] = []

    try:
        logging.config.dictConfig(server_module._uvicorn_logging_config())
        relay_handler = logging.getLogger("mcp").handlers[0]
        configured_handlers = logging.getLogger("mcp").handlers[:]
        old_stream = relay_handler.setStream(captured)
        try:
            logging.getLogger("mcp.server.streamable_http_manager").info(message)
        finally:
            relay_handler.setStream(old_stream)
    finally:
        mcp_logger.handlers[:] = old_handlers
        mcp_logger.setLevel(old_level)
        mcp_logger.propagate = old_propagate
        for handler in configured_handlers:
            if handler not in old_handlers:
                handler.close()

    output = captured.getvalue()
    assert output.count(message) == 1
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T[\d:.]+Z) \[INFO\] " + re.escape(message) + "\n",
        output,
    )
    assert match is not None


def test_server_main_hands_settings_to_the_dual_listener_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
        "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
    }
    monkeypatch.setattr("mcp_relay.server.os.environ", environment)
    observed: dict[str, object] = {}

    def fake_run(settings: RelaySettings) -> None:
        observed.update(settings=settings)

    monkeypatch.setattr(server_module, "_run_relay", fake_run)
    main([])

    settings = observed["settings"]
    assert isinstance(settings, RelaySettings)
    assert settings.mcp_bind_host == "127.0.0.1"
    assert settings.mcp_port == 8000
    assert settings.client_bind_host == "127.0.0.1"
    assert settings.client_port == 8001


def test_relay_settings_default_to_loopback_without_a_transport_policy() -> None:
    configured = settings()
    assert configured.mcp_bind_host == "127.0.0.1"
    assert configured.client_bind_host == "127.0.0.1"
    assert "allow_" + "insecure_ws" not in RelaySettings.model_fields


@pytest.mark.parametrize(
    ("raw_bound", "resolved_bound"),
    [
        ("1", 64 * 1024),
        (str(1024 * 1024 * 1024), 32 * 1024 * 1024),
    ],
)
def test_env_only_settings_use_the_already_resolved_frame_bound(
    raw_bound: str,
    resolved_bound: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(json_bounds, "MAX_WS_MESSAGE_BYTES", resolved_bound)
    configured = RelaySettings.from_environment(
        {
            "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
            "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
            "RELAY_MAX_WS_MESSAGE_BYTES": raw_bound,
        }
    )
    assert configured.max_ws_message_bytes == resolved_bound


def test_env_only_main_passes_the_resolved_frame_bound_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
        "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
        "RELAY_MAX_WS_MESSAGE_BYTES": str(16 * 1024 * 1024),
    }
    monkeypatch.setattr("mcp_relay.server.os.environ", environment)
    observed: dict[str, object] = {}

    def fake_run(settings: RelaySettings) -> None:
        observed.update(settings=settings)

    monkeypatch.setattr(server_module, "_run_relay", fake_run)
    main([])

    expected = 16 * 1024 * 1024
    assert json_bounds.MAX_WS_MESSAGE_BYTES == expected
    configured = observed["settings"]
    assert getattr(configured, "max_ws_message_bytes") == expected


def test_relay_settings_reject_a_frame_bound_outside_the_resolved_snapshot() -> None:
    with pytest.raises(ValueError, match="^invalid relay server configuration$"):
        RelaySettings(
            client_token='client-secret-synthetic-credential-0000000000000000',
            mcp_token='mcp-secret-synthetic-credential-0000000000000000',
            max_ws_message_bytes=json_bounds.MAX_WS_MESSAGE_BYTES + 1,
        )


def test_server_main_uses_canonical_environment_defaults_and_optional_deferred_mcp_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
        "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
    }
    monkeypatch.setattr("mcp_relay.server.os.environ", environment)
    observed: dict[str, object] = {}

    def fake_run(settings: RelaySettings) -> None:
        observed.update(settings=settings)

    monkeypatch.setattr(server_module, "_run_relay", fake_run)
    try:
        main([])
    except SystemExit as exc:
        pytest.fail(f"canonical server environment was rejected: {exc}")

    assert "RELAY_CLIENT_ID" not in environment
    assert "RELAY_MCP_ALLOWED_HOSTS" not in environment
    assert "RELAY_MCP_ALLOWED_ORIGINS" not in environment

    environment["RELAY_CLIENT_TOKEN"] = 'mcp-secret-synthetic-credential-0000000000000000'
    with pytest.raises(SystemExit):
        main([])


def test_server_main_resolves_per_listener_binds_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "RELAY_SERVER_MCP_HOST": "127.0.0.1",
        "RELAY_SERVER_MCP_PORT": "8765",
        "RELAY_SERVER_CLIENT_HOST": "0.0.0.0",
        "RELAY_SERVER_CLIENT_PORT": "8766",
        "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
        "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
    }
    monkeypatch.setattr("mcp_relay.server.os.environ", environment)
    observed: dict[str, object] = {}

    def fake_run(settings: RelaySettings) -> None:
        observed.update(settings=settings)

    monkeypatch.setattr(server_module, "_run_relay", fake_run)
    try:
        main([])
    except SystemExit as exc:
        pytest.fail(f"canonical per-listener binds were rejected: {exc}")

    configured = observed["settings"]
    assert isinstance(configured, RelaySettings)
    assert configured.mcp_bind_host == "127.0.0.1"
    assert configured.mcp_port == 8765
    assert configured.client_bind_host == "0.0.0.0"
    assert configured.client_port == 8766


def test_server_main_accepts_explicit_canonical_lan_wildcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "RELAY_SERVER_MCP_HOST": "0.0.0.0",
        "RELAY_SERVER_MCP_PORT": "8000",
        "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
        "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
    }
    monkeypatch.setattr("mcp_relay.server.os.environ", environment)
    observed: dict[str, object] = {}

    def fake_run(settings: RelaySettings) -> None:
        observed.update(settings=settings)

    monkeypatch.setattr(server_module, "_run_relay", fake_run)
    try:
        main([])
    except SystemExit as exc:
        pytest.fail(f"explicit canonical LAN bind was rejected: {exc}")

    configured = observed["settings"]
    assert isinstance(configured, RelaySettings)
    assert configured.mcp_bind_host == "0.0.0.0"


def test_relay_configuration_errors_never_echo_tokens(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_secret = 'CLIENT_TOKEN_SENTINEL-synthetic-credential-0000000000000000'
    control_secret = 'CONTROL_TOKEN_SENTINEL-synthetic-credential-0000000000000000'
    base = {
        "client_id": "client-a",
        "client_token": client_secret,
        "mcp_token": control_secret,
    }
    for invalid in (
        {"client_id": ""},
        {"client_token": ""},
        {"mcp_token": ""},
        {"client_token": client_secret, "mcp_token": client_secret},
        {"max_timeout_seconds": 0},
        {"max_ws_message_bytes": 1},
    ):
        with pytest.raises(ValueError) as error:
            RelaySettings(**(base | invalid))
        assert str(error.value) == "invalid relay server configuration"
        assert client_secret not in str(error.value)
        assert control_secret not in str(error.value)
    with pytest.raises(ValueError) as error:
        RelaySettings.model_validate(base | {"mcp_token": client_secret})
    assert str(error.value) == "invalid relay server configuration"
    assert client_secret not in str(error.value)
    assert control_secret not in str(error.value)

    monkeypatch.setattr(
        "mcp_relay.server.os.environ",
        {
            "MCP_RELAY_CLIENT_ID": "client-a",
            "MCP_RELAY_CLIENT_TOKEN": client_secret,
            "MCP_RELAY_CONTROL_TOKEN": client_secret,
        },
    )
    monkeypatch.setattr(sys, "argv", ["mcp-relay-server"])
    with pytest.raises(SystemExit):
        main()
    stderr = capsys.readouterr().err
    assert client_secret not in stderr
    assert control_secret not in stderr


@pytest.mark.parametrize(
    ("field", "valid", "invalid"),
    [
        ("client_id", "a" * 128, "a" * 129),
        ("client_id", "client.a_1-2", "client/a"),
        ("client_token", "a" * 256, "a" * 257),
        ("mcp_token", "c" * 256, "c" * 257),
        ("registry_base_url", "https://registry.test", "not-a-url"),
    ],
)
def test_relay_settings_match_protocol_identifier_and_token_limits(
    field: str, valid: str, invalid: str
) -> None:
    base = {
        "client_id": "client-a",
        "client_token": 'client-secret-synthetic-credential-0000000000000000',
        "mcp_token": 'control-secret-synthetic-credential-0000000000000000',
    }

    assert getattr(RelaySettings(**(base | {field: valid})), field) == valid
    with pytest.raises(ValueError, match="^invalid relay server configuration$"):
        RelaySettings(**(base | {field: invalid}))
    with pytest.raises(ValueError, match="^invalid relay server configuration$"):
        RelaySettings.model_validate(base | {field: invalid})


@pytest.mark.parametrize(
    ("host", "classification"),
    [
        ("127.0.0.1", "loopback"),
        ("127.42.0.1", "loopback"),
        ("::1", "loopback"),
        ("0.0.0.0", "wildcard"),
        ("::", "wildcard"),
        ("100.64.0.1", "specific"),
        ("192.168.1.1", "specific"),
        ("::ffff:192.168.1.1", "specific"),
    ],
)
def test_bind_addresses_classify_for_exposure_reporting(
    host: str, classification: str
) -> None:
    assert _classify_bind_address(host) == classification


def test_server_cli_rejects_invalid_configuration_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = "PORT_SENTINEL"
    monkeypatch.setattr(
        "mcp_relay.server.os.environ",
        {
            "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
            "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
            "RELAY_SERVER_MCP_PORT": sentinel,
        },
    )
    monkeypatch.setattr(sys, "argv", ["mcp-relay-server"])
    with pytest.raises(SystemExit):
        main()
    stderr = capsys.readouterr().err
    assert sentinel not in stderr

    # The legacy combined host/port flags no longer exist on the parser.
    monkeypatch.setattr(
        "mcp_relay.server.os.environ",
        {
            "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
            "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
        },
    )
    with pytest.raises(SystemExit):
        main(["--host", "0.0.0.0"])
    stderr = capsys.readouterr().err
    assert "unrecognized arguments" in stderr


def test_mcp_accepts_any_host_and_origin_with_valid_token() -> None:
    """Host/Origin policy is removed; correct-token requests are never refused."""
    app = create_app(
        RelaySettings(
            client_id="client-a",
            client_token='client-secret-synthetic-credential-0000000000000000',
            mcp_token='control-secret-synthetic-credential-0000000000000000',
            mcp_bind_host="0.0.0.0",
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": 'Bearer control-secret-synthetic-credential-0000000000000000',
                "Host": "relay.example.test:8000",
                "Origin": "https://relay.example.test",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code not in {403, 421}


@pytest.mark.parametrize("path", ["/v2/clients/client-a/invoke", "/v2/invoke"])
def test_rest_invocation_surfaces_are_not_exposed(path: str) -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.post(
            path,
            headers={"Authorization": 'Bearer control-secret-synthetic-credential-0000000000000000'},
            json={"tool_name": "sample.ping", "arguments": {}},
        )

    assert response.status_code == 404
    assert app.openapi_url is None
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi()["paths"] == {}
    assert all(
        getattr(route, "path", None) not in {"/v2/invoke", "/v2/clients/{client_id}/invoke"}
        for route in app.routes
    )


def test_websocket_rejects_v1_result_after_authenticated_registration() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as socket:
            socket.send_json({"version": 1, "type": "register", "client_id": "client-a", "relay_contract": 1})
            socket.receive_json()
            socket.send_json(
                {"version": 1, "type": "result", "request_id": "r", "result": {}}
            )
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
            assert error.value.code == 1002


@pytest.mark.parametrize("method", ["get", "post", "delete"])
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"authorization", b"Bearer wrong")],
        [(b"authorization", b"Basic control-secret")],
        [(b"authorization", b"Bearer")],
        [(b"authorization", b"bearer control-secret")],
        [(b"authorization", b"Bearer  control-secret")],
        [(b"authorization", b"Bearer control-secret extra")],
        [(b"authorization", b"Bearer token-\xff")],
        [(b"authorization", b"Bearer " + b"x" * 257)],
        [
            (b"authorization", b"Bearer control-secret"),
            (b"authorization", b"Bearer control-secret"),
        ],
    ],
)
def test_mcp_boundary_rejects_invalid_authorization(
    method: str,
    headers: list[tuple[bytes, bytes]],
) -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.request(
            method,
            "/mcp",
            headers=headers + [(b"accept", b"application/json, text/event-stream")],
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "authentication required"}


def test_mcp_canonical_path_accepts_valid_bearer_without_redirect() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": 'Bearer control-secret-synthetic-credential-0000000000000000',
                "Host": "127.0.0.1:8000",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            follow_redirects=False,
        )

    assert response.status_code != 401
    assert response.status_code not in {301, 302, 303, 307, 308}


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("relay.example.test", id="arbitrary-dns"),
        pytest.param("203.0.113.17:8765", id="arbitrary-ipv4"),
        pytest.param("[2001:db8::17]:8765", id="arbitrary-ipv6"),
    ],
)
def test_mcp_arbitrary_host_is_not_rejected_by_relay_policy(host: str) -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": 'Bearer control-secret-synthetic-credential-0000000000000000',
                "Host": host,
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code not in {401, 403, 421}


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("https://attacker.example", id="arbitrary-dns"),
        pytest.param("http://203.0.113.17:9999", id="arbitrary-ip"),
        pytest.param("null", id="opaque-origin"),
    ],
)
def test_mcp_arbitrary_origin_is_not_rejected_by_relay_policy(origin: str) -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": 'Bearer control-secret-synthetic-credential-0000000000000000',
                "Host": "127.0.0.1:8000",
                "Origin": origin,
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code not in {401, 403, 421}


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param(None, id="missing"),
        pytest.param("Bearer wrong", id="invalid"),
        pytest.param('Bearer client-secret-synthetic-credential-0000000000000000', id="other-channel-token"),
    ],
)
def test_mcp_authentication_stays_fail_closed_with_arbitrary_host_and_origin(
    authorization: str | None,
) -> None:
    app = create_app(settings())
    headers = [
        (b"host", b"relay.example.test"),
        (b"origin", b"https://attacker.example"),
        (b"accept", b"application/json, text/event-stream"),
    ]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("ascii")))
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_mcp_slash_redirect_is_authenticated_and_points_to_canonical_path() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        missing = client.post("/mcp/", follow_redirects=False)
        authenticated = client.post(
            "/mcp/",
            headers={
                "Authorization": 'Bearer control-secret-synthetic-credential-0000000000000000',
                "Host": "127.0.0.1:8000",
            },
            follow_redirects=False,
        )

    assert missing.status_code == 401
    assert authenticated.status_code == 307
    assert authenticated.headers["location"] == "http://127.0.0.1:8000/mcp"


def test_mcp_slash_redirect_accepts_arbitrary_host_with_valid_token() -> None:
    """Host policy is removed; /mcp/ redirect no longer depends on Host."""
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            headers={
                "Authorization": 'Bearer control-secret-synthetic-credential-0000000000000000',
                "Host": "hostile.example",
            },
            follow_redirects=False,
        )

    assert response.status_code == 307
    assert response.headers["location"] == "http://hostile.example/mcp"


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        pytest.param("127.0.0.1:43123", None, id="loopback-host"),
        pytest.param("192.168.1.41:8000", None, id="lan-host"),
        pytest.param(
            "192.168.1.41:8000",
            "http://192.168.1.41:8000",
            id="same-origin-lan",
        ),
        pytest.param("relay.example.test", None, id="arbitrary-host"),
        pytest.param(
            "127.0.0.1:8000",
            "https://hostile.example",
            id="arbitrary-origin",
        ),
    ],
)
def test_mcp_host_and_origin_are_not_policy_refused_with_valid_token(
    host: str, origin: str | None
) -> None:
    """Host/Origin policy is removed; token auth remains the only gate."""
    headers = {
        "Authorization": 'Bearer control-secret-synthetic-credential-0000000000000000',
        "Host": host,
        "Accept": "application/json, text/event-stream",
    }
    if origin is not None:
        headers["Origin"] = origin
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code not in {403, 421}


@pytest.mark.parametrize("path", ["/mcp%2f", "/mcp/%2e", "/other/mcp"])
def test_unauthenticated_alternate_paths_cannot_reach_mcp(path: str) -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.post(
            path,
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            follow_redirects=False,
        )

    assert response.status_code in {401, 404}
    if response.status_code == 404:
        assert "jsonrpc" not in response.text


def test_websocket_requires_register_as_first_message() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect("/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}) as ws:
            ws.send_json({"version": 2, "type": "heartbeat"})
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
    assert exc_info.value.code == 1002


def test_websocket_authenticates_client_bearer_before_token_free_register_frame() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_json(
                {"version": 1, "type": "register", "client_id": "client-a", "relay_contract": 1}
            )
            try:
                registered = ws.receive_json()
            except WebSocketDisconnect as exc:
                pytest.fail(
                    "a valid Client Bearer handshake must accept a token-free register "
                    f"frame (closed with {exc.code})"
                )
    assert registered == {
        "version": 1,
        "type": "registered",
        "client_id": "client-a",
        "server_version": package_version(),
        "relay_contract": 1,
    }


def test_websocket_rejects_invalid_client_bearer_before_processing_register_frame() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws", headers={"Authorization": "Bearer wrong"}
            ):
                pytest.fail("invalid bearer upgraded")
    assert exc_info.value.code == 1008


def test_websocket_rejects_missing_client_bearer_before_any_frame() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws"):
                pytest.fail("missing bearer upgraded")
    assert exc_info.value.code == 1008


def test_websocket_closes_1002_on_contract_mismatched_register() -> None:
    """A register frame with a wrong relay_contract is permanently refused."""
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_json(
                {"version": 1, "type": "register", "client_id": "client-a", "relay_contract": 2}
            )
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1002


def test_websocket_closes_1002_on_contract_mismatched_capabilities() -> None:
    """A capabilities frame with a wrong contract never updates the registry."""
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_json(
                {"version": 1, "type": "register", "client_id": "client-a", "relay_contract": 1}
            )
            registered = ws.receive_json()
            assert registered["type"] == "registered"
            ws.send_json(
                {"version": 1, "type": "capabilities", "tools": [], "relay_contract": 9, "client_version": "0.1.0"}
            )
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1002
            # The client was disconnected: the registry holds no capabilities.
            snapshot = app.state.registry.announced_capabilities
            assert snapshot == frozenset()


def test_websocket_rejects_missing_client_bearer_token_secret_safe() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws"):
                pytest.fail("missing bearer upgraded")

    assert exc_info.value.code == 1008
    assert 'client-secret-synthetic-credential-0000000000000000' not in exc_info.value.reason


def test_websocket_rejects_register_frame_credentials_even_with_valid_bearer() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as ws:
            ws.send_json(
                {
                    "version": 1,
                    "type": "register",
                    "relay_contract": 1,
                    "client_id": "client-a",
                    "token": 'client-secret-synthetic-credential-0000000000000000',
                }
            )
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()

    assert exc_info.value.code == 1002
    assert 'client-secret-synthetic-credential-0000000000000000' not in exc_info.value.reason


def test_websocket_rejects_bad_token_and_second_connection_distinctly() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
        ) as first:
            first.send_json(
                {"version": 1, "type": "register", "client_id": "client-a", "relay_contract": 1}
            )
            assert first.receive_json()["type"] == "registered"
            with client.websocket_connect(
                "/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}
            ) as duplicate:
                duplicate.send_json(
                    {"version": 1, "type": "register", "client_id": "client-a", "relay_contract": 1}
                )
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    duplicate.receive_json()
            assert exc_info.value.code == 1013
            assert exc_info.value.reason == "client already connected"


def test_websocket_rejects_json_integer_exceeding_python_limit() -> None:
    app = create_app(settings())
    oversized_integer = "9" * 4301
    with TestClient(app) as client:
        with client.websocket_connect("/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}) as ws:
            ws.send_text(
                '{"version":1,"type":"heartbeat","integer":' + oversized_integer + "}"
            )
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()

    assert exc_info.value.code == 1002


def test_websocket_processes_capabilities_and_heartbeat(
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
            ws.send_json(
                {
                    "version": 1,
                    "type": "register",
                    "relay_contract": 1,
                    "client_id": "client-a",
                }
            )
            assert ws.receive_json()["type"] == "registered"
            before = app.state.registry.last_heartbeat
            ws.send_json({"version": 2, "type": "heartbeat"})
            assert heartbeat_handled.wait(timeout=2)
            assert app.state.registry.last_heartbeat > before
            ws.send_json(
                {
                    "version": 1,
                    "type": "capabilities",
                    "tools": ["sample.exec"],
                    "relay_contract": 1,
                    "client_version": "0.2.0",
                }
            )
            assert capabilities_handled.wait(timeout=2)


def test_websocket_rejects_oversized_text_binary_and_deep_json() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect("/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}) as ws:
            ws.send_text("x" * (app.state.settings.max_ws_message_bytes + 1))
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
        assert exc_info.value.code == 1009

        with client.websocket_connect("/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}) as ws:
            ws.send_bytes(b"{}")
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
        assert exc_info.value.code == 1002

        with client.websocket_connect("/ws", headers={"Authorization": 'Bearer client-secret-synthetic-credential-0000000000000000'}) as ws:
            ws.send_text("{" * 1100 + "}" * 1100)
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
        assert exc_info.value.code == 1002


def test_server_registry_announces_installed_package_version() -> None:
    app = create_app(
        RelaySettings(
            client_id="client-a", client_token='client-secret-synthetic-credential-0000000000000000', mcp_token='control-secret-synthetic-credential-0000000000000000'
        )
    )
    registry = app.state.registry
    assert registry.server_version == package_version()


def test_server_main_config_attribute_error_surfaces_via_parser_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any invalid attribute surface during config-file startup must reach
    parser.error, not escape as a raw AttributeError traceback."""
    import types

    environment = {
        "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
        "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
    }
    monkeypatch.setattr("mcp_relay.server.os.environ", environment)

    def fake_load(path: object, env: object = None) -> types.SimpleNamespace:
        return types.SimpleNamespace(settings=object())  # no bind attributes

    monkeypatch.setattr(server_module, "load_server_runtime", fake_load)

    with pytest.raises(SystemExit) as excinfo:
        main(["--config", "/tmp/does-not-matter.yaml"])

    assert excinfo.value.code == 2


def test_server_startup_logs_one_line_effective_binds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    environment = {
        "RELAY_MCP_TOKEN": 'mcp-secret-synthetic-credential-0000000000000000',
        "RELAY_CLIENT_TOKEN": 'client-secret-synthetic-credential-0000000000000000',
        "RELAY_SERVER_MCP_HOST": "127.0.0.1",
        "RELAY_SERVER_MCP_PORT": "8000",
        "RELAY_SERVER_CLIENT_HOST": "0.0.0.0",
        "RELAY_SERVER_CLIENT_PORT": "8001",
    }
    monkeypatch.setattr("mcp_relay.server.os.environ", environment)
    monkeypatch.setattr(server_module, "_run_relay", lambda settings: None)

    with caplog.at_level(logging.INFO, logger="mcp_relay.server"):
        main([])

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "mcp_relay.server"
    ]
    assert messages == ["mcp on 127.0.0.1:8000, client on 0.0.0.0:8001"]
