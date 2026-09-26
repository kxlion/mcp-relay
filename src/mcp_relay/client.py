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
from collections.abc import Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any, AsyncContextManager, Awaitable, Callable, Protocol, cast
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
from .capabilities.base import (
    CapabilityProviderClient,
    LocalCapability,
)
from .config import load_client_settings
from .diagnostics import debug as _debug_log
from .diagnostics import error as _error_log
from .diagnostics import info as _info_log
from .diagnostics import set_log_file as _set_log_file
from .environment import UnknownRelayEnvironmentError
from .mcp_catalog import ClientCatalog
from .mcp_command import CommandError, execute_command
from .protocol import (
    MAX_TOKEN_LENGTH,
    MIN_TOKEN_LENGTH,
    RELAY_CONTRACT,
    TOKEN_PATTERN,
    Cancel,
    Capabilities,
    ClientError,
    ClientResult,
    ErrorDetail,
    Heartbeat,
    InvokeMessage,
    Registered,
    parse_server_message,
)
from .provider_tools import ProviderToolDescriptor
from .providers.base import (
    ProviderResultTooLargeError,
    ProviderToolClient,
    bounded_arguments,
    bounded_descriptors,
    bounded_result,
)
from .relay_tools import WIRE_OPERATION_NAMES
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


class ProviderUnavailableError(ConnectionError):
    """A selected provider became unavailable during an Client session."""


class ProtocolIncompatibleError(ConnectionError):
    """The Server closed the connection over a relay-contract mismatch.

    Raised when the WebSocket closes with code 1002 and the
    ``protocol_incompatible`` diagnostic. This is a permanent error for the
    current Server build: automatic reconnection is stopped.
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__("relay protocol incompatible")
        self.reason = reason


def _debug_client_phase(phase: str) -> None:
    _debug_log(f"client lifecycle phase: {phase}")


def _operator_client_info(message: str) -> None:
    """Emit concise lifecycle information without enabling native diagnostics."""
    _info_log(message)


def _operator_client_debug(message: str) -> None:
    """Emit a pre-dispatch debug event (file always, stderr under DEBUG)."""
    _debug_log(message)


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
        capabilities: Sequence[LocalCapability] | None = None,
        connector: Callable[..., AsyncContextManager[TextSocket]] | None = None,
        monotonic: Callable[[], float] | None = None,
        provider_clients: Mapping[str, ProviderToolClient] | None = None,
        provider_resolver: Callable[
            [], Awaitable[Mapping[str, ProviderToolClient]]
        ] | None = None,
    ) -> None:
        self.settings = settings
        configured_capabilities = list(capabilities or ())
        self._capabilities = self._index_capabilities(configured_capabilities)
        self._unique_capabilities = tuple(dict.fromkeys(map(id, configured_capabilities)))
        self._capability_objects = {id(item): item for item in configured_capabilities}
        self._provider_clients = dict(provider_clients or {})
        self._provider_resolver = provider_resolver
        self._provider_close_objects = {
            id(client): client for client in self._provider_clients.values()
            if id(client) not in self._capability_objects
        }
        self._provider_routes: dict[
            str, tuple[ProviderToolClient, ProviderToolDescriptor]
        ] = {}
        self._announcement_tools: tuple[str, ...] = ()
        # Third-party execution path: the client catalog holds alias records
        # and route references; commands reserve a route and send exactly once.
        self.catalog: ClientCatalog | None = None
        self._inventory_ready = False
        self._close_task: asyncio.Task[None] | None = None
        # Step 7B: the initial MCP reconciliation, owned by this client and
        # stopped explicitly in ``aclose`` — never an orphan task.
        self._startup_task: asyncio.Task[None] | None = None
        self._closed = False
        self._stop_event = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._connector = connector or websockets.connect
        self._session_registered = False
        self._registered_at: float | None = None
        self._socket: TextSocket | None = None
        # Package version announced by the Relay Server during the handshake.
        # ``unknown`` until a server that omits the field connects (legacy
        # servers) or before the first session; it is never client-supplied.
        self._server_version = "unknown"
        # This Client's installed package version, resolved once. ``unknown``
        # only when the distribution metadata is unavailable.
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

    @property
    def connection_metadata(self) -> dict[str, str]:
        """Structured, secret-free metadata about the current connection."""
        return {"server_version": self._server_version}

    def _report_version_skew(self) -> None:
        """Emit one bounded operator line when Server and Client versions differ."""
        if self._client_version == "unknown" or self._server_version == "unknown":
            return
        if self._client_version == self._server_version:
            return
        _operator_client_info(
            "version skew detected: "
            f"server {self._server_version}, client {self._client_version}"
        )

    def stop(self) -> None:
        self._stop_event.set()

    def start_initial_reconciliation(
        self, reconciliation: Callable[[], Coroutine[None, None, None]]
    ) -> asyncio.Task[None]:
        """Own the initial MCP reconciliation as an explicit background task.

        Step 7B: the control-channel connection and its heartbeat must
        never wait for the (possibly long) local MCP startup. The
        reconciliation runs as a task owned by this client; ``aclose``
        cancels and awaits it exactly once, so no orphan task survives
        shutdown. Cancelling the reconciliation mid-spawn is safe: the hub
        closes the in-flight transport and reports the alias unavailable
        (``spawn_cancelled``), keeping the committed YAML unchanged.
        """
        if self._startup_task is not None and not self._startup_task.done():
            raise RuntimeError("initial reconciliation already started")
        self._startup_task = asyncio.create_task(reconciliation())
        return self._startup_task

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
                    _debug_client_phase("capabilities-start")
                    await self._start_capabilities()
                    _debug_client_phase("capabilities-ready")
                    _operator_client_info(
                        f"connection attempt to {safe_server_target(self.settings.server_url)}"
                    )
                    _debug_client_phase("connect")
                    async with self._connector(
                        self.settings.server_url,
                        **self._connection_options(),
                    ) as socket:
                        connection_open = True
                        _operator_client_info("WebSocket connection established")
                        _debug_client_phase("connected")
                        await self.run_session(socket)
                except asyncio.CancelledError:
                    raise
                except ProviderUnavailableError:
                    _operator_client_info("local capability became unavailable; stopping")
                    self.stop()
                except ProtocolIncompatibleError as error:
                    # A permanent contract mismatch (close 1002 with the
                    # protocol_incompatible diagnostic): automatic retries
                    # can never succeed against the same Server build. The
                    # operator updates one side and restarts the Client.
                    _operator_client_info(
                        "Relay protocol is incompatible with the Server; "
                        "stopping automatic reconnection"
                    )
                    _debug_log(
                        f"client protocol incompatible: reason={error.reason}"
                    )
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
                    phase = getattr(error, "startup_phase", None)
                    detail = f" phase-{phase}" if isinstance(phase, str) else ""
                    _debug_log(f"client reconnect: {type(error).__name__}{detail}")
                    pass
                else:
                    if self._session_registered and not self._stop_event.is_set():
                        _operator_client_info("Relay disconnected; reconnecting")
                    elif connection_open and not self._stop_event.is_set():
                        _operator_client_info(
                            "registration was rejected; retrying"
                        )
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

    @staticmethod
    def _index_capabilities(
        capabilities: Sequence[LocalCapability],
    ) -> dict[str, LocalCapability]:
        indexed: dict[str, LocalCapability] = {}
        for capability in capabilities:
            for tool in capability.tools:
                if not isinstance(tool, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", tool):
                    raise ValueError("unsupported local capability")
                if tool in indexed:
                    raise ValueError(f"duplicate local capability: {tool}")
                indexed[tool] = capability
        return indexed

    async def _start_capabilities(self) -> None:
        """Start injected capabilities, then atomically publish the inventory.

        No native tools or reference inventory exist. Provider clients come
        from the static mapping or, when a resolver is configured, from the
        resolver's current answer (the alias hub), so each announcement
        reflects the runtime hub state.
        """
        for ident in self._unique_capabilities:
            capability = self._capability_objects[ident]
            await capability.start()
        await self._publish_inventory()

    async def _publish_inventory(self) -> None:
        """Publish the fixed Relay announcement and local capability routes.

        The announcement carries exactly the fixed wire operations from
        ``relay_tools`` — never third-party descriptors, never schemas, and
        independent of the admin setting. Local capabilities keep explicit
        routes (the control verbs); third-party execution goes exclusively
        through the client catalog via ``mcp.command``.
        """
        routes: dict[str, tuple[ProviderToolClient, ProviderToolDescriptor]] = {}
        for ident in self._unique_capabilities:
            capability = self._capability_objects[ident]
            inventory = bounded_descriptors(await capability.list_tools())
            if {f"{d.provider_name}.{d.tool_name}" for d in inventory} != set(capability.tools):
                raise ConfigurationError()
            client = CapabilityProviderClient(capability, inventory)
            for descriptor in inventory:
                wire_name = f"{descriptor.provider_name}.{descriptor.tool_name}"
                if wire_name in routes:
                    raise ConfigurationError()
                routes[wire_name] = (client, descriptor)
        announcement_tools = sorted(WIRE_OPERATION_NAMES)
        if not set(announcement_tools).issubset(set(routes) | set(WIRE_OPERATION_NAMES)):
            raise ConfigurationError()
        # Validate the wire envelope shape (closed op set, bounds).
        Capabilities(
            version=1,
            type="capabilities",
            tools=announcement_tools,
            relay_contract=RELAY_CONTRACT,
            client_version=self._client_version,
        )
        self._provider_routes = routes
        self._announcement_tools = tuple(announcement_tools)
        self._inventory_ready = True

    async def reannounce(self) -> None:
        """Re-publish the inventory to the connected Server, if any.

        Invoked by the control capability after an inventory-changing
        mutation. Without a live session the call is a no-op: the next
        session start rebuilds and announces the inventory anyway.
        """
        if self._closed:
            return
        await self._publish_inventory()
        socket = self._socket
        if socket is None:
            return
        await self._send(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                tools=list(self._announcement_tools),
                relay_contract=RELAY_CONTRACT,
                client_version=self._client_version,
            ).model_dump(mode="json", exclude_defaults=True),
        )

    async def aclose(self) -> None:
        """Close every configured capability exactly once."""
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
        # Step 7B: stop the owned reconciliation task first so a mid-spawn
        # cancellation closes the in-flight transport before the
        # capabilities are torn down.
        startup, self._startup_task = self._startup_task, None
        if startup is not None:
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
        for ident in self._unique_capabilities:
            capability = self._capability_objects[ident]
            await asyncio.gather(capability.aclose(), return_exceptions=True)
        for client in self._provider_close_objects.values():
            await asyncio.gather(client.close(), return_exceptions=True)

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
        if not self._inventory_ready:
            await self._start_capabilities()
        self._socket = socket
        _debug_client_phase("register-send")
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
        # The Server's own package version rides in the existing handshake.
        # The field is mandatory on Registered, but the ``unknown`` fallback
        # keeps this side defensive against a hypothetical schema drift.
        self._server_version = registered.server_version or "unknown"
        _debug_client_phase("registered")
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
                tools=list(self._announcement_tools),
                relay_contract=RELAY_CONTRACT,
                client_version=self._client_version,
            ).model_dump(mode="json", exclude_defaults=True),
        )
        capability_summary = ", ".join(self._announcement_tools) or "none"
        _operator_client_info(
            f"capabilities announced ({len(self._announcement_tools)}): {capability_summary}"
        )
        _debug_client_phase("capabilities-send")
        heartbeat = asyncio.create_task(self._heartbeat(socket))
        action: asyncio.Task[None] | None = None
        action_request_id: str | None = None
        cancelled_requests: set[str] = set()
        receive: asyncio.Task[object] | None = asyncio.create_task(self._receive(socket))
        stopping = asyncio.create_task(self._stop_event.wait())
        unavailable = {asyncio.create_task(self._capability_objects[ident].wait_unavailable()) for ident in self._unique_capabilities}
        provider_unavailable: set[asyncio.Task[object]] = set()
        for provider in self._provider_close_objects.values():
            waiter = getattr(provider, "wait_unavailable", None)
            if callable(waiter):
                wait_unavailable = cast(Callable[[], Coroutine[Any, Any, None]], waiter)
                provider_unavailable.add(asyncio.create_task(wait_unavailable()))
        # Third-party routes: watch the catalog's alias providers too.
        if self.catalog is not None:
            for alias, record in sorted(self.catalog.snapshot._records.items()):
                provider = record.provider
                if provider is None:
                    continue
                waiter = getattr(provider, "wait_unavailable", None)
                if callable(waiter):
                    wait_unavailable = cast(
                        Callable[[], Coroutine[Any, Any, None]], waiter
                    )
                    provider_unavailable.add(asyncio.create_task(wait_unavailable()))
        try:
            while not self._stop_event.is_set():
                wait_for = {receive, stopping, *unavailable, *provider_unavailable}
                if action is not None:
                    wait_for.add(action)
                done, _ = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)
                if stopping in done:
                    break
                if done & unavailable:
                    _debug_client_phase("session-exit-capability-unavailable")
                    if action is not None:
                        if action_request_id is not None:
                            cancelled_requests.add(action_request_id)
                        action.cancel()
                        await asyncio.gather(action, return_exceptions=True)
                        action = None
                        action_request_id = None
                    raise ConnectionError("local capability unavailable")
                if done & provider_unavailable:
                    # A third-party MCP provider failing never kills the
                    # Client: that alias becomes non-executable (its runtime
                    # owner closes the transport; the catalog reflects the
                    # failure) while the local capabilities, the other
                    # aliases, and the session keep serving.
                    for task in done & provider_unavailable:
                        provider_unavailable.discard(task)
                    _debug_client_phase("alias-provider-unavailable-isolated")
                if action is not None and action in done:
                    try:
                        await action
                    except BaseException:
                        _debug_client_phase("session-exit-action-failed")
                        raise
                    action = None
                    action_request_id = None
                if receive in done:
                    try:
                        message = receive.result()
                    except BaseException:
                        _debug_client_phase("session-exit-receive-failed")
                        raise
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
            self._socket = None
            heartbeat.cancel()
            if receive is not None:
                receive.cancel()
            stopping.cancel()
            for task in unavailable:
                task.cancel()
            for task in provider_unavailable:
                task.cancel()
            if action is not None:
                if action_request_id is not None:
                    cancelled_requests.add(action_request_id)
                action.cancel()
            await asyncio.gather(
                heartbeat,
                stopping,
                *unavailable,
                *provider_unavailable,
                *(item for item in (receive, action) if item is not None),
                return_exceptions=True,
            )

    async def _heartbeat(self, socket: TextSocket) -> None:
        while not self._stop_event.is_set():
            await self._sleep_or_stop(HEARTBEAT_INTERVAL_SECONDS)
            if not self._stop_event.is_set():
                await self._send(socket, Heartbeat(version=2, type="heartbeat").model_dump(mode="json"))

    async def _perform(
        self,
        socket: TextSocket,
        message: InvokeMessage,
        cancelled_requests: set[str],
    ) -> None:
        try:
            if message.tool_name == "mcp.command":
                # Third-party execution: closed envelope, catalog reservation,
                # exactly one send, native result. Never the control path.
                await self._perform_mcp_command(socket, message, cancelled_requests)
                return
            route = self._provider_routes.get(message.tool_name)
            if route is None:
                raise ValueError("unsupported provider tool")
            provider, descriptor = route
            arguments = message.arguments
            if descriptor is not None:
                # The driver remains the sole validator; the relay enforces
                # transport bounds only and never interprets the schema.
                arguments = bounded_arguments(arguments)
                provider_tool_name = descriptor.tool_name
            else:
                provider_tool_name = message.tool_name
            _operator_client_info(f"Executing tool: {message.tool_name}")
            if isinstance(provider, CapabilityProviderClient):
                result = await provider.call_message(
                    provider_tool_name,
                    arguments,
                    request_id=message.request_id,
                )
            else:
                result = await provider.call_tool(provider_tool_name, arguments)
            if message.request_id in cancelled_requests:
                return
            provider_result = bounded_result(result)
            await self._send(
                socket,
                ClientResult(
                    version=2,
                    type="result",
                    request_id=message.request_id,
                    result=provider_result,
                ).model_dump(mode="json", by_alias=True, exclude_none=True),
            )
        except asyncio.CancelledError:
            raise
        except ProviderResultTooLargeError as error:
            if message.request_id in cancelled_requests:
                return
            detail = getattr(error, "detail", None)
            _debug_log(
                "client invocation refused: "
                f"tool={message.tool_name} code=result_too_large"
                + (f" | {detail}" if detail else "")
            )
            await self._send(
                socket,
                ClientError(
                    version=2,
                    type="error",
                    request_id=message.request_id,
                    error=ErrorDetail(
                        code="result_too_large",
                        message=(
                            f"tool '{message.tool_name}': {detail}"
                            if detail
                            else ProviderResultTooLargeError.wire_message
                        ),
                        execution_state="unknown",
                    ),
                ).model_dump(mode="json"),
            )
        except CommandError as error:
            if message.request_id in cancelled_requests:
                return
            _debug_log(
                "client command refused: "
                f"tool={message.tool_name} code={error.code} "
                f"state={error.execution_state}"
            )
            await self._send(
                socket,
                ClientError(
                    version=2,
                    type="error",
                    request_id=message.request_id,
                    error=error.to_payload(),
                ).model_dump(mode="json"),
            )
        except Exception as error:
            if message.request_id in cancelled_requests:
                return
            error_detail = f"{type(error).__name__}: {error}".replace(
                "\n", " "
            )[:200]
            _debug_log(
                "client invocation failed: "
                f"tool={message.tool_name} exception={error_detail}"
            )
            await self._send_error(socket, message.request_id, "client_error", "local action failed")

    async def _perform_mcp_command(
        self,
        socket: TextSocket,
        message: InvokeMessage,
        cancelled_requests: set[str],
    ) -> None:
        # A single safe target suffix is shared by DEBUG start and ERROR failed.
        # Never print a raw envelope value in an operator log.
        def safe_identifier(value: object) -> str | None:
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9_.:/-]{1,128}", value
            ):
                return None
            return value

        candidate_alias = safe_identifier(message.arguments.get("alias"))
        candidate_tool = safe_identifier(message.arguments.get("tool"))
        # An unknown name is still caller-controlled data; looking like an
        # identifier does not prove it cannot be a token. Only log names
        # already present in the trusted local catalog snapshot.
        record = (
            self.catalog.snapshot._records.get(candidate_alias)
            if self.catalog is not None and candidate_alias is not None
            else None
        )
        alias = candidate_alias if record is not None else None
        tool = (
            candidate_tool
            if record is not None
            and any(item.name == candidate_tool for item in record.descriptors)
            else None
        )
        target = (f" alias={alias}" if alias is not None else "") + (
            f" tool={tool}" if tool is not None else ""
        )
        try:
            await self._perform_mcp_command_outcome(
                socket, message, cancelled_requests, target
            )
        except asyncio.CancelledError:
            # A cancelled command is silent: no fake done, no failure event.
            raise
        except CommandError as error:
            if message.request_id not in cancelled_requests:
                _operator_client_error(
                    "mcp.command failed: "
                    f"request_id={message.request_id}{target} "
                    f"code={error.code} "
                    f"execution_state={error.execution_state}"
                )
            raise
        except ProviderResultTooLargeError:
            if message.request_id not in cancelled_requests:
                _operator_client_error(
                    "mcp.command failed: "
                    f"request_id={message.request_id}{target} "
                    "code=result_too_large "
                    "execution_state=unknown"
                )
            raise
        except Exception:
            if message.request_id not in cancelled_requests:
                _operator_client_error(
                    "mcp.command failed: "
                    f"request_id={message.request_id}{target} "
                    "code=client_error "
                    "execution_state=unknown"
                )
            raise

    async def _perform_mcp_command_outcome(
        self,
        socket: TextSocket,
        message: InvokeMessage,
        cancelled_requests: set[str],
        target: str,
    ) -> None:
        # Pre-dispatch start event: the same bounded identifiers as failed.
        _operator_client_debug(
            "mcp.command start: "
            f"request_id={message.request_id}{target}"
        )
        if self.catalog is None:
            raise CommandError(
                "execution_failed",
                "no local MCP catalog is available",
                execution_state="not_started",
            )
        outcome = await execute_command(message.arguments, catalog=self.catalog)
        if message.request_id in cancelled_requests:
            # The invocation was cancelled while the provider ran; a provider
            # that swallows cancellation must not yield a late result.
            return
        await self._send(
            socket,
            ClientResult(
                version=2,
                type="result",
                request_id=message.request_id,
                result=outcome.result,
            ).model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        # Terminal success event: result received ≠ business success. The
        # native isError flag is the only result content ever inspected;
        # alias/tool are the envelope identifiers execute_command validated.
        _operator_client_info(
            "mcp.command done: "
            f"request_id={message.request_id}{target} "
            f"isError={str(outcome.result.is_error).lower()}"
            + (" error_source=provider" if outcome.result.is_error else "")
        )

    async def _receive(self, socket: TextSocket) -> object:
        try:
            text = await socket.recv()
        except websockets.exceptions.ConnectionClosed as closed:
            # A 1002 close carrying the protocol_incompatible diagnostic is a
            # permanent contract mismatch, distinct from transient failures.
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
                error={
                    "code": code,
                    "message": message,
                    # Control-path failures never dispatch an MCP business
                    # operation, so the target was never contacted.
                    "execution_state": "not_started",
                },
            ).model_dump(mode="json"),
        )

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


async def _run_client(
    settings: ClientSettings,
    *,
    config_path: Path | None = None,
) -> None:
    control: ControlCapability | None = None
    capabilities: list[LocalCapability] = []
    if config_path is not None:
        # YAML mode owns the local MCP server hub: reconcile the configured
        # aliases, publish the third-party catalog, and expose the agent-facing
        # control capability. Environment-only mode has no YAML authority, so
        # the control surface and the catalog stay off. The capability list is
        # final BEFORE the RelayClient snapshot: appending after construction
        # leaves the control capability unregistered and every routed verb
        # fails with "unsupported provider tool".
        from .capabilities.control import ControlCapability
        from .config import load_client_admin_setting
        from .mcp_catalog import ClientCatalog
        from .mcp_hub import McpHub, production_transport_factory

        hub = McpHub(
            config_path,
            settings.workspace,
            transport_factory=production_transport_factory,
        )
        catalog = ClientCatalog()
        control = ControlCapability(
            hub=hub,
            workspace=settings.workspace,
            client_version=bounded_version_label(package_version()) or "unknown",
            admin_enabled=load_client_admin_setting(config_path),
        )
        control.bind_catalog(catalog)
        capabilities.append(control)
        client = RelayClient(settings, capabilities=capabilities)
        client.catalog = catalog
        control.bind_catalog_refresh(
            lambda: _refresh_catalog(hub, catalog)
        )
    else:
        client = RelayClient(settings, capabilities=capabilities)
    if control is not None:
        control.bind_inventory_change(client.reannounce)
        # Step 7B: publish the catalog on every runtime state change so
        # STARTING and terminal states are observable while a startup is
        # still in flight (including a cancelled admin-triggered spawn).
        control.hub.bind_on_change(
            lambda: _refresh_catalog(control.hub, client.catalog)
        )
    try:
        if control is not None:
            # Step 7B: the initial reconciliation is decoupled from the
            # connection — it runs as a task owned by the client (stopped
            # explicitly in aclose), never inline before the session. A
            # long alias startup can no longer delay the control channel.
            client.start_initial_reconciliation(
                lambda: _initial_reconciliation(control.hub, client.catalog)
            )
        await _run_with_signal_handlers(client)
    finally:
        await client.aclose()


async def _initial_reconciliation(hub: Any, catalog: Any) -> None:
    """Run the initial MCP reconciliation as an owned background task.

    Step 7B: this coroutine is never awaited inline by ``_run_client`` — it
    runs as a task owned by the RelayClient so the control connection and
    heartbeat start independently of a long local MCP startup. Alias
    startup failures never block the control channel: the aliases report
    their state through the catalog and the tools.
    """
    try:
        await hub.reconcile_all()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        _debug_log(f"hub startup incomplete: {type(error).__name__}")
    finally:
        _refresh_catalog(hub, catalog)


def _refresh_catalog(hub: Any, catalog: Any) -> None:
    """Publish the hub's current alias state into the client catalog."""
    if catalog is None:
        return
    hub.publish_catalog(catalog)


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
