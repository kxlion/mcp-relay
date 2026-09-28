"""Outbound Linux Relay client and its deliberately small local configuration."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import secrets
import signal
import stat
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, AsyncContextManager, Callable, Protocol
from urllib.parse import urlparse

import websockets
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)

from . import json_bounds
from .config import load_client_admin_setting, load_client_settings
from .control import Control
from .diagnostics import debug as _debug_log
from .diagnostics import error as _error_log
from .diagnostics import info as _info_log
from .diagnostics import set_log_file as _set_log_file
from .environment import UnknownRelayEnvironmentError
from .mcp_catalog import ClientCatalog
from .mcp_command import CommandError, execute_command
from .mcp_hub import McpHub, production_transport_factory
from .mcp_registry import lookup_registry_server
from .protocol import (
    MAX_TOKEN_LENGTH,
    MIN_TOKEN_LENGTH,
    OP_MCP_COMMAND,
    RELAY_CONTRACT,
    TOKEN_PATTERN,
    Cancel,
    Capabilities,
    Catalog,
    ClientError,
    ClientResult,
    ErrorDetail,
    Heartbeat,
    InvokeMessage,
    Registered,
    parse_server_message,
)
from .providers.base import ProviderResultTooLargeError, bounded_result
from .version import bounded_version_label, package_version


class ConfigurationError(ValueError):
    """A deliberately non-descriptive error for local client configuration."""

    def __init__(self) -> None:
        super().__init__("invalid client configuration")


def _debug_configuration_validation(error: ValidationError) -> None:
    """Report only rejected field locations on the debug log level."""
    locations = sorted(
        ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        for item in error.errors()
    )
    _debug_log("client configuration rejected fields: " + ", ".join(locations))


class ProtocolIncompatibleError(ConnectionError):
    """The Server closed the connection over a relay-contract mismatch.

    Raised when the WebSocket closes with code 1002 and the
    ``protocol_incompatible`` diagnostic. This is a permanent error for the
    current Server build: automatic reconnection is stopped.
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__("relay protocol incompatible")
        self.reason = reason


def _operator_client_info(message: str) -> None:
    """Emit concise lifecycle information without enabling native diagnostics."""
    _info_log(message)


def _operator_client_error(message: str) -> None:
    """Emit a sanitized terminal-failure event (closed codes only)."""
    _error_log(message)


def safe_server_target(value: str) -> str:
    """Render only the scheme, host, and port from a configured Relay URL."""
    try:
        parsed = urlparse(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<configured Relay>"
    if not parsed.scheme or not host:
        return "<configured Relay>"
    display_host = f"[{host}]" if ":" in host else host
    display_port = f":{port}" if port is not None else ""
    return f"{parsed.scheme}://{display_host}{display_port}"


class ClientSettings(BaseModel):
    """Settings controlled only by the local operator, never by INVOKE frames.

    Deliberately minimal: every former tunable (timeouts, reconnect curve,
    message sizes) is a protocol constant now. Sizes live in
    ``json_bounds`` (override-able via RELAY_* env for debugging); timings
    are fixed constants below.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    server_url: str = Field(json_schema_extra={"env": "RELAY_URL"})
    client_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
        json_schema_extra={"env": "RELAY_CLIENT_ID"},
    )
    client_token: SecretStr = Field(
        repr=False,
        min_length=MIN_TOKEN_LENGTH,
        max_length=MAX_TOKEN_LENGTH,
        json_schema_extra={"env": "RELAY_CLIENT_TOKEN"},
    )
    workspace: Path = Field(json_schema_extra={"env": "RELAY_CLIENT_WORKSPACE"})

    @field_validator("client_token")
    @classmethod
    def valid_client_token(cls, value: SecretStr) -> SecretStr:
        # SecretStr's own length bounds apply; check the actual credential
        # without adding the rejected value to errors or debug logs.
        if not re.fullmatch(TOKEN_PATTERN, value.get_secret_value()):
            raise ValueError("client token must contain printable ASCII without spaces")
        return value

    def __init__(self, /, **data: object) -> None:
        try:
            super().__init__(**data)
        except ValidationError as error:
            # Pydantic's default rendering includes rejected input values.  Those
            # values can be credentials, so never expose the original error.
            _debug_configuration_validation(error)
            raise ConfigurationError() from None

    @classmethod
    def model_validate(cls, *args: object, **kwargs: object) -> ClientSettings:
        try:
            return super().model_validate(*args, **kwargs)
        except ValidationError as error:
            _debug_configuration_validation(error)
            raise ConfigurationError() from None

    @classmethod
    def model_validate_json(cls, *args: object, **kwargs: object) -> ClientSettings:
        try:
            return super().model_validate_json(*args, **kwargs)
        except ValidationError as error:
            _debug_configuration_validation(error)
            raise ConfigurationError() from None

    @classmethod
    def model_validate_strings(cls, *args: object, **kwargs: object) -> ClientSettings:
        try:
            return super().model_validate_strings(*args, **kwargs)
        except ValidationError as error:
            _debug_configuration_validation(error)
            raise ConfigurationError() from None

    @field_validator("server_url")
    @classmethod
    def valid_server_url(cls, value: str) -> str:
        try:
            parsed = urlparse(value)
            parsed.port
        except ValueError as error:
            raise ValueError("invalid server_url") from error
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("server_url must be a ws:// or wss:// URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("server_url must not include userinfo")
        # The endpoint path is intentionally not part of the configuration
        # contract; a future Relay protocol may move it.
        if parsed.fragment:
            raise ValueError("server_url must not include a fragment")
        return value

    @field_validator("workspace")
    @classmethod
    def local_workspace(cls, value: Path) -> Path:
        if not value.is_absolute() or not value.is_dir() or value.is_symlink():
            raise ValueError("workspace must be an absolute existing non-symlink directory")
        return value.resolve(strict=True)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> ClientSettings:
        try:
            env = os.environ if environ is None else environ
            from .environment import validate_relay_environment

            validate_relay_environment(env)
            values = _canonical_client_values(env)
            return cls(**values)
        except UnknownRelayEnvironmentError:
            raise
        except (ConfigurationError, OSError, ValueError, TypeError):
            raise ConfigurationError() from None




# Connection timing constants. Deliberately not configurable: they encode
# the reconnect/heartbeat contract and no deployment ever needed to tune
# them. Debugging connection issues goes through the logs, not timer knobs.
HEARTBEAT_INTERVAL_SECONDS: float = 15.0
RECONNECT_MIN_SECONDS: float = 0.1
RECONNECT_MAX_SECONDS: float = 5.0
STABLE_SESSION_SECONDS: float = 30.0
#: Delay that coalesces a burst of hub changes into one catalog frame.
CATALOG_COALESCE_SECONDS: float = 0.05
#: Room kept inside a WebSocket frame for the catalog envelope.
CATALOG_FRAME_MARGIN_BYTES: int = 64 * 1024

_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

def _token_from_environment(env: Mapping[str, str], *, token_key: str) -> str:
    token = env.get(token_key)
    if not token:
        raise ConfigurationError()
    return token


def _canonical_client_values(
    env: Mapping[str, str]
) -> dict[str, object]:
    url = env.get("RELAY_URL")
    workspace_value = env.get("RELAY_CLIENT_WORKSPACE")
    if not url or not workspace_value:
        raise ConfigurationError()
    token = _token_from_environment(env, token_key="RELAY_CLIENT_TOKEN")
    workspace = _validated_workspace(Path(workspace_value))
    client_id = _load_or_create_client_id(workspace, env.get("RELAY_CLIENT_ID"))
    values: dict[str, object] = {
        "server_url": url,
        "client_id": client_id,
        "client_token": token,
        "workspace": workspace,
    }
    return values


def _validated_workspace(path: Path) -> Path:
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ValueError("workspace must be an absolute existing non-symlink directory")
    return path.resolve(strict=True)


def _validate_client_id(value: str) -> str:
    if not _CLIENT_ID_PATTERN.fullmatch(value):
        raise ValueError("invalid client identity")
    return value


def _private_local_path(path: Path, *, directory: bool) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("client identity path must not be a symlink")
    if directory:
        valid_type = stat.S_ISDIR(info.st_mode)
        expected_mode = 0o700
    else:
        valid_type = stat.S_ISREG(info.st_mode)
        expected_mode = 0o600
    if not valid_type or (
        os.name != "nt"
        and (
            stat.S_IMODE(info.st_mode) != expected_mode
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        )
    ):
        raise ValueError("client identity path is not private")
    return info


def _ensure_client_state_dir(workspace: Path) -> Path:
    state_dir = workspace / ".mcp-relay"
    created = False
    try:
        state_dir.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    if created:
        try:
            os.chmod(state_dir, 0o700)
        except OSError:
            state_dir.rmdir()
            raise
    _private_local_path(state_dir, directory=True)
    return state_dir


def _read_client_id_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (
            os.name != "nt"
            and (
                stat.S_IMODE(info.st_mode) != 0o600
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            )
        ) or info.st_size > 128:
            raise ValueError("client identity file is not private")
        value = os.read(fd, 129).decode("utf-8").strip()
    finally:
        os.close(fd)
    return _validate_client_id(value)


def _create_client_id_file(path: Path, value: str) -> str:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = _read_client_id_file(path)
        if existing != value:
            raise ValueError("existing client identity differs")
        return existing
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        encoded = value.encode("utf-8")
        written = 0
        while written < len(encoded):
            count = os.write(fd, encoded[written:])
            if count <= 0:
                raise OSError("could not persist client identity")
            written += count
        os.fsync(fd)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)
    return value


def _load_or_create_client_id(workspace: Path, configured: str | None) -> str:
    selected = _validate_client_id(configured) if configured else None
    state_dir = _ensure_client_state_dir(workspace)
    identity_path = state_dir / "client-id"
    try:
        _private_local_path(identity_path, directory=False)
    except FileNotFoundError:
        if selected is None:
            selected = "client-" + secrets.token_hex(16)
        return _create_client_id_file(identity_path, selected)
    existing = _read_client_id_file(identity_path)
    if selected is not None and selected != existing:
        raise ValueError("existing client identity differs")
    return existing


class TextSocket(Protocol):
    async def send(self, payload: str) -> None: ...
    async def recv(self) -> str: ...


def _connection_options_for(
    settings: ClientSettings,
    connector: Callable[..., Any],
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "max_size": json_bounds.MAX_WS_MESSAGE_BYTES,
        "proxy": None,
    }
    headers = {"Authorization": "Bearer " + settings.client_token.get_secret_value()}
    try:
        parameters = inspect.signature(connector).parameters
    except (TypeError, ValueError):
        parameter_names: set[str] = set()
    else:
        parameter_names = set(parameters)
    header_option = (
        "additional_headers"
        if "additional_headers" in parameter_names or "extra_headers" not in parameter_names
        else "extra_headers"
    )
    options[header_option] = headers
    return options


async def check_connection(
    settings: ClientSettings,
    *,
    connector: Callable[..., AsyncContextManager[TextSocket]] | None = None,
) -> None:
    """Verify reachability and authentication with the existing register exchange."""
    connect = connector or websockets.connect
    async with connect(
        settings.server_url,
        **_connection_options_for(settings, connect),
    ) as socket:
        await socket.send(
            json.dumps(
                {
                    "version": 1,
                    "type": "register",
                    "client_id": settings.client_id,
                    "relay_contract": RELAY_CONTRACT,
                },
                separators=(",", ":"),
            )
        )
        raw = await socket.recv()
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > (
            json_bounds.MAX_WS_MESSAGE_BYTES
        ):
            raise ConnectionError("invalid registration response")
        try:
            registered = parse_server_message(json.loads(raw))
        except (TypeError, ValueError) as exc:
            raise ConnectionError("invalid registration response") from exc
        if not isinstance(registered, Registered) or registered.client_id != settings.client_id:
            raise ConnectionError("Relay Server rejected registration")


class RelayClient:
    """One outbound connection; a received cancel can never yield a late result."""

    def __init__(
        self,
        settings: ClientSettings,
        *,
        control: Control | None = None,
        catalog: ClientCatalog | None = None,
        hub: McpHub | None = None,
        connector: Callable[..., AsyncContextManager[TextSocket]] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings
        self.catalog = catalog if catalog is not None else ClientCatalog()
        self.hub = hub
        self.control = control or Control(
            hub=hub, catalog=self.catalog, client_version="unknown"
        )
        self._close_task: asyncio.Task[None] | None = None
        # The initial MCP reconciliation, owned here and stopped in ``aclose``.
        self._startup_task: asyncio.Task[None] | None = None
        self._closed = False
        self._stop_event = asyncio.Event()
        self._catalog_dirty = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._connector = connector or websockets.connect
        self._session_registered = False
        self._registered_at: float | None = None
        # Announced by the Server in ``registered``; never Client-supplied.
        self._server_version = "unknown"
        self._client_version = bounded_version_label(package_version()) or "unknown"
        self._monotonic = monotonic or time.monotonic

    @property
    def server_version(self) -> str:
        """Package version announced by the Relay Server, or ``"unknown"``."""
        return self._server_version

    @property
    def client_version(self) -> str:
        """This Client's installed package version, or ``"unknown"``."""
        return self._client_version

    def _report_version_skew(self) -> None:
        """Emit one bounded operator line when Server and Client versions differ."""
        if "unknown" in (self._client_version, self._server_version):
            return
        if self._client_version == self._server_version:
            return
        _operator_client_info(
            "version skew detected: "
            f"server {self._server_version}, client {self._client_version}"
        )

    def stop(self) -> None:
        self._stop_event.set()

    def catalog_changed(self) -> None:
        """Refresh the catalog from the hub and schedule a push to the Server."""
        if self.hub is not None:
            self.hub.publish_catalog(self.catalog)
        self._catalog_dirty.set()

    def start_initial_reconciliation(self) -> asyncio.Task[None]:
        """Start local MCP servers without delaying the control connection.

        Cancelling mid-spawn is safe: the hub closes the in-flight transport
        and reports the alias ``spawn_cancelled``; the YAML is unchanged.
        """
        if self._startup_task is not None and not self._startup_task.done():
            raise RuntimeError("initial reconciliation already started")
        self._startup_task = asyncio.create_task(self._initial_reconciliation())
        return self._startup_task

    async def _initial_reconciliation(self) -> None:
        if self.hub is None:
            return
        try:
            await self.hub.reconcile_all()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _debug_log(f"hub startup incomplete: {type(error).__name__}")
        finally:
            self.catalog_changed()

    def _connection_options(self) -> dict[str, Any]:
        return _connection_options_for(self.settings, self._connector)

    async def run(self) -> None:
        try:
            delay = RECONNECT_MIN_SECONDS
            while not self._stop_event.is_set():
                self._session_registered = False
                self._registered_at = None
                self._server_version = "unknown"
                connection_open = False
                try:
                    _operator_client_info(
                        f"connection attempt to {safe_server_target(self.settings.server_url)}"
                    )
                    async with self._connector(
                        self.settings.server_url,
                        **self._connection_options(),
                    ) as socket:
                        connection_open = True
                        _operator_client_info("WebSocket connection established")
                        await self.run_session(socket)
                except asyncio.CancelledError:
                    raise
                except ProtocolIncompatibleError as error:
                    # Retrying can never succeed against the same Server build.
                    _operator_client_info(
                        "Relay protocol is incompatible with the Server; "
                        "stopping automatic reconnection"
                    )
                    _debug_log(f"client protocol incompatible: reason={error.reason}")
                    self.stop()
                except Exception as error:
                    if self._session_registered:
                        _operator_client_info("Relay disconnected; reconnecting")
                    elif connection_open:
                        _operator_client_info(
                            "registration was rejected or closed before authentication; retrying"
                        )
                    else:
                        _operator_client_info(
                            "connection or authentication failed; retrying"
                        )
                    _debug_log(f"client reconnect: {type(error).__name__}")
                else:
                    if self._session_registered and not self._stop_event.is_set():
                        _operator_client_info("Relay disconnected; reconnecting")
                    elif connection_open and not self._stop_event.is_set():
                        _operator_client_info("registration was rejected; retrying")
                if self._session_was_stable():
                    delay = RECONNECT_MIN_SECONDS
                if not self._stop_event.is_set():
                    _operator_client_info(
                        f"retrying in {delay:g}s (maximum {RECONNECT_MAX_SECONDS:g}s)"
                    )
                    await self._sleep_or_stop(delay)
                    delay = min(delay * 2, RECONNECT_MAX_SECONDS)
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Stop the reconciliation and every local MCP server exactly once."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._aclose_owned())
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                if self._close_task.done():
                    break
        if cancellation is not None:
            raise cancellation

    async def _aclose_owned(self) -> None:
        # Stop the reconciliation first so a mid-spawn cancellation closes the
        # in-flight transport before the hub is torn down.
        startup, self._startup_task = self._startup_task, None
        if startup is not None:
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
        if self.hub is not None:
            await asyncio.gather(self.hub.aclose(), return_exceptions=True)

    def _session_was_stable(self) -> bool:
        """Only reset after a registered connection outlives the local threshold."""
        return (
            self._registered_at is not None
            and self._monotonic() - self._registered_at >= STABLE_SESSION_SECONDS
        )

    async def _sleep_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def run_session(self, socket: TextSocket) -> None:
        await self._send(
            socket,
            {
                "version": 1,
                "type": "register",
                "client_id": self.settings.client_id,
                "relay_contract": RELAY_CONTRACT,
            },
        )
        registered = await self._receive(socket)
        if not isinstance(registered, Registered) or registered.client_id != self.settings.client_id:
            raise ValueError("server did not confirm registration")
        if registered.relay_contract != RELAY_CONTRACT:
            raise ProtocolIncompatibleError("relay contract mismatch")
        self._server_version = registered.server_version or "unknown"
        self._session_registered = True
        self._registered_at = self._monotonic()
        _operator_client_info(
            f"Connected to Relay Server version {self._server_version}"
        )
        self._report_version_skew()
        _operator_client_info(
            f"authenticated registration succeeded for client {self.settings.client_id}"
        )
        await self._send(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                client_version=self._client_version,
                admin=self.control.admin_enabled,
            ).model_dump(mode="json"),
        )
        self._catalog_dirty.set()
        heartbeat = asyncio.create_task(self._heartbeat(socket))
        pusher = asyncio.create_task(self._push_catalog(socket))
        action: asyncio.Task[None] | None = None
        action_request_id: str | None = None
        cancelled_requests: set[str] = set()
        receive: asyncio.Task[object] = asyncio.create_task(self._receive(socket))
        stopping = asyncio.create_task(self._stop_event.wait())
        try:
            while not self._stop_event.is_set():
                wait_for: set[asyncio.Task[Any]] = {receive, stopping, pusher, heartbeat}
                if action is not None:
                    wait_for.add(action)
                done, _ = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)
                if stopping in done:
                    break
                for background in (pusher, heartbeat):
                    if background in done:
                        await background  # re-raise the send failure
                        raise ConnectionError("session task ended")
                if action is not None and action in done:
                    await action
                    action = None
                    action_request_id = None
                if receive in done:
                    message = receive.result()
                    receive = asyncio.create_task(self._receive(socket))
                    if isinstance(message, InvokeMessage):
                        if action is not None:
                            await self._send_error(socket, message.request_id, "busy", "an action is already running")
                        else:
                            action = asyncio.create_task(
                                self._perform(socket, message, cancelled_requests)
                            )
                            action_request_id = message.request_id
                    elif isinstance(message, Cancel):
                        if action is not None and action_request_id == message.request_id:
                            cancelled_requests.add(message.request_id)
                            action.cancel()
                            await asyncio.gather(action, return_exceptions=True)
                            cancelled_requests.discard(message.request_id)
                            action = None
                            action_request_id = None
                    else:
                        raise ValueError("unexpected server message")
        finally:
            tasks = [heartbeat, pusher, receive, stopping]
            if action is not None:
                if action_request_id is not None:
                    cancelled_requests.add(action_request_id)
                tasks.append(action)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _heartbeat(self, socket: TextSocket) -> None:
        while not self._stop_event.is_set():
            await self._sleep_or_stop(HEARTBEAT_INTERVAL_SECONDS)
            if not self._stop_event.is_set():
                await self._send(socket, Heartbeat(version=2, type="heartbeat").model_dump(mode="json"))

    async def _push_catalog(self, socket: TextSocket) -> None:
        """Send the catalog on session start and after every effective change."""
        # A fresh registration starts with an empty catalog on the Server.
        last_sent: list[dict[str, Any]] = []
        while True:
            await self._catalog_dirty.wait()
            # Coalesce a burst of hub changes into one frame.
            await asyncio.sleep(CATALOG_COALESCE_SECONDS)
            self._catalog_dirty.clear()
            tools = self.catalog.build(max_bytes=_catalog_budget())
            if tools == last_sent:
                continue
            await self._send(
                socket,
                Catalog(version=2, type="catalog", tools=tools).model_dump(
                    mode="json", exclude_none=True
                ),
            )
            last_sent = tools
            _operator_client_info(f"tool catalog published ({len(tools)} tools)")

    async def _perform(
        self,
        socket: TextSocket,
        message: InvokeMessage,
        cancelled_requests: set[str],
    ) -> None:
        target = self._log_target(message)
        try:
            if message.tool_name == OP_MCP_COMMAND:
                _debug_log(f"mcp.command start: request_id={message.request_id}{target}")
                result = await execute_command(message.arguments, catalog=self.catalog)
            else:
                payload = await self.control.invoke(
                    message.tool_name,
                    dict(message.arguments),
                    request_id=message.request_id,
                )
                result = bounded_result(
                    {
                        "content": [{"type": "text", "text": json.dumps(payload)}],
                        "structuredContent": payload,
                    }
                )
            if message.request_id in cancelled_requests:
                # A provider that swallows cancellation must not yield a late result.
                return
            await self._send(
                socket,
                ClientResult(
                    version=2,
                    type="result",
                    request_id=message.request_id,
                    result=result,
                ).model_dump(mode="json", by_alias=True, exclude_none=True),
            )
            if message.tool_name == OP_MCP_COMMAND:
                _operator_client_info(
                    f"mcp.command done: request_id={message.request_id}{target} "
                    f"isError={str(result.is_error).lower()}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if message.request_id in cancelled_requests:
                return
            detail = _command_error(error, message.tool_name)
            if message.tool_name == OP_MCP_COMMAND:
                _operator_client_error(
                    f"mcp.command failed: request_id={message.request_id}{target} "
                    f"code={detail.code} execution_state={detail.execution_state}"
                )
            await self._send(
                socket,
                ClientError(
                    version=2,
                    type="error",
                    request_id=message.request_id,
                    error=ErrorDetail(**detail.to_payload()),
                ).model_dump(mode="json"),
            )

    def _log_target(self, message: InvokeMessage) -> str:
        """Alias/tool suffix for logs, only for names already in the catalog."""
        if message.tool_name != OP_MCP_COMMAND:
            return ""
        alias = message.arguments.get("alias")
        tool = message.arguments.get("tool")
        record = self.catalog.records.get(alias) if isinstance(alias, str) else None
        if record is None:
            return ""
        known_tool = any(d.name == tool for d in record.descriptors)
        return f" alias={alias}" + (f" tool={tool}" if known_tool else "")

    async def _receive(self, socket: TextSocket) -> object:
        try:
            text = await socket.recv()
        except websockets.exceptions.ConnectionClosed as closed:
            received = closed.rcvd
            if (
                received is not None
                and received.code == 1002
                and "protocol_incompatible" in (received.reason or "")
            ):
                raise ProtocolIncompatibleError(received.reason) from None
            raise
        if not isinstance(text, str) or len(text.encode("utf-8")) > (
            json_bounds.MAX_WS_MESSAGE_BYTES
        ):
            _debug_log(
                "provider frame failure: category=frame-oversized direction=inbound"
            )
            raise ValueError("invalid server frame")
        return parse_server_message(json.loads(text))

    async def _send(self, socket: TextSocket, message: object) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        payload_bytes = len(payload.encode("utf-8"))
        if payload_bytes > json_bounds.MAX_WS_MESSAGE_BYTES:
            _debug_log(
                "provider frame failure: category=frame-oversized direction=outbound"
            )
            if isinstance(message, Mapping) and message.get("type") == "result":
                raise ProviderResultTooLargeError(
                    "RELAY_MAX_WS_MESSAGE_BYTES: "
                    f"{json_bounds.MAX_WS_MESSAGE_BYTES} < payload: "
                    f"{payload_bytes} bytes"
                )
            raise ValueError("outbound message exceeds limit")
        async with self._write_lock:
            await socket.send(payload)

    async def _send_error(
        self, socket: TextSocket, request_id: str, code: str, message: str
    ) -> None:
        await self._send(
            socket,
            ClientError(
                version=2,
                type="error",
                request_id=request_id,
                error=ErrorDetail(
                    code=code, message=message, execution_state="not_started"
                ),
            ).model_dump(mode="json"),
        )


def _catalog_budget() -> int:
    """Bytes available to catalog entries inside one WebSocket frame."""
    return max(0, json_bounds.MAX_WS_MESSAGE_BYTES - CATALOG_FRAME_MARGIN_BYTES)


def _command_error(error: Exception, operation: str) -> CommandError:
    """Map a local failure onto the closed {code, message, execution_state}."""
    if isinstance(error, CommandError):
        return error
    if isinstance(error, ProviderResultTooLargeError):
        detail = getattr(error, "detail", None)
        return CommandError(
            "result_too_large",
            f"'{operation}': {detail}" if detail else ProviderResultTooLargeError.wire_message,
            execution_state="unknown",
        )
    _debug_log(
        "client invocation failed: "
        f"operation={operation} exception={type(error).__name__}"
    )
    state = "unknown" if operation == OP_MCP_COMMAND else "not_started"
    return CommandError("execution_failed", "local action failed", execution_state=state)


async def _run_with_signal_handlers(client: RelayClient) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, client.stop)
        except NotImplementedError:
            # Windows event loops do not expose add_signal_handler(). The
            # default console handling still interrupts the process safely.
            continue
    await client.run()


def build_client(
    settings: ClientSettings, *, config_path: Path | None = None
) -> RelayClient:
    """Wire the hub, catalog and control for one Client process.

    Without a YAML file there is no hub: no local MCP servers and no
    administration, only status.
    """
    catalog = ClientCatalog()
    hub: McpHub | None = None
    admin = False
    if config_path is not None:
        hub = McpHub(
            config_path,
            settings.workspace,
            transport_factory=production_transport_factory,
            source_resolver=_resolve_source,
        )
        admin = load_client_admin_setting(config_path)
    control = Control(
        hub=hub,
        catalog=catalog,
        client_version=bounded_version_label(package_version()) or "unknown",
        admin_enabled=admin,
    )
    client = RelayClient(settings, control=control, catalog=catalog, hub=hub)
    if hub is not None:
        hub.bind_on_change(client.catalog_changed)
    return client


async def _resolve_source(source: str, version: str | None) -> Any:
    return await lookup_registry_server(source, version=version)


async def _run_client(
    settings: ClientSettings,
    *,
    config_path: Path | None = None,
) -> None:
    client = build_client(settings, config_path=config_path)
    try:
        client.start_initial_reconciliation()
        await _run_with_signal_handlers(client)
    finally:
        await client.aclose()


def main(
    argv: Sequence[str] | None = None,
) -> None:
    from .environment import UnknownRelayEnvironmentError

    parser = argparse.ArgumentParser(description="MCP Relay outbound client")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--client-token", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    _set_log_file(Path("client.log"))
    if args.client_token is not None:
        parser.error(
            "--client-token is unsafe; use RELAY_CLIENT_TOKEN, .env, "
            "or the secure onboarding input options"
        )
    try:
        if args.config is not None:
            settings = load_client_settings(args.config)
        else:
            # Env-only startup (no config file): still resolve the RELAY_MAX_*
            # overrides — the single override mechanism, env-direct here.
            json_bounds.resolve_size_overrides(os.environ)
            settings = ClientSettings.from_environment()
    except UnknownRelayEnvironmentError as exc:
        parser.error(str(exc))
    except (ConfigurationError, ValueError):
        parser.error("invalid client configuration")
    try:
        asyncio.run(_run_client(settings, config_path=args.config))
    except (ConfigurationError, ValueError):
        parser.error("invalid client configuration")


if __name__ == "__main__":
    main()
