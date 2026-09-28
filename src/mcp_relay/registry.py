"""In-memory connection registry for the single-client Relay MVP."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from mcp.types import CallToolResult

from .mcp_results import native_result
from .protocol import (
    RELAY_CONTRACT,
    Cancel,
    Capabilities,
    Catalog,
    CatalogTool,
    ClientError,
    ClientResult,
    InvokeMessage,
    Progress,
    Registered,
)
from .version import package_version


class JsonSocket(Protocol):
    async def send_json(self, message: object) -> None: ...


class RelayError(Exception):
    """Base error whose messages are safe to return to the control client."""


class AuthenticationError(RelayError):
    pass


class ClientAlreadyConnectedError(RelayError):
    pass


class ClientOfflineError(RelayError):
    pass


class UnknownClientError(RelayError):
    pass


class ClientBusyError(RelayError):
    pass


class DuplicateRequestError(RelayError):
    pass


class UnknownRequestError(RelayError):
    pass


class LateResponseError(RelayError):
    """An client replied after its invocation had already completed."""


#: Public hook for WS-tunnel progress frames: an async callable receiving the
#: in-flight relay request_id, the bounded progress value and its message.
ProgressListener = Callable[[str, int, str], Any]
#: Called (synchronously) whenever the published tool surface may have changed.
SurfaceListener = Callable[[], None]


class RemoteClientError(RelayError):
    def __init__(
        self,
        code: str,
        message: str,
        execution_state: str = "not_started",
    ) -> None:
        self.code = code
        self.message = message
        self.execution_state = execution_state
        super().__init__(message)


@dataclass
class _Client:
    socket: JsonSocket
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    admin: bool = False
    catalog: dict[str, CatalogTool] = field(default_factory=dict)
    last_heartbeat: float = field(default_factory=time.monotonic)
    progress_request_id: str | None = None
    progress: int | None = None
    # Bounded metadata announced in the capabilities frame; ``unknown`` until
    # the first announcement arrives.
    client_version: str = "unknown"


@dataclass(frozen=True)
class ClientStatusSnapshot:
    """Safe, immutable public state copied while holding the registry lock."""

    client_id: str | None
    connected: bool
    admin: bool
    invocation_state: str
    progress: int | None
    heartbeat_age_seconds: float | None
    client_version: str | None
    # Wall-clock window of the current/last connection (enriched status tool).
    connected_since: float | None = None
    last_disconnect_at: float | None = None
    last_disconnect_reason: str | None = None
    published_tools: int = 0


class RelayRegistry:
    """Atomically owns at most one authenticated client socket and request."""

    _RECENTLY_COMPLETED_LIMIT = 128

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_token: str,
        cancel_send_timeout_seconds: float = 0.25,
        server_version: str | None = None,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_token = client_token
        # The Server's package version comes from the installed distribution,
        # never from the Client. ``unknown`` when unresolvable.
        self._server_version = (
            package_version() if server_version is None else server_version
        ) or "unknown"
        self._client: _Client | None = None
        self._pending: dict[str, asyncio.Future[CallToolResult]] = {}
        self._recently_completed: dict[str, None] = {}
        self._lock = asyncio.Lock()
        self._cancel_send_timeout_seconds = cancel_send_timeout_seconds
        self.registrations_accepted = 0
        # Wall-clock connection windows for the enriched status tool.
        self._wall_clock = wall_clock or time.time
        self._connected_since: float | None = None
        self._last_disconnect_at: float | None = None
        self._last_disconnect_reason: str | None = None
        # Wired by the MCP facade: progress frames reach the calling MCP
        # context, surface changes reach every open MCP session.
        self._progress_listener: ProgressListener | None = None
        self._surface_listener: SurfaceListener | None = None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def current_progress(self) -> int | None:
        """Current bounded progress of the single invocation, if any."""
        return self._client.progress if self._client is not None else None

    @property
    def last_heartbeat(self) -> float | None:
        return self._client.last_heartbeat if self._client is not None else None

    @property
    def client_admin(self) -> bool:
        """Whether the connected Client allows the admin verbs."""
        client = self._client
        return client is not None and client.admin

    @property
    def catalog(self) -> tuple[CatalogTool, ...]:
        """Third-party tools of the connected Client; empty when offline."""
        client = self._client
        return () if client is None else tuple(client.catalog.values())

    def catalog_tool(self, name: str) -> CatalogTool | None:
        client = self._client
        return None if client is None else client.catalog.get(name)

    def set_surface_listener(self, listener: SurfaceListener | None) -> None:
        self._surface_listener = listener

    def _surface_changed(self) -> None:
        if self._surface_listener is not None:
            self._surface_listener()

    def set_progress_listener(self, listener: ProgressListener | None) -> None:
        """Register (or clear) the async listener for in-flight progress."""
        self._progress_listener = listener

    async def status_snapshot(self) -> ClientStatusSnapshot:
        """Return only safe client state, copied atomically under the lock."""
        async with self._lock:
            client = self._client
            if client is None:
                return ClientStatusSnapshot(
                    client_id=self._client_id,
                    connected=False,
                    admin=False,
                    invocation_state="idle",
                    progress=None,
                    heartbeat_age_seconds=None,
                    client_version=None,
                    connected_since=None,
                    last_disconnect_at=self._last_disconnect_at,
                    last_disconnect_reason=self._last_disconnect_reason,
                )
            heartbeat_age = max(0.0, time.monotonic() - client.last_heartbeat)
            return ClientStatusSnapshot(
                client_id=self._client_id,
                connected=True,
                admin=client.admin,
                invocation_state="busy" if self._pending else "idle",
                progress=client.progress,
                heartbeat_age_seconds=heartbeat_age,
                client_version=client.client_version,
                connected_since=self._connected_since,
                last_disconnect_at=self._last_disconnect_at,
                last_disconnect_reason=self._last_disconnect_reason,
                published_tools=len(client.catalog),
            )

    @property
    def server_version(self) -> str:
        """The Server package version announced in ``registered`` frames."""
        return self._server_version

    async def register(self, socket: JsonSocket, message: object) -> Registered:
        client_id = getattr(message, "client_id", None)
        if not isinstance(client_id, str):
            raise AuthenticationError("invalid client credentials")
        async with self._lock:
            if self._client_id is not None and client_id != self._client_id:
                raise AuthenticationError("invalid client credentials")
            if self._client is not None:
                raise ClientAlreadyConnectedError("client is already connected")
            if self._client_id is None:
                self._client_id = client_id
            self._client = _Client(socket=socket)
            self._connected_since = self._wall_clock()
            self.registrations_accepted += 1
        return Registered(
            version=1,
            type="registered",
            client_id=client_id,
            server_version=self._server_version,
            relay_contract=RELAY_CONTRACT,
        )

    async def set_capabilities(self, socket: JsonSocket, message: Capabilities) -> None:
        async with self._lock:
            client = self._require_socket(socket)
            client.admin = message.admin
            client.client_version = message.client_version
        self._surface_changed()

    async def set_catalog(self, socket: JsonSocket, message: Catalog) -> None:
        """Replace the connected Client's published tools atomically."""
        async with self._lock:
            client = self._require_socket(socket)
            client.catalog = {tool.name: tool for tool in message.tools}
        self._surface_changed()

    async def send(self, socket: JsonSocket, message: object) -> None:
        """Serialize every server-to-client write for the registered connection."""
        async with self._lock:
            client = self._require_socket(socket)
            write_lock = client.write_lock
        await self._send_serialized(socket, write_lock, message)

    async def heartbeat(self, socket: JsonSocket) -> None:
        async with self._lock:
            client = self._require_socket(socket)
            observed = time.monotonic()
            # Some Windows timer sources have millisecond resolution; keep a
            # heartbeat strictly newer for status consumers even when two
            # frames arrive inside one timer tick.
            client.last_heartbeat = max(observed, client.last_heartbeat + 1e-9)

    async def invoke(
        self,
        client_id: str | None,
        message: InvokeMessage,
        timeout_seconds: float,
    ) -> CallToolResult:
        request_id = message.request_id
        async with self._lock:
            if self._client_id is None or (
                client_id is not None and client_id != self._client_id
            ):
                raise UnknownClientError("unknown client")
            if self._client is None:
                raise ClientOfflineError("client is offline")
            if request_id in self._pending:
                raise DuplicateRequestError("duplicate request_id")
            if self._pending:
                raise ClientBusyError("client already has an invocation in progress")
            self._recently_completed.pop(request_id, None)
            future: asyncio.Future[CallToolResult] = (
                asyncio.get_running_loop().create_future()
            )
            self._pending[request_id] = future
            self._client.progress_request_id = request_id
            self._client.progress = None
            socket = self._client.socket
            write_lock = self._client.write_lock
        try:
            try:
                await self._send_serialized(
                    socket, write_lock, message.model_dump(mode="json")
                )
            except asyncio.CancelledError:
                await self._finalize_request(request_id, future)
                await self._send_cancel_if_connected(socket, request_id)
                raise
            except Exception as exc:
                await self._finalize_request(request_id, future)
                self._consume_future(future)
                raise ClientOfflineError("client is offline") from exc
            try:
                return await asyncio.wait_for(future, timeout_seconds)
            except (TimeoutError, asyncio.CancelledError):
                await self._finalize_request(request_id, future)
                await self._send_cancel_if_connected(socket, request_id)
                raise
        finally:
            await self._finalize_request(request_id, future)

    async def handle_result(self, message: ClientResult) -> None:
        # Converted to the native MCP result exactly once, here.
        await self._resolve(
            message.request_id, result=native_result(message.result)
        )

    async def handle_error(self, message: ClientError) -> None:
        await self._resolve(
            message.request_id,
            exception=RemoteClientError(
                message.error.code,
                message.error.message,
                message.error.execution_state,
            ),
        )

    async def handle_progress(self, message: Progress) -> None:
        """Record progress only for the request currently in flight."""
        async with self._lock:
            future = self._pending.get(message.request_id)
            if future is None:
                if message.request_id in self._recently_completed:
                    raise LateResponseError("late or duplicate response")
                raise UnknownRequestError("unknown request_id")
            if future.done():
                raise LateResponseError("late or duplicate response")
            if self._client is None:
                raise UnknownRequestError("unknown request_id")
            self._client.progress_request_id = message.request_id
            self._client.progress = message.progress
            listener = self._progress_listener
        if listener is not None:
            try:
                await listener(message.request_id, message.progress, message.message)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A progress listener failure must never break the invocation
                # or the registry's own progress accounting.
                return

    async def _resolve(
        self,
        request_id: str,
        *,
        result: CallToolResult | None = None,
        exception: Exception | None = None,
    ) -> None:
        async with self._lock:
            future = self._pending.get(request_id)
            if future is None:
                if request_id in self._recently_completed:
                    raise LateResponseError("late or duplicate response")
                raise UnknownRequestError("unknown request_id")
            if future.done():
                raise LateResponseError("late or duplicate response")
            if exception is not None:
                future.set_exception(exception)
            else:
                if result is None:  # pragma: no cover - result/error are exclusive
                    raise RuntimeError("missing result")
                future.set_result(result)

    async def disconnect(self, socket: JsonSocket, *, reason: str | None = None) -> None:
        async with self._lock:
            if self._client is None or self._client.socket is not socket:
                return
            self._client = None
            self._connected_since = None
            bounded_reason = (reason or "connection closed")[:64]
            self._last_disconnect_at = self._wall_clock()
            self._last_disconnect_reason = bounded_reason
            for request_id, future in self._pending.items():
                if not future.done():
                    # The request was already sent to the Client; its answer
                    # is lost, so the server cannot know whether the business
                    # MCP command ran: closed code `timeout`, state `unknown`.
                    future.set_exception(
                        RemoteClientError(
                            "timeout",
                            "client disconnected before answering",
                            execution_state="unknown",
                        )
                    )
                self._remember_completed(request_id)
            self._pending.clear()
        self._surface_changed()

    async def _send_cancel_if_connected(
        self, socket: JsonSocket, request_id: str
    ) -> None:
        """Best-effort, bounded cancellation after request state is released."""
        async with self._lock:
            if self._client is None or self._client.socket is not socket:
                return
            write_lock = self._client.write_lock
        message = Cancel(
            version=2,
            type="cancel",
            request_id=request_id,
            reason="control request cancelled or timed out",
        ).model_dump(mode="json")
        try:
            await asyncio.wait_for(
                self._send_serialized(socket, write_lock, message),
                timeout=self._cancel_send_timeout_seconds,
            )
        except (Exception, asyncio.CancelledError):
            return

    async def _send_serialized(
        self, socket: JsonSocket, write_lock: asyncio.Lock, message: object
    ) -> None:
        async with write_lock:
            await socket.send_json(message)

    async def _finalize_request(
        self, request_id: str, future: asyncio.Future[CallToolResult]
    ) -> None:
        """Atomically release request state and leave its bounded tombstone."""
        async with self._lock:
            if self._pending.get(request_id) is future:
                self._pending.pop(request_id)
            self._remember_completed(request_id)
            if (
                self._client is not None
                and self._client.progress_request_id == request_id
            ):
                self._client.progress_request_id = None
                self._client.progress = None

    @staticmethod
    def _consume_future(future: asyncio.Future[CallToolResult]) -> None:
        """Avoid an unobserved disconnect exception after failed initial I/O."""
        if not future.done():
            future.cancel()
        if not future.cancelled():
            future.exception()

    def _require_socket(self, socket: JsonSocket) -> _Client:
        if self._client is None or self._client.socket is not socket:
            raise AuthenticationError("unregistered socket")
        return self._client

    def _remember_completed(self, request_id: str) -> None:
        """Keep a bounded correlation tombstone for late client responses."""
        self._recently_completed[request_id] = None
        while len(self._recently_completed) > self._RECENTLY_COMPLETED_LIMIT:
            self._recently_completed.pop(next(iter(self._recently_completed)))
