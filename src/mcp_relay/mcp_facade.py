"""MCP facade of the Relay Server: Relay tools plus the Client's own tools.

Two tools are always listed: ``relay_status`` and ``relay_registry_search``.
The connected Client's third-party tools are published natively under their
catalog names, and the five admin tools only while that Client allows
administration. Every change of that surface is announced to open MCP
sessions with ``notifications/tools/list_changed``.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from mcp import types as mcp_types
from mcp.types import CallToolResult
from pydantic import BaseModel, PrivateAttr, ValidationError

from .mcp_registry import (
    DEFAULT_REGISTRY_BASE_URL,
    RegistrySearchInput,
    RegistrySearchResult,
    RegistryUnreachableError,
    search_registry_servers,
)
from .mcp_results import RelayToolError, relay_error_result
from .protocol import (
    OP_CLIENT_STATUS,
    OP_MCP_ADD,
    OP_MCP_COMMAND,
    OP_MCP_DELETE,
    OP_MCP_DISABLE,
    OP_MCP_ENABLE,
    OP_MCP_MODIFY,
    RELAY_CONTRACT,
    CatalogTool,
    InvokeMessage,
)
from .registry import (
    ClientBusyError,
    ClientOfflineError,
    ClientStatusSnapshot,
    DuplicateRequestError,
    RelayRegistry,
    RemoteClientError,
    UnknownClientError,
)
from .version import package_version

#: Upper bound of the live Client probe made by ``relay_status``.
STATUS_PROBE_SECONDS = 2.0

_RELAY_FAILURES: tuple[type[BaseException], ...] = (
    UnknownClientError,
    ClientOfflineError,
    ClientBusyError,
    DuplicateRequestError,
    TimeoutError,
    RemoteClientError,
)

#: Closed codes a Client error frame may carry; anything else is normalized.
_CLIENT_ERROR_CODES = frozenset(
    {
        "invalid_arguments",
        "result_too_large",
        "alias_unknown",
        "alias_unavailable",
        "tool_unknown",
        "execution_failed",
        "timeout",
        "config_invalid",
        "spawn_failed",
        "startup_budget_exhausted",
        "spawn_cancelled",
        "transport_unsupported",
        "registry_unreachable",
        "invalid_alias",
        "alias_conflict",
        "invalid_entry",
        "permission_denied",
    }
)

_LOCAL_FAILURES: dict[type[BaseException], tuple[str, str, str]] = {
    # (code, message, execution_state); nothing left the Server for these.
    UnknownClientError: ("client_unavailable", "client unavailable", "not_started"),
    ClientOfflineError: ("client_unavailable", "client offline", "not_started"),
    ClientBusyError: ("client_busy", "client busy", "not_started"),
    DuplicateRequestError: ("client_busy", "client busy", "not_started"),
    # The command may or may not have reached the target.
    TimeoutError: ("timeout", "invocation timed out", "unknown"),
}


def _failure(error: BaseException) -> RelayToolError:
    """Map an expected dispatch failure to a closed Relay error."""
    if isinstance(error, RemoteClientError):
        if error.code in _CLIENT_ERROR_CODES:
            return RelayToolError(
                error.code, error.message, execution_state=error.execution_state
            )
        code = "client_busy" if error.code == "busy" else "execution_failed"
        return RelayToolError(
            code, "client invocation failed", execution_state=error.execution_state
        )
    for failure_type, (code, message, state) in _LOCAL_FAILURES.items():
        if isinstance(error, failure_type):
            return RelayToolError(code, message, execution_state=state)
    return RelayToolError(
        "internal_error", "internal relay error", execution_state="not_started"
    )


class _ProgressTunnel:
    """Bind WS progress frames to the invoking tool's FastMCP context.

    ``forward`` runs in the WS-ingress task, but ``Context.report_progress``
    reads request-scoped state, so the dispatching tool runs a ``pump`` task
    of its own and forwards each queued frame from there.
    """

    _QUEUE_LIMIT = 256
    _PUMP_POLL_SECONDS = 0.05

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[tuple[int, str]]] = {}
        self._closed: set[str] = set()

    def bind(self, request_id: str) -> None:
        self._closed.discard(request_id)
        self._queues[request_id] = asyncio.Queue(maxsize=self._QUEUE_LIMIT)

    def unbind(self, request_id: str) -> None:
        self._closed.add(request_id)

    def forward(self, request_id: str, progress: int, message: str) -> None:
        queue = self._queues.get(request_id)
        if queue is None:
            return
        if queue.full():
            # Bounded buffering: drop the oldest rather than grow unbounded.
            queue.get_nowait()
        queue.put_nowait((progress, message))

    async def pump(self, request_id: str, ctx: Context) -> None:
        queue = self._queues.get(request_id)
        if queue is None:
            return
        while True:
            try:
                progress, message = await asyncio.wait_for(
                    queue.get(), timeout=self._PUMP_POLL_SECONDS
                )
            except TimeoutError:
                if request_id in self._closed and queue.empty():
                    self._queues.pop(request_id, None)
                    self._closed.discard(request_id)
                    return
                continue
            try:
                await ctx.report_progress(progress=progress, message=message or None)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue  # a failed notification never breaks the invocation


class _Dispatcher:
    """Send one bounded invocation to the Client, with progress forwarding."""

    def __init__(self, registry: RelayRegistry, timeout_seconds: float) -> None:
        self.registry = registry
        self.timeout_seconds = timeout_seconds
        self._progress = _ProgressTunnel()

        async def listener(request_id: str, progress: int, message: str) -> None:
            self._progress.forward(request_id, progress, message)

        registry.set_progress_listener(listener)

    async def invoke(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> CallToolResult:
        message = InvokeMessage(
            version=2,
            type="invoke",
            request_id=uuid.uuid4().hex,
            tool_name=operation,
            arguments=arguments,
        )
        try:
            ctx: Context | None = get_context()
        except RuntimeError:
            ctx = None
        pump: asyncio.Task[None] | None = None
        if ctx is not None:
            self._progress.bind(message.request_id)
            pump = asyncio.create_task(self._progress.pump(message.request_id, ctx))
        try:
            return await self.registry.invoke(
                None, message, timeout_seconds or self.timeout_seconds
            )
        finally:
            if pump is not None:
                self._progress.unbind(message.request_id)
                await asyncio.gather(pump, return_exceptions=True)

    async def call(self, operation: str, arguments: dict[str, Any]) -> CallToolResult:
        """Invoke and render every failure as a closed ``isError`` result."""
        try:
            return await self.invoke(operation, arguments)
        except _RELAY_FAILURES as error:
            return relay_error_result(_failure(error))


class RelayedTool(Tool):
    """One third-party tool of the Client, called through ``mcp.command``."""

    alias: str
    tool: str
    _dispatcher: Any = PrivateAttr(default=None)

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        assert self._dispatcher is not None
        result = await self._dispatcher.call(
            OP_MCP_COMMAND,
            {"alias": self.alias, "tool": self.tool, "arguments": dict(arguments)},
        )
        return ToolResult.from_mcp_result(result)


def _relayed_tool(entry: CatalogTool, dispatcher: _Dispatcher) -> RelayedTool:
    tool = RelayedTool(
        name=entry.name,
        description=entry.description or None,
        parameters=dict(entry.input_schema),
        output_schema=(
            None if entry.output_schema is None else dict(entry.output_schema)
        ),
        annotations=(
            None
            if not entry.annotations
            else mcp_types.ToolAnnotations.model_validate(entry.annotations)
        ),
        alias=entry.alias,
        tool=entry.tool,
    )
    tool._dispatcher = dispatcher
    return tool


def _admin_tools(dispatcher: _Dispatcher) -> list[Tool]:
    """The five admin verbs; listed only while the Client allows them."""

    async def relay_mcp_add(
        alias: str, entry: dict[str, Any]
    ) -> CallToolResult:
        """Declare and start a new MCP server alias on the Client.

        ``entry`` takes exactly one of ``command``, ``url`` or ``source``, and
        may add ``enabled``, ``version``, ``tools`` (allowlist) and ``env``.
        """
        return await dispatcher.call(OP_MCP_ADD, {"alias": alias, "entry": entry})

    async def relay_mcp_modify(
        alias: str, entry: dict[str, Any]
    ) -> CallToolResult:
        """Replace an MCP server alias entry completely, then restart it."""
        return await dispatcher.call(OP_MCP_MODIFY, {"alias": alias, "entry": entry})

    async def relay_mcp_delete(alias: str) -> CallToolResult:
        """Stop an MCP server and remove its alias from the Client."""
        return await dispatcher.call(OP_MCP_DELETE, {"alias": alias})

    async def relay_mcp_enable(alias: str) -> CallToolResult:
        """Enable an MCP server alias and start it."""
        return await dispatcher.call(OP_MCP_ENABLE, {"alias": alias})

    async def relay_mcp_disable(alias: str) -> CallToolResult:
        """Disable an MCP server alias; the entry is kept, the server stops."""
        return await dispatcher.call(OP_MCP_DISABLE, {"alias": alias})

    return [
        Tool.from_function(function, output_schema=None)
        for function in (
            relay_mcp_add,
            relay_mcp_modify,
            relay_mcp_delete,
            relay_mcp_enable,
            relay_mcp_disable,
        )
    ]


class _ClientToolsProvider(Provider):
    """Lists what the connected Client offers, read at every request."""

    def __init__(self, registry: RelayRegistry, dispatcher: _Dispatcher) -> None:
        super().__init__()
        self._registry = registry
        self._dispatcher = dispatcher
        self._admin = {tool.name: tool for tool in _admin_tools(dispatcher)}

    async def _list_tools(self) -> list[Tool]:
        tools: list[Tool] = []
        if self._registry.client_admin:
            tools.extend(self._admin.values())
        tools.extend(
            _relayed_tool(entry, self._dispatcher)
            for entry in sorted(self._registry.catalog, key=lambda item: item.name)
        )
        return tools

    async def _get_tool(self, name: str, version: Any = None) -> Tool | None:
        if name in self._admin:
            return self._admin[name] if self._registry.client_admin else None
        entry = self._registry.catalog_tool(name)
        return None if entry is None else _relayed_tool(entry, self._dispatcher)


class _SessionTracker(Middleware):
    """Remember open MCP sessions to announce tool-list changes to them.

    The SDK hands out a fresh ``ServerSession`` proxy per request; sending on
    any of them without a related request uses the connection's standalone
    stream, so keeping the latest proxy per session id is enough.
    """

    MAX_SESSIONS = 64

    def __init__(self) -> None:
        self.sessions: OrderedDict[str, Any] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()

    async def on_request(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        ctx = context.fastmcp_context
        if ctx is not None:
            try:
                session_id, session = ctx.session_id, ctx.session
            except RuntimeError:
                pass
            else:
                self.sessions[session_id] = session
                self.sessions.move_to_end(session_id)
                while len(self.sessions) > self.MAX_SESSIONS:
                    self.sessions.popitem(last=False)
        return await call_next(context)

    def announce(self) -> None:
        """Schedule one ``tools/list_changed`` per session, best effort."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._broadcast())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _broadcast(self) -> None:
        for session_id, session in list(self.sessions.items()):
            try:
                await session.send_notification(
                    mcp_types.ToolListChangedNotification()
                )
            except Exception:
                self.sessions.pop(session_id, None)


# ---------------------------------------------------------------------------
# relay_status
# ---------------------------------------------------------------------------


class ServerStatus(BaseModel):
    version: str
    relay_contract: int
    published_tools: int


class ClientStatus(BaseModel):
    client_id: str | None
    connected: bool
    version: str | None
    admin: bool | None
    invocation_state: Literal["idle", "busy"]
    progress: int | None
    heartbeat_age_seconds: float | None
    connected_since: str | None
    last_disconnect: dict[str, str] | None
    uptime_seconds: int | None
    #: ``live`` (answered now), ``cached`` (last good answer) or ``unavailable``.
    report: Literal["live", "cached", "unavailable"]
    report_age_seconds: float | None


class RelayStatus(BaseModel):
    server: ServerStatus
    client: ClientStatus
    mcp_servers: list[dict[str, Any]] | None
    disk_differs: list[str] | None


def _rfc3339(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _StatusProbe:
    """Live Client report with a cached fallback when the Client can't answer."""

    def __init__(self, dispatcher: _Dispatcher) -> None:
        self._dispatcher = dispatcher
        self._cached: dict[str, Any] | None = None
        self._cached_at: float | None = None

    async def report(
        self, snapshot: ClientStatusSnapshot
    ) -> tuple[dict[str, Any] | None, str, float | None]:
        if snapshot.connected and snapshot.invocation_state == "idle":
            timeout = min(STATUS_PROBE_SECONDS, self._dispatcher.timeout_seconds)
            try:
                result = await self._dispatcher.invoke(
                    OP_CLIENT_STATUS, {}, timeout_seconds=timeout
                )
            except _RELAY_FAILURES:
                result = None
            if result is not None and isinstance(result.structured_content, dict):
                self._cached = dict(result.structured_content)
                self._cached_at = time.monotonic()
                return self._cached, "live", 0.0
        if self._cached is None or self._cached_at is None:
            return None, "unavailable", None
        return self._cached, "cached", round(time.monotonic() - self._cached_at, 3)


async def _relay_status(registry: RelayRegistry, probe: _StatusProbe) -> RelayStatus:
    snapshot = await registry.status_snapshot()
    report, source, age = await probe.report(snapshot)
    last_disconnect = None
    if snapshot.last_disconnect_at is not None:
        last_disconnect = {
            "at": _rfc3339(snapshot.last_disconnect_at) or "",
            "reason": snapshot.last_disconnect_reason or "",
        }
    return RelayStatus(
        server=ServerStatus(
            version=package_version() or "unknown",
            relay_contract=RELAY_CONTRACT,
            published_tools=len(registry.catalog),
        ),
        client=ClientStatus(
            client_id=snapshot.client_id,
            connected=snapshot.connected,
            version=snapshot.client_version,
            admin=snapshot.admin if snapshot.connected else None,
            invocation_state="busy" if snapshot.invocation_state == "busy" else "idle",
            progress=snapshot.progress,
            heartbeat_age_seconds=snapshot.heartbeat_age_seconds,
            connected_since=_rfc3339(snapshot.connected_since),
            last_disconnect=last_disconnect,
            uptime_seconds=None if report is None else report.get("uptime_seconds"),
            report=source,
            report_age_seconds=age,
        ),
        mcp_servers=None if report is None else report.get("mcp_servers"),
        disk_differs=None if report is None else report.get("disk_differs"),
    )


def create_mcp_facade(
    *,
    registry: RelayRegistry,
    timeout_seconds: float,
    registry_base_url: str = DEFAULT_REGISTRY_BASE_URL,
    registry_transport: Any | None = None,
) -> FastMCP:
    """Create the MCP server for one Relay Server.

    ``registry_transport`` is an httpx2 async transport override for the
    registry search, used by tests.
    """
    dispatcher = _Dispatcher(registry, timeout_seconds)
    probe = _StatusProbe(dispatcher)
    sessions = _SessionTracker()
    mcp: FastMCP = FastMCP(
        "MCP Relay",
        strict_input_validation=True,
        providers=[_ClientToolsProvider(registry, dispatcher)],
        middleware=[sessions],
    )
    registry.set_surface_listener(sessions.announce)

    @mcp.tool
    async def relay_status() -> RelayStatus:
        """Report the Relay Server, the connected Client and its MCP servers.

        Client details come from a short live probe; when the Client is busy
        or unreachable the last good answer is returned as ``cached`` with its
        age.
        """
        try:
            return await _relay_status(registry, probe)
        except Exception:
            raise ToolError("internal relay error") from None

    @mcp.tool
    async def relay_registry_search(
        query: str,
        limit: int = 10,
        cursor: str | None = None,
        version: str | None = None,
        updated_since: str | None = None,
        include_deleted: bool = False,
    ) -> RegistrySearchResult:
        """Search the official MCP Registry (read-only, server-side).

        Returns bounded server metadata: name, title, description, version,
        repository URL and declarative-launcher packages. Never touches the
        Client and never writes any configuration.
        """
        try:
            search_input = RegistrySearchInput(
                query=query,
                limit=limit,
                cursor=cursor,
                version=version,
                updated_since=updated_since,
                include_deleted=include_deleted,
            )
        except ValidationError:
            raise ToolError("invalid registry search arguments") from None
        try:
            return await search_registry_servers(
                search_input.query,
                base_url=registry_base_url,
                limit=search_input.limit,
                cursor=search_input.cursor,
                version=search_input.version,
                updated_since=search_input.updated_since,
                include_deleted=search_input.include_deleted,
                transport=registry_transport,
            )
        except RegistryUnreachableError:
            raise ToolError(
                json.dumps(
                    {
                        "code": "registry_unreachable",
                        "message": "registry unreachable",
                        "suggested_action": "retry_later",
                    }
                )
            ) from None
        except Exception:
            raise ToolError("internal relay error") from None

    return mcp


def create_mcp_http_app(mcp: FastMCP) -> Any:
    """Create the FastMCP Streamable HTTP app for the /mcp path.

    Host/Origin protection is disabled here: every request is authenticated
    with the Relay's own Bearer token instead.
    """
    return mcp.http_app(
        path="/mcp",
        stateless_http=False,
        json_response=True,
        host_origin_protection=False,
    )
