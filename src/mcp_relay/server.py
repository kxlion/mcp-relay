"""FastAPI application factories for the Relay MCP and WebSocket surfaces.

Two explicit listener apps share one ``RelayRegistry``:

- :func:`create_mcp_app` — the MCP surface: ``/mcp`` and MCP Bearer
  authentication only, owning the facade's session manager via its lifespan;
- :func:`mcp_relay.ws_server.create_ws_app` — the WS surface: ``/ws`` and
  Client Bearer authentication only.

:func:`_create_listener_apps` builds the registry once and hands the SAME
instance to both factories. Runtime startup serves those apps on independently
reserved sockets.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import os
import re
import socket
import sys
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import FastAPI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import json_bounds
from .auth import credentials_match
from .config import load_server_runtime
from .diagnostics import console_level as _console_level
from .diagnostics import format_line as _format_line
from .diagnostics import set_log_file as _set_log_file
from .diagnostics import write_file_line as _write_file_line
from .environment import UnknownRelayEnvironmentError
from .mcp_facade import create_mcp_facade, create_mcp_http_app
from .protocol import MAX_TOKEN_LENGTH, MIN_TOKEN_LENGTH, TOKEN_PATTERN, ClientId
from .registry import RelayRegistry
from .version import package_version
from .ws_server import create_ws_app

_BIND_FLAG_DESTS = ("mcp_host", "mcp_port", "client_host", "client_port")
_BIND_FLAG_NAMES = tuple(f"--{dest.replace('_', '-')}" for dest in _BIND_FLAG_DESTS)
logger = logging.getLogger("mcp_relay.server")


def _uvicorn_logging_config() -> dict[str, object]:
    """Route all Server runtime logging through the unified operator format.

    Uvicorn (``uvicorn``, ``uvicorn.error``, ``uvicorn.access``), ``mcp.*``
    and ``mcp_relay.*`` log records go to stderr through one handler
    formatted like every diagnostics line (``...Z [LEVEL] message``), gated by
    ``LOG_LEVEL``. Those records are additionally mirrored into
    ``server.log`` by the diagnostics file bridge installed at startup.
    """
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    loggers = config["loggers"]
    if not isinstance(loggers, dict):
        raise TypeError("Uvicorn logging config has invalid logger settings")
    config["disable_existing_loggers"] = False
    config["formatters"]["relay"] = {
        "()": "mcp_relay.server.UnifiedOperatorFormatter",
    }
    config["handlers"]["relay"] = {
        "class": "logging.StreamHandler",
        "formatter": "relay",
        "stream": "ext://sys.stderr",
        "level": _console_level(),
    }
    config["handlers"]["relay_file_bridge"] = {
        "()": _DiagnosticsFileBridge,
        "formatter": "relay",
    }
    loggers["mcp"] = {
        "handlers": ["relay", "relay_file_bridge"],
        "level": "INFO",
        "propagate": False,
    }
    # Route uvicorn and relay runtime loggers through the same diagnostics
    # bridge. Handlers are attached explicitly per logger (never recursively
    # to root); dictConfig replaces any stock uvicorn handlers on these
    # loggers, so there is exactly one emission per event per sink. DEBUG is
    # allowed to reach the loggers — the stderr handler still gates the
    # console on LOG_LEVEL and the file sink always receives every level.
    # uvicorn and uvicorn.error are capped at INFO: their TRACE records are
    # raw handshake header dumps (including Authorization Bearer tokens) and
    # must never reach any sink. uvicorn.access stays at DEBUG so the status
    # classification filter always sees every access record.
    for name, level in (
        ("uvicorn", "INFO"),
        ("uvicorn.error", "INFO"),
        ("uvicorn.access", "DEBUG"),
        ("mcp_relay", "DEBUG"),
    ):
        loggers[name] = {
            "handlers": ["relay", "relay_file_bridge"],
            "level": level,
            "propagate": False,
        }
    # Classify access records from the HTTP status *before* the stderr
    # handler's LOG_LEVEL gate (logger-level filters run before handler
    # level checks in Logger.callHandlers).
    loggers["uvicorn.access"]["filters"] = ["relay_access_level"]
    config["filters"] = {"relay_access_level": {"()": _AccessLevelFilter}}
    return config


class _DiagnosticsFileBridge(logging.Handler):
    """Mirror runtime log records into the diagnostics file sink.

    Declared in the uvicorn logging config so Server-side runtime activity
    (uvicorn, uvicorn.error, uvicorn.access, ``mcp.*`` and ``mcp_relay.*``
    loggers, all configured at DEBUG) reaches ``server.log`` — which would
    otherwise only exist from the first diagnostics emission. The sink owns
    formatting and error containment; ``LOG_LEVEL`` gates only stderr, so
    every mirrored level — DEBUG included — reaches the file.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # pragma: no cover - logging never breaks the relay
            return
        _write_file_line(message)


class _AccessLevelFilter(logging.Filter):
    """Remap ``uvicorn.access`` record levels from the HTTP status in ``args``.

    Attached to the ``uvicorn.access`` logger, so the level is classified from
    the LogRecord fields *before* handler-level gating (``LOG_LEVEL`` on the
    stderr handler) and before formatting. Non-access-shaped records and
    unrecognized statuses keep their native level.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        status: object = None
        if isinstance(args, tuple) and len(args) == 5:
            status = args[4]  # HTTP: (client, method, path, version, status)
        elif isinstance(args, tuple) and len(args) == 3:
            status = args[2]  # WebSocket: (client, path, status)
        levelname, levelno = _access_status_level(status)
        record.levelname = levelname
        record.levelno = levelno
        return True


def _access_status_level(status_code: object) -> tuple[str, int]:
    """Map an HTTP status to the operator level (2xx/3xx INFO, 4xx WARNING, 5xx ERROR)."""
    try:
        status = int(status_code)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ("INFO", logging.INFO)
    if 500 <= status <= 599:
        return ("ERROR", logging.ERROR)
    if 400 <= status <= 499:
        return ("WARNING", logging.WARNING)
    return ("INFO", logging.INFO)


_ACCESS_USERINFO_RE = re.compile(r"//[^/?#]*@")
_ACCESS_COMPONENT_MAX = 256


def _sanitize_access_component(value: object) -> str:
    """Bound and strip control characters from one access-record field."""
    text = str(value)[:_ACCESS_COMPONENT_MAX]
    return "".join(ch if ch.isprintable() else " " for ch in text)


def _sanitize_access_path(full_path: object) -> str:
    """Path without the query string (secrets) or userinfo credentials."""
    text = str(full_path)
    path = text.split("?", 1)[0]
    path = _ACCESS_USERINFO_RE.sub("//", path)
    return _sanitize_access_component(path)


def _format_access_message(record: logging.LogRecord) -> str:
    """Bounded, secret-free summary built from the record's fields.

    Uvicorn HTTP access records carry ``args=(client_addr, method, full_path,
    http_version, status_code)``; WebSocket ones ``(client_addr, path[,
    status])``. Anything else gets a generic summary — never a raw args dump.
    A transport-level HTTP 200 whose MCP payload is an ``isError`` result stays
    INFO: business errors surface through their own loggers, not the access log.
    """
    args = record.args
    if isinstance(args, tuple) and len(args) == 5:
        client, method, full_path, http_version, status = args
        return (
            f"{_sanitize_access_component(client)} {_sanitize_access_component(method)} "
            f"{_sanitize_access_path(full_path)} HTTP/{_sanitize_access_component(http_version)} "
            f"{_sanitize_access_component(status)}"
        )
    # WebSocket records (uvicorn emits '%s - "WebSocket %s" %d' or the
    # 2-arg [accepted] variant). The check inspects the record's format
    # string field — never the rendered message.
    if isinstance(args, tuple) and len(args) in (2, 3) and "WebSocket" in str(record.msg):
        client, full_path = args[0], args[1]
        tail = f" {_sanitize_access_component(args[2])}" if len(args) == 3 else " [accepted]"
        return (
            f"WebSocket {_sanitize_access_path(full_path)}{tail} "
            f"{_sanitize_access_component(client)}"
        )
    return "access event"


class UnifiedOperatorFormatter(logging.Formatter):
    """Format stdlib records as the single operator line of diagnostics."""

    def format(self, record: logging.LogRecord) -> str:
        if record.name == "uvicorn.access":
            message = _format_access_message(record)
            exc_type = record.exc_info[0] if record.exc_info else None
            if exc_type is not None:
                message = f"{message}: {exc_type.__name__}"
        else:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}: {record.exc_info[0].__name__}"
        if record.name.startswith("uvicorn"):
            message = f"{record.name}: {message}"
        return _format_line(record.levelname, message)


class _MCPBearerAuth:
    """Authenticate MCP requests with exactly one bounded Bearer token."""

    def __init__(self, app: ASGIApp, mcp_token: str) -> None:
        self._app = app
        self._expected = f"Bearer {mcp_token}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in {"/mcp", "/mcp/"}:
            values = [
                value
                for name, value in scope.get("headers", [])
                if name.lower() == b"authorization"
            ]
            supplied: str | None = None
            if len(values) == 1 and len(values[0]) <= len(b"Bearer ") + MAX_TOKEN_LENGTH:
                try:
                    supplied = values[0].decode("ascii")
                except UnicodeDecodeError:
                    supplied = None
            if supplied is None or not credentials_match(supplied, self._expected):
                response = JSONResponse(
                    {"detail": "authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


class RelaySettings(BaseModel):
    """Explicit deployment settings; callers supply both independent secrets."""

    model_config = ConfigDict(extra="forbid", strict=True)

    client_token: Annotated[
        str, Field(min_length=MIN_TOKEN_LENGTH, max_length=MAX_TOKEN_LENGTH, pattern=TOKEN_PATTERN, json_schema_extra={"env": "RELAY_CLIENT_TOKEN"})
    ] = Field(repr=False)
    mcp_token: Annotated[
        str, Field(min_length=MIN_TOKEN_LENGTH, max_length=MAX_TOKEN_LENGTH, pattern=TOKEN_PATTERN, json_schema_extra={"env": "RELAY_MCP_TOKEN"})
    ] = Field(repr=False)
    # A server can start before any Client has registered.
    client_id: ClientId | None = None
    mcp_bind_host: str = Field(default="127.0.0.1", json_schema_extra={"env": "RELAY_SERVER_MCP_HOST"})
    mcp_port: Annotated[int, Field(ge=1, le=65535)] = Field(default=8000, json_schema_extra={"env": "RELAY_SERVER_MCP_PORT"})
    client_bind_host: str = Field(default="127.0.0.1", json_schema_extra={"env": "RELAY_SERVER_CLIENT_HOST"})
    client_port: Annotated[int, Field(ge=1, le=65535)] = Field(default=8001, json_schema_extra={"env": "RELAY_SERVER_CLIENT_PORT"})
    max_timeout_seconds: Annotated[float, Field(gt=0, le=3600)] = Field(
        default=30.0, json_schema_extra={"env": "RELAY_MAX_TIMEOUT_SECONDS"}
    )
    cancel_send_timeout_seconds: Annotated[float, Field(ge=0.0001, le=5)] = Field(
        default=0.25, json_schema_extra={"env": "RELAY_CANCEL_SEND_TIMEOUT_SECONDS"}
    )
    # Frame size is a protocol constant shared with the client
    # (json_bounds.MAX_WS_MESSAGE_BYTES, override-able via RELAY_MAX_WS_MESSAGE_BYTES);
    # enforced here before JSON decoding and aligned with uvicorn's ws_max_size.
    # default_factory (not a baked default) so the env override is honored.
    max_ws_message_bytes: Annotated[int, Field(ge=1024)] = Field(
        default_factory=lambda: json_bounds.MAX_WS_MESSAGE_BYTES,
    )
    # Base URL of the official MCP Registry used by relay_registry_search.
    registry_base_url: Annotated[str, Field(pattern=r"^https?://")] = (
        "https://registry.modelcontextprotocol.io"
    )

    def __init__(self, /, **data: object) -> None:
        try:
            super().__init__(**data)
        except ValidationError:
            # Pydantic includes rejected input values in its normal error text.
            # Server settings contain two credentials, so expose no raw input.
            raise ValueError("invalid relay server configuration") from None

    @classmethod
    def model_validate(cls, *args: object, **kwargs: object) -> RelaySettings:
        try:
            return super().model_validate(*args, **kwargs)
        except ValidationError:
            raise ValueError("invalid relay server configuration") from None

    @classmethod
    def model_validate_json(cls, *args: object, **kwargs: object) -> RelaySettings:
        try:
            return super().model_validate_json(*args, **kwargs)
        except ValidationError:
            raise ValueError("invalid relay server configuration") from None

    @classmethod
    def model_validate_strings(cls, *args: object, **kwargs: object) -> RelaySettings:
        try:
            return super().model_validate_strings(*args, **kwargs)
        except ValidationError:
            raise ValueError("invalid relay server configuration") from None

    @field_validator("mcp_bind_host", "client_bind_host")
    @classmethod
    def literal_bind_address(cls, value: str) -> str:
        return str(ip_address(value))

    @model_validator(mode="after")
    def valid_settings(self) -> RelaySettings:
        if self.mcp_bind_host == self.client_bind_host and self.mcp_port == self.client_port:
            raise ValueError("listener ports must be distinct on the same IP address")
        if credentials_match(self.client_token, self.mcp_token):
            raise ValueError("client and control tokens must differ")
        if self.max_ws_message_bytes != json_bounds.MAX_WS_MESSAGE_BYTES:
            raise ValueError(
                "max_ws_message_bytes must match the resolved protocol bound"
            )
        return self


    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> RelaySettings:
        """Load canonical server settings without requiring an Client identity."""
        env = os.environ if environ is None else environ
        try:
            from .environment import validate_relay_environment

            validate_relay_environment(env)
            values: dict[str, object] = {
                "client_token": env["RELAY_CLIENT_TOKEN"],
                "mcp_token": env["RELAY_MCP_TOKEN"],
                "mcp_bind_host": env.get("RELAY_SERVER_MCP_HOST", "127.0.0.1"),
                "mcp_port": int(env.get("RELAY_SERVER_MCP_PORT", "8000")),
                "client_bind_host": env.get("RELAY_SERVER_CLIENT_HOST", "127.0.0.1"),
                "client_port": int(env.get("RELAY_SERVER_CLIENT_PORT", "8001")),
                "max_ws_message_bytes": json_bounds.MAX_WS_MESSAGE_BYTES,
            }
            for field in ("max_timeout_seconds", "cancel_send_timeout_seconds"):
                key = f"RELAY_{field.upper()}"
                if key in env:
                    values[field] = float(env[key])
            return cls(**values)
        except UnknownRelayEnvironmentError:
            raise
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid relay server configuration") from None


def create_mcp_app(registry: RelayRegistry, *, settings: RelaySettings) -> FastAPI:
    """Create the MCP listener app: /mcp and MCP authentication only.

    The app owns the MCP facade and, through its lifespan, the MCP session
    manager for this listener. It serves no WebSocket route and publishes no
    docs or OpenAPI.
    """
    mcp = create_mcp_facade(
        registry=registry,
        timeout_seconds=settings.max_timeout_seconds,
        registry_base_url=settings.registry_base_url,
    )
    mcp_http_app = create_mcp_http_app(mcp)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Single owner: the FastMCP HTTP app's own lifespan runs its session
        # manager exactly once for this listener (Starlette mounts never
        # propagate lifespan events).
        async with mcp_http_app.lifespan(mcp_http_app):
            yield

    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.registry = registry
    app.state.settings = settings
    app.state.mcp = mcp

    # The child owns /mcp directly, avoiding a /mcp -> /mcp/ redirect.
    app.mount(
        "/",
        _MCPBearerAuth(
            mcp_http_app,
            settings.mcp_token,
        ),
    )
    return app


def _create_listener_apps(settings: RelaySettings) -> tuple[FastAPI, FastAPI]:
    """Build the independent listener apps around one shared registry."""
    registry = RelayRegistry(
        client_id=settings.client_id,
        client_token=settings.client_token,
        cancel_send_timeout_seconds=settings.cancel_send_timeout_seconds,
        # Announce the installed package version inside the existing
        # authenticated handshake; the value never comes from the Client and
        # contains no tokens, paths, or other configuration.
        server_version=package_version(),
    )
    mcp_app = create_mcp_app(registry, settings=settings)
    ws_app = create_ws_app(
        registry,
        client_token=settings.client_token,
        max_ws_message_bytes=settings.max_ws_message_bytes,
    )
    return mcp_app, ws_app


class ListenerBindError(RuntimeError):
    """A configured listener address could not be reserved."""


class _PrimaryUvicornServer(uvicorn.Server):
    """Signal-owning listener that lets its peer finish before process exit.

    Uvicorn re-emits every captured signal after its own shutdown. That is safe
    for a single server, but would terminate this process before the paired WS
    server can drain. Keep Uvicorn's normal signal capture and exit handling,
    then consume the re-emission so :func:`_serve_uvicorn_pair` can stop and
    await both listeners.
    """

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        with super().capture_signals():
            try:
                yield
            finally:
                self._captured_signals.clear()


class _SignalFreeUvicornServer(uvicorn.Server):
    """Uvicorn server whose ``serve`` call does not install signal handlers.

    Uvicorn 0.51 wraps ``Server._serve`` in ``Server.capture_signals``. The MCP
    server keeps that default and is the process signal owner; overriding the
    documented hook on the WS server avoids competing process-wide handlers
    while preserving ``Server.serve(sockets=[...])`` for both listeners.
    """

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _bind_listener_socket(host: str, port: int, *, name: str) -> socket.socket:
    """Bind and listen on one exact configured address, without fallback."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    if sys.platform == "win32":
        # Windows SO_REUSEADDR lets another socket bind a port that is already
        # listening; SO_EXCLUSIVEADDRUSE refuses such a bind in both directions.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
        # On POSIX, listening makes the reservation exclusive even with
        # SO_REUSEADDR; the socket stays compatible with
        # asyncio.create_server(sock=...).
        listener.listen()
    except OSError as exc:
        listener.close()
        detail = exc.strerror or "address unavailable"
        raise ListenerBindError(
            f"{name} listener failed to bind {host}:{port}: {detail}"
        ) from exc
    return listener


def _reserve_listener_sockets(
    mcp_host: str, mcp_port: int, client_host: str, client_port: int
) -> tuple[socket.socket, socket.socket]:
    """Atomically reserve the MCP and Client sockets or release both.

    Each listener binds its own configured address, so the two surfaces can
    live on different interfaces (for example loopback MCP with a wildcard
    Client listener). Failure of either bind releases both reservations.
    """
    reserved: list[socket.socket] = []
    try:
        reserved.append(
            _bind_listener_socket(mcp_host, mcp_port, name="MCP")
        )
        reserved.append(
            _bind_listener_socket(client_host, client_port, name="CLIENT")
        )
    except BaseException:
        for listener in reserved:
            listener.close()
        raise
    return reserved[0], reserved[1]


async def _serve_uvicorn_pair(
    mcp_server: uvicorn.Server,
    ws_server: uvicorn.Server,
    mcp_socket: socket.socket,
    ws_socket: socket.socket,
) -> None:
    """Serve both listeners until either one finishes, then stop its peer."""

    async def serve_one(
        server: uvicorn.Server, listener: socket.socket
    ) -> BaseException | None:
        try:
            await server.serve(sockets=[listener])
        except BaseException as exc:
            # Uvicorn may use SystemExit for startup failures. Keep that inside
            # the child task until its peer has been asked to stop cleanly.
            return exc
        return None

    servers = (mcp_server, ws_server)
    tasks = (
        asyncio.create_task(serve_one(mcp_server, mcp_socket)),
        asyncio.create_task(serve_one(ws_server, ws_socket)),
    )
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for server in servers:
            server.should_exit = True
        results = await asyncio.gather(*tasks)

    failures = [
        result
        for result in results
        if result is not None and not isinstance(result, asyncio.CancelledError)
    ]
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("multiple Uvicorn listener failures", failures)


async def _serve_relay(settings: RelaySettings) -> None:
    """Reserve and concurrently serve the independent MCP and Client listeners."""
    mcp_socket, client_socket = _reserve_listener_sockets(
        settings.mcp_bind_host,
        settings.mcp_port,
        settings.client_bind_host,
        settings.client_port,
    )
    try:
        mcp_app, client_app = _create_listener_apps(settings)
        common_config = {
            "ws_max_size": settings.max_ws_message_bytes,
            "log_config": _uvicorn_logging_config(),
        }
        mcp_server = _PrimaryUvicornServer(
            uvicorn.Config(
                mcp_app,
                host=settings.mcp_bind_host,
                port=settings.mcp_port,
                **common_config,
            )
        )
        client_server = _SignalFreeUvicornServer(
            uvicorn.Config(
                client_app,
                host=settings.client_bind_host,
                port=settings.client_port,
                **common_config,
            )
        )
        await _serve_uvicorn_pair(
            mcp_server,
            client_server,
            mcp_socket,
            client_socket,
        )
    finally:
        mcp_socket.close()
        client_socket.close()


def _run_relay(settings: RelaySettings) -> None:
    """Own the single asyncio loop used by both Uvicorn servers."""
    asyncio.run(_serve_relay(settings))


def main(argv: Sequence[str] | None = None) -> None:
    """Run the Relay server from YAML or the canonical server environment."""
    env = os.environ
    parser = argparse.ArgumentParser(description="MCP Relay server")
    parser.add_argument("--config", type=str)
    for name in _BIND_FLAG_NAMES:
        if name.endswith("--port"):
            parser.add_argument(name, type=int)
        else:
            parser.add_argument(name)
    args = parser.parse_args(argv)
    _set_log_file(Path("server.log"))
    explicit_binds = [
        name
        for dest, name in zip(_BIND_FLAG_DESTS, _BIND_FLAG_NAMES, strict=True)
        if getattr(args, dest) is not None
    ]
    if args.config is not None and explicit_binds:
        # A configuration file owns the bind topology; per-listener bind
        # flags conflict with it even when the value equals the default.
        parser.error(
            "--config does not accept per-listener bind flags: "
            + ", ".join(explicit_binds)
        )
    try:
        if args.config is not None:
            runtime = load_server_runtime(args.config, env=env)
            settings = runtime.settings
        else:
            # Env-only startup (no config file): resolve the RELAY_MAX_*
            # overrides first — the single override mechanism, env-direct
            # here — then validate through the same RelaySettings contract
            # that load_server_runtime applies to the config-file path.
            # Explicit per-listener bind flags override the environment.
            json_bounds.resolve_size_overrides(env)
            env_map = dict(env)
            flag_env = {
                "mcp_host": "RELAY_SERVER_MCP_HOST",
                "mcp_port": "RELAY_SERVER_MCP_PORT",
                "client_host": "RELAY_SERVER_CLIENT_HOST",
                "client_port": "RELAY_SERVER_CLIENT_PORT",
            }
            for dest, env_name in flag_env.items():
                value = getattr(args, dest)
                if value is not None:
                    env_map[env_name] = str(value)
            settings = RelaySettings.from_environment(env_map)
    except (KeyError, TypeError, ValueError, OSError, AttributeError):
        # AttributeError stays guarded so any missed attribute surface during
        # startup reaches parser.error, never a raw traceback.
        parser.error("invalid relay server configuration")
    try:
        logger.info(
            "mcp on %s:%d, client on %s:%d",
            settings.mcp_bind_host,
            settings.mcp_port,
            settings.client_bind_host,
            settings.client_port,
        )
        _run_relay(settings)
    except ListenerBindError as exc:
        parser.error(str(exc))
    except AttributeError:
        parser.error("invalid relay server configuration")


def _classify_bind_address(host: str) -> str:
    """Classify a literal bind address for exposure reporting.

    Returns ``"loopback"`` (only this host), ``"wildcard"`` (every
    interface), or ``"specific"`` (one non-loopback interface). The
    classification informs the later LAN-exposed flag; it is derived from
    the address itself, never from configuration text.
    """
    address = ip_address(host)
    if address.is_loopback:
        return "loopback"
    if address.is_unspecified:
        return "wildcard"
    return "specific"


if __name__ == "__main__":
    main()
