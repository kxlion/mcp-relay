"""Strict fixed-surface MCP facade for the single-client Relay server.

Every public tool is registered statically at startup from the single
surface definition in ``relay_tools``: two Server-local tools, the enriched
client status, discovery and execution (always available) and the six
admin-gated CRUD/reload verbs. Nothing here varies with connectivity, the
admin setting, or the third-party catalog: one refused operation keeps its
public descriptor, and no third-party tool is ever published individually.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, cast

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import Field, ValidationError

from .mcp_registry import (
    DEFAULT_REGISTRY_BASE_URL,
    RegistrySearchInput,
    RegistrySearchResult,
    RegistryUnreachableError,
    search_registry_servers,
)
from .mcp_results import RelayToolError, relay_error_result
from .output_models import Output
from .protocol import (
    InvokeMessage,
    VersionLabel,
)
from .registry import (
    ClientBusyError,
    ClientOfflineError,
    ClientStatusSnapshot,
    DuplicateRequestError,
    RelayRegistry,
    RemoteClientError,
    UnknownClientError,
    UnsupportedToolError,
)

#: Expected dispatch failures mapped to safe, closed MCP tool errors.
_RELAY_FAILURES: tuple[type[BaseException], ...] = (
    UnknownClientError,
    ClientOfflineError,
    ClientBusyError,
    DuplicateRequestError,
    UnsupportedToolError,
    TimeoutError,
    RemoteClientError,
)


_EXECUTION_STATE_BY_FAILURE: dict[type[BaseException], str] = {
    # Nothing left the server for these refusals.
    UnknownClientError: "not_started",
    ClientOfflineError: "not_started",
    ClientBusyError: "not_started",
    DuplicateRequestError: "not_started",
    UnsupportedToolError: "not_started",
    # A command may or may not have reached the third-party MCP tool.
    TimeoutError: "unknown",
}


_CLIENT_EMITTED_ERROR_CODES = frozenset({"result_too_large"})

#: The closed Relay codes the Client can emit in an error frame: the
#: command/discovery codes, the hub refusals transported by the admin verbs,
#: and the control capability's structured refusals. Anything else is
#: normalized to an existing closed code at this facade — the wire contract
#: never forwards an unknown code to MCP clients.
_RELAY_CLIENT_ERROR_CODES = frozenset(
    {
        # Client-routed command/discovery codes.
        "invalid_arguments",
        "catalog_stale",
        "invalid_cursor",
        "result_too_large",
        "alias_unknown",
        "alias_unavailable",
        "tool_unknown",
        "execution_failed",
        "timeout",
        # Hub refusals relayed through the admin verbs.
        "config_invalid",
        "spawn_failed",
        "startup_budget_exhausted",
        "spawn_cancelled",
        "transport_unsupported",
        "registry_unreachable",
        # Control capability structured refusals.
        "invalid_alias",
        "alias_conflict",
        "invalid_entry",
        "permission_denied",
    }
)

#: Generic transport-level client codes with one appropriate existing Relay
#: code each.
_CODE_NORMALIZATION: dict[str, str] = {
    "busy": "client_busy",
    "client_error": "execution_failed",
}


def _normalize_relay_code(code: str) -> str:
    """Map an unknown Relay code onto an existing closed code."""
    if code in _RELAY_CLIENT_ERROR_CODES:
        return code
    return _CODE_NORMALIZATION.get(code, "execution_failed")


def _dispatch_failure_error(
    error: BaseException,
    *,
    command_dispatched: bool = True,
) -> RelayToolError:
    """Map an expected dispatch failure to a closed-code Relay tool error.

    The closed {code, message, execution_state} contract is honored for every
    dispatch failure: refusals before the send carry ``not_started`` and a
    lost or timed-out command carries ``unknown``. Verbs that never execute
    a business MCP command (``command_dispatched=False`` — mcp.list,
    client.status) report ``not_started`` even on a timeout: nothing was
    uncertain, the answer was simply lost.
    """
    if isinstance(error, RemoteClientError):
        # Honest passthrough, allowlisted: only codes the relay client itself
        # emits carry their own message (the client builds it closed and
        # safe, e.g. result_too_large naming the tool). Any other
        # RemoteClientError keeps the (normalized) closed code and replaces
        # the message with the opaque closed fallback.
        message = (
            error.message
            if error.code in _CLIENT_EMITTED_ERROR_CODES
            else "client invocation failed"
        )
        return RelayToolError(
            _normalize_relay_code(error.code),
            message,
            execution_state=error.execution_state,
        )
    for failure_type, state in _EXECUTION_STATE_BY_FAILURE.items():
        if isinstance(error, failure_type):
            resolved = state
            if not command_dispatched and failure_type is TimeoutError:
                resolved = "not_started"
            return RelayToolError(
                _FAILURE_CODES[failure_type],
                _FAILURE_MESSAGES[failure_type],
                execution_state=resolved,
            )
    return RelayToolError("internal_error", "internal relay error", execution_state="not_started")


_FAILURE_CODES: dict[type[BaseException], str] = {
    UnknownClientError: "client_unavailable",
    ClientOfflineError: "client_unavailable",
    ClientBusyError: "client_busy",
    DuplicateRequestError: "client_busy",
    # The Client announced the nine Relay operations; an operation outside
    # that fixed set is a tool the Relay does not know (closed code set).
    UnsupportedToolError: "tool_unknown",
    TimeoutError: "timeout",
}

_FAILURE_MESSAGES: dict[type[BaseException], str] = {
    UnknownClientError: "client unavailable",
    ClientOfflineError: "client offline",
    ClientBusyError: "client busy",
    DuplicateRequestError: "client busy",
    UnsupportedToolError: "unsupported relay operation",
    TimeoutError: "invocation timed out",
}


def _dispatch_failure_message(
    error: BaseException,
    *,
    command_dispatched: bool = True,
) -> str:
    """Map an expected dispatch failure to a bounded, safe message.

    ``command_dispatched=False`` marks verbs that never execute a business
    MCP command (mcp.list, client.status): a timeout there is a lost
    discovery/status answer, not an uncertain command, so the spec's
    ``not_started`` state applies.
    """
    closed = _dispatch_failure_error(
        error, command_dispatched=command_dispatched
    )
    return json.dumps(closed.to_payload())


def _request_id() -> str:
    return uuid.uuid4().hex


class _ProgressTunnel:
    """Bind WS progress frames to the invoking tool's FastMCP context.

    ``forward`` runs in the WS-ingress task, but ``Context.report_progress``
    reads request-scoped state from the invoking task's context variables —
    so the dispatching tool starts a ``pump`` task of its own and forwards
    each queued frame from there. Progress notifications therefore reach the
    calling MCP client without any private FastMCP access.
    """

    _QUEUE_LIMIT = 256
    _PUMP_POLL_SECONDS = 0.05

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[tuple[int, str] | None]] = {}
        self._closed: set[str] = set()

    def bind(self, request_id: str, ctx: Context) -> None:
        self._closed.discard(request_id)
        self._queues[request_id] = asyncio.Queue(maxsize=self._QUEUE_LIMIT)

    def unbind(self, request_id: str) -> None:
        self._closed.add(request_id)

    def forward(self, request_id: str, progress: int, message: str) -> None:
        """Queue one WS progress frame; called from the WS-ingress task."""
        queue = self._queues.get(request_id)
        if queue is None:
            return
        try:
            queue.put_nowait((progress, message))
        except asyncio.QueueFull:
            # Bounded buffering: drop the oldest rather than grow unbounded.
            try:
                queue.get_nowait()
                queue.put_nowait((progress, message))
            except asyncio.QueueEmpty:
                return

    async def pump(self, request_id: str, ctx: Context) -> None:
        """Forward queued frames from the invoking task until dispatch ends."""
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
                await ctx.report_progress(
                    progress=progress, message=message or None
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed notification never breaks the relay invocation.
                continue


class LastDisconnectOutput(Output):
    at: str
    reason: str


class StatusCountersOutput(Output):
    """Fixed-surface counters; third-party tool counts are never claimed."""

    public_tools: int = 0
    client_operations: int = 0


class ClientStatusOutput(Output):
    client_id: str | None
    connected: bool
    capabilities: list[str]
    invocation_state: Literal["idle", "busy"]
    progress: int | None
    heartbeat_age_seconds: float | None
    # Bounded version metadata with an explicit unknown fallback so MCP
    # clients can compare server and client versions at a glance.
    client_version: VersionLabel = "unknown"
    # Enriched status: connection windows and fixed-surface counters. The
    # tool always answers from server state, even while no Client connects.
    connected_since: str | None = None
    last_disconnect: LastDisconnectOutput | None = None
    counters: StatusCountersOutput = Field(default_factory=StatusCountersOutput)
    suggested_action: Literal["start_client"] | None = None


def _rfc3339(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _status_output(snapshot: ClientStatusSnapshot) -> ClientStatusOutput:
    last_disconnect = None
    if (
        snapshot.last_disconnect_at is not None
        and snapshot.last_disconnect_reason is not None
    ):
        last_disconnect = LastDisconnectOutput(
            at=_rfc3339(snapshot.last_disconnect_at),
            reason=snapshot.last_disconnect_reason,
        )
    return ClientStatusOutput(
        client_id=snapshot.client_id,
        connected=snapshot.connected,
        capabilities=list(snapshot.capabilities),
        invocation_state=snapshot.invocation_state,
        progress=snapshot.progress,
        heartbeat_age_seconds=snapshot.heartbeat_age_seconds,
        client_version=snapshot.client_version or "unknown",
        connected_since=(
            _rfc3339(snapshot.connected_since)
            if snapshot.connected_since is not None
            else None
        ),
        last_disconnect=last_disconnect,
        counters=StatusCountersOutput(
            public_tools=snapshot.public_tools,
            client_operations=snapshot.client_operations,
        ),
        suggested_action=None if snapshot.connected else "start_client",
    )


def create_mcp_facade(
    *,
    registry: RelayRegistry,
    timeout_seconds: float,
    client_id: str | None = None,
    registry_base_url: str = DEFAULT_REGISTRY_BASE_URL,
    registry_transport: Any | None = None,
) -> FastMCP:
    """Create one MCP server for a Relay app with the full fixed surface.

    The Server-local tools are registered here; the Client-routed tools are
    registered by :func:`register_client_routed_tools` once the caller has
    bound the facade to its HTTP app (they need no extra wiring). Together
    the two groups form the complete static surface from ``relay_tools``:
    one refused operation keeps its public descriptor and no third-party
    tool is ever published individually.

    ``registry_transport`` is an httpx2 async transport override used by tests
    and by deployments that must reach the registry through a custom client;
    production keeps the default transport.
    """
    # ``strict_input_validation`` publishes closed input schemas
    # (``additionalProperties: false``) through the public FastMCP API — the
    # Relay contract that used to be enforced by post-hoc private-schema
    # surgery on the MCP SDK.
    mcp: FastMCP = FastMCP("MCP Relay", strict_input_validation=True)
    registered_tool_names: list[str] = []

    @mcp.tool
    async def relay_server_status() -> ClientStatusOutput:
        """Return the Relay's safe status; always answers, Client or not."""
        try:
            return _status_output(await registry.status_snapshot())
        except ToolError:
            raise
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
        client channel and never writes any configuration.
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
            raise ToolError(_structured_error_json("registry_unreachable")) from None
        except Exception:
            raise ToolError("internal relay error") from None

    registered_tool_names.extend(
        ("relay_server_status", "relay_registry_search")
    )
    registered_tool_names.extend(
        register_client_routed_tools(
            mcp,
            registry=registry,
            timeout_seconds=timeout_seconds,
            client_id=client_id,
        )
    )
    registry.set_public_tools_count(len(registered_tool_names))

    return mcp


def register_client_routed_tools(
    mcp: FastMCP,
    *,
    registry: RelayRegistry,
    timeout_seconds: float,
    client_id: str | None = None,
) -> list[str]:
    """Register the nine Client-routed tools of the fixed surface.

    Each public tool maps to exactly one wire operation via
    ``relay_tools.PUBLIC_TO_WIRE`` and dispatches a single bounded
    ``InvokeMessage`` to the connected Client. The Server-side facade does
    not interpret arguments: the Client validates them again against the
    closed operation envelopes. Returns the registered tool names so the
    facade can report the public surface size without touching any private
    FastMCP manager.
    """

    # WS-tunnel progress frames are surfaced to the calling MCP client's
    # context through the public FastMCP hook ``Context.report_progress``.
    progress_tunnel = _ProgressTunnel()

    async def _progress_listener(
        request_id: str, progress: int, message: str
    ) -> None:
        progress_tunnel.forward(request_id, progress, message)

    registry.set_progress_listener(_progress_listener)

    async def _dispatch(
        tool_name: str, arguments: dict[str, Any], ctx: Context
    ) -> object:
        message = InvokeMessage(
            version=2,
            type="invoke",
            request_id=_request_id(),
            tool_name=tool_name,
            arguments=arguments,
        )
        progress_tunnel.bind(message.request_id, ctx)
        pump_task = asyncio.create_task(
            progress_tunnel.pump(message.request_id, ctx)
        )
        try:
            return await registry.invoke(client_id, message, timeout_seconds)
        finally:
            progress_tunnel.unbind(message.request_id)
            try:
                await pump_task
            except asyncio.CancelledError:
                pass

    @mcp.tool(output_schema=None)
    async def relay_client_status(ctx: Context) -> dict[str, Any]:
        """Report the connected Client's real runtime status; remote call."""
        try:
            return cast(dict[str, Any], await _dispatch("client.status", {}, ctx))
        except _RELAY_FAILURES as error:
            raise ToolError(
                _dispatch_failure_message(error, command_dispatched=False)
            ) from None
        except ToolError:
            raise
        except Exception:
            raise ToolError("internal relay error") from None

    @mcp.tool(output_schema=None)
    async def relay_mcp_list(
        alias: str | None = None,
        tool: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        *,
        ctx: Context,
    ) -> dict[str, Any]:
        """Discover configured MCP servers and their tools; always available.

        Returns the Client's own closed response object: servers, tools or
        the full tool detail, with the catalog revision and next cursor.
        """
        try:
            arguments: dict[str, object] = {}
            if alias is not None:
                arguments["alias"] = alias
            if tool is not None:
                arguments["tool"] = tool
            if limit is not None:
                arguments["limit"] = limit
            if cursor is not None:
                arguments["cursor"] = cursor
            return cast(dict[str, Any], await _dispatch("mcp.list", arguments, ctx))
        except _RELAY_FAILURES as error:
            raise ToolError(
                _dispatch_failure_message(error, command_dispatched=False)
            ) from None
        except ToolError:
            raise
        except Exception:
            raise ToolError("internal relay error") from None

    @mcp.tool(output_schema=None)
    async def relay_mcp_command(
        alias: str,
        tool: str,
        arguments: dict[str, object],
        catalog_revision: str,
        *,
        ctx: Context,
    ) -> CallToolResult:
        """Execute one discovered third-party MCP tool; native result back.

        Requires the catalog revision from discovery. The result is the
        target tool's own native CallToolResult — multimodal content,
        structuredContent and isError are relayed intact; it may carry
        destructive effects. Relay failures are bounded isError results
        carrying the closed {code, message, execution_state} object.
        """
        try:
            native = cast(
                CallToolResult,
                await _dispatch(
                    "mcp.command",
                    {
                        "alias": alias,
                        "tool": tool,
                        "arguments": arguments,
                        "catalog_revision": catalog_revision,
                    },
                    ctx,
                ),
            )
        except _RELAY_FAILURES as error:
            return relay_error_result(_dispatch_failure_error(error))
        except ToolError:
            raise
        except Exception:
            return relay_error_result(
                RelayToolError("internal_error", "internal relay error", execution_state="not_started")
            )
        return native

    # Admin CRUD tools: the Client's result frame arrives as a NATIVE
    # CallToolResult from the registry (Tranche 4: converted exactly once at
    # the WS ingress) — structuredContent carries the capability's closed
    # dict. Dispatch failures render the closed error object as isError=true,
    # exactly like relay_mcp_command. They answer structured_output=False so
    # the SDK's output validation never re-interprets the payload.
    @mcp.tool(output_schema=None)
    async def relay_mcp_add(
        alias: str,
        entry: dict[str, object],
        env: dict[str, str] | None = None,
        *,
        ctx: Context,
    ) -> CallToolResult:
        """Declare and start a new MCP server alias on the Client (admin)."""
        try:
            native = cast(
                CallToolResult,
                await _dispatch(
                    "mcp.add", {"alias": alias, "entry": entry, "env": env}, ctx
                ),
            )
        except _RELAY_FAILURES as error:
            return relay_error_result(_dispatch_failure_error(error))
        except ToolError:
            raise
        except Exception:
            return relay_error_result(
                RelayToolError("internal_error", "internal relay error", execution_state="not_started")
            )
        return native

    @mcp.tool(output_schema=None)
    async def relay_mcp_modify(
        alias: str,
        entry: dict[str, object],
        env: dict[str, str] | None = None,
        *,
        ctx: Context,
    ) -> CallToolResult:
        """Replace an existing MCP server alias entry on the Client (admin)."""
        try:
            native = cast(
                CallToolResult,
                await _dispatch(
                    "mcp.modify", {"alias": alias, "entry": entry, "env": env}, ctx
                ),
            )
        except _RELAY_FAILURES as error:
            return relay_error_result(_dispatch_failure_error(error))
        except ToolError:
            raise
        except Exception:
            return relay_error_result(
                RelayToolError("internal_error", "internal relay error", execution_state="not_started")
            )
        return native

    @mcp.tool(output_schema=None)
    async def relay_mcp_delete(
        alias: str,
        *,
        ctx: Context,
    ) -> CallToolResult:
        """Stop and remove an MCP server alias from the Client (admin)."""
        try:
            native = cast(
                CallToolResult,
                await _dispatch("mcp.delete", {"alias": alias}, ctx),
            )
        except _RELAY_FAILURES as error:
            return relay_error_result(_dispatch_failure_error(error))
        except ToolError:
            raise
        except Exception:
            return relay_error_result(
                RelayToolError("internal_error", "internal relay error", execution_state="not_started")
            )
        return native

    @mcp.tool(output_schema=None)
    async def relay_mcp_enable(
        alias: str,
        *,
        ctx: Context,
    ) -> CallToolResult:
        """Enable an MCP server alias on the Client (admin)."""
        try:
            native = cast(
                CallToolResult,
                await _dispatch("mcp.enable", {"alias": alias}, ctx),
            )
        except _RELAY_FAILURES as error:
            return relay_error_result(_dispatch_failure_error(error))
        except ToolError:
            raise
        except Exception:
            return relay_error_result(
                RelayToolError("internal_error", "internal relay error", execution_state="not_started")
            )
        return native

    @mcp.tool(output_schema=None)
    async def relay_mcp_disable(
        alias: str,
        *,
        ctx: Context,
    ) -> CallToolResult:
        """Disable an MCP server alias on the Client; the entry is kept (admin)."""
        try:
            native = cast(
                CallToolResult,
                await _dispatch("mcp.disable", {"alias": alias}, ctx),
            )
        except _RELAY_FAILURES as error:
            return relay_error_result(_dispatch_failure_error(error))
        except ToolError:
            raise
        except Exception:
            return relay_error_result(
                RelayToolError("internal_error", "internal relay error", execution_state="not_started")
            )
        return native

    return [
        "relay_client_status",
        "relay_mcp_list",
        "relay_mcp_command",
        "relay_mcp_add",
        "relay_mcp_modify",
        "relay_mcp_delete",
        "relay_mcp_enable",
        "relay_mcp_disable",
    ]


def create_mcp_http_app(
    mcp: FastMCP,
) -> Any:
    """Create the FastMCP Streamable HTTP app for the /mcp path.

    Host/Origin protection is intentionally disabled at this configuration
    point: the relay authenticates every request with its own Bearer token
    instead. No wildcard allowlist and no CORS middleware are added.
    """
    return mcp.http_app(
        path="/mcp",
        stateless_http=False,
        json_response=True,
        host_origin_protection=False,
    )


def _structured_error_json(code: str) -> str:
    """Encode one structured Relay control error for MCP tool-error results."""
    suggested = {
        "registry_unreachable": "retry_later",
    }.get(code)
    payload: dict[str, str] = {"code": code, "message": code.replace("_", " ")}
    if suggested is not None:
        payload["suggested_action"] = suggested
    return json.dumps(payload)
