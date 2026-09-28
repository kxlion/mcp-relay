"""Adapter for a locally configured MCP provider transport."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol

from ..diagnostics import debug as _debug_log
from ..json_bounds import (
    JsonBoundsError,
    JsonValue,
    validate_json_bounds,
)
from ..output_models import ProviderToolResult
from ..provider_tools import (
    MAX_PROVIDER_DESCRIPTION_LENGTH,
    MAX_PROVIDER_TOOLS,
    ProviderToolDescriptor,
)
from .base import (
    DEFAULT_PROVIDER_CLOSE_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ProviderCleanupError,
    ProviderConnectionError,
    ProviderStaleInventoryError,
    ProviderToolError,
    ProviderUnavailableError,
    UnknownProviderToolError,
    _ProviderDeadlineExceeded,
    bounded_arguments,
    bounded_descriptors,
    bounded_error_detail,
    bounded_result,
    drain_pending_tasks,
    exception_type_chain,
    run_bounded,
    validate_timeout,
)


class McpTransport(Protocol):
    async def list_tools(self, cursor: str | None = None) -> object: ...

    async def call_tool(
        self, name: str, arguments: Mapping[str, JsonValue]
    ) -> object: ...

    async def close(self) -> None: ...


def _debug_inventory_failure(provider_name: str, category: str) -> None:
    del provider_name
    _debug_log(f"provider inventory failure: category={category}")


def _debug_descriptor_failure(provider_name: str, category: str) -> None:
    del provider_name
    _debug_log(f"provider descriptor failure: category={category}")


def _schema_failure_category(value: object) -> str | None:
    """Map schema-bound failures to a closed diagnostic category.

    The driver remains the sole validator: upstream schemas are announced
    as-is, so the only refusals here are transport-bound violations.
    """
    try:
        validate_json_bounds(value, require_object=True, label="schema")
    except (JsonBoundsError, TypeError, ValueError):
        return "transport-bounds"
    return None


def _safe_exception_chain(error: BaseException) -> str:
    return " <- ".join(exception_type_chain(error))


class McpProviderToolClient:
    """Expose one preconfigured local MCP connection as a bounded provider."""

    def __init__(
        self,
        transport: McpTransport,
        *,
        provider_name: str,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        close_timeout_seconds: float = DEFAULT_PROVIDER_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport
        self._provider_name = provider_name
        self._timeout_seconds = validate_timeout(
            timeout_seconds, label="timeout_seconds"
        )
        self._close_timeout_seconds = validate_timeout(
            close_timeout_seconds, label="close_timeout_seconds"
        )
        self._tools: tuple[ProviderToolDescriptor, ...] | None = None
        self._closed = False
        self._available = True
        self._unavailable = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._list_lock = asyncio.Lock()
        self._pending_tasks: set[asyncio.Task[object]] = set()
        # False between an upstream ``tools/list_changed`` and a good re-read.
        self._inventory_valid = True

    async def list_tools(
        self, *, timeout_seconds: float | None = None
    ) -> Sequence[ProviderToolDescriptor]:
        # The hub bounds the first listing by the remaining startup budget.
        budget = (
            self._timeout_seconds
            if timeout_seconds is None
            else validate_timeout(timeout_seconds, label="timeout_seconds")
        )
        deadline = asyncio.get_running_loop().time() + budget
        try:
            tools = await self._list_tools(deadline)
        except _ProviderDeadlineExceeded:
            self._mark_unavailable()
            raise
        except asyncio.CancelledError:
            self._mark_unavailable()
            raise
        except ProviderConnectionError:
            self._mark_unavailable()
            raise
        # A completed bounded read is by definition a fresh inventory: it
        # restores executability after an upstream change notification.
        self._inventory_valid = True
        return tools

    @property
    def inventory_valid(self) -> bool:
        """False between an upstream change notification and a good reread."""
        return self._inventory_valid

    def cached_inventory(self) -> Sequence[ProviderToolDescriptor] | None:
        """The current cached descriptors without any transport I/O."""
        return self._tools

    def bind_transport_notifications(
        self, on_tools_changed: Callable[[], Awaitable[None]]
    ) -> None:
        """Route the transport's upstream ``tools/list_changed`` to the owner."""
        self._transport.on_tools_changed = on_tools_changed

    def invalidate_inventory(self) -> None:
        """Mark the cached inventory non-executable until the next re-read."""
        if self._closed:
            return
        self._inventory_valid = False
        self._tools = None

    async def wait_unavailable(self) -> None:
        await self._unavailable.wait()

    async def _list_tools(
        self, deadline: float
    ) -> Sequence[ProviderToolDescriptor]:
        self._require_available()
        # An invalidated inventory is never served from cache: the bounded
        # re-read required by the upstream change happens right here.
        if self._tools is not None and self._inventory_valid:
            return self._tools
        async with self._list_lock:
            self._require_available()
            if self._tools is not None and self._inventory_valid:
                return self._tools
            try:
                descriptors: list[ProviderToolDescriptor] = []
                cursor: str | None = None
                seen_cursors: set[str] = set()
                loop = asyncio.get_running_loop()
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise _ProviderDeadlineExceeded(
                            "provider operation timed out"
                        )
                    response = await _list_page(
                        self._transport,
                        cursor,
                        remaining,
                        self._pending_tasks,
                    )
                    failure_category = "page-shape"
                    try:
                        raw_tools = _field(response, "tools")
                        if not isinstance(raw_tools, (list, tuple)):
                            failure_category = "tools-field"
                            raise ValueError
                        for tool in raw_tools:
                            descriptor = _descriptor(self._provider_name, tool)
                            descriptors.append(descriptor)
                        if len(descriptors) > MAX_PROVIDER_TOOLS:
                            failure_category = "too-many-tools"
                            raise ValueError
                        next_cursor = _field(response, "next_cursor", default=None)
                        if next_cursor is not None and (
                            not isinstance(next_cursor, str)
                            or not next_cursor
                            or len(next_cursor) > 2048
                            or next_cursor in seen_cursors
                            or len(seen_cursors) >= MAX_PROVIDER_TOOLS
                        ):
                            failure_category = "cursor"
                            raise ValueError
                    except (Exception,):
                        _debug_inventory_failure(
                            self._provider_name, failure_category
                        )
                        invalid_inventory = True
                    else:
                        invalid_inventory = False
                    if invalid_inventory:
                        raise ProviderToolError("invalid provider tool inventory")
                    if next_cursor is None:
                        break
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                self._tools = bounded_descriptors(descriptors)
            except (ProviderToolError, asyncio.CancelledError):
                raise
            return self._tools

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> ProviderToolResult:
        self._require_available()
        if not self._inventory_valid:
            # A notified inventory is not executable: refuse without touching
            # the transport. The bounded reread belongs to discovery, never to
            # an execution path.
            raise ProviderStaleInventoryError("provider tool inventory is stale")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_seconds
        try:
            tools = await self._list_tools(deadline)
        except asyncio.CancelledError:
            self._mark_unavailable()
            raise
        except ProviderToolError as error:
            # Pre-send only: an inventory reread failure (connection, bounds,
            # deadline) proves the MCP send never happened. One typed refusal
            # carries that proof downstream — no message sniffing.
            self._mark_unavailable()
            raise ProviderUnavailableError(
                "provider unavailable before dispatch "
                f"({bounded_error_detail(error)})"
            ) from None
        descriptor = next(
            (tool for tool in tools if tool.tool_name == tool_name),
            None,
        )
        if descriptor is None:
            raise UnknownProviderToolError("unknown provider tool")
        # The driver remains the sole validator; the relay enforces transport
        # bounds only and never interprets the declared schema.
        bounded_arguments(arguments)
        remaining = deadline - loop.time()
        if remaining <= 0:
            self._mark_unavailable()
            # Pre-send only: the reread succeeded, so the MCP send was never
            # reached even though the caller resumed past its deadline. Mirror
            # the reread-failure branch: one typed refusal carries the proof.
            deadline_exceeded = _ProviderDeadlineExceeded(
                "provider operation timed out"
            )
            raise ProviderUnavailableError(
                "provider unavailable before dispatch "
                f"({bounded_error_detail(deadline_exceeded)})"
            ) from None

        root_cause: BaseException | None = None
        try:
            raw_result = await run_bounded(
                lambda: self._transport.call_tool(tool_name, arguments),
                remaining,
                self._pending_tasks,
            )
        except _ProviderDeadlineExceeded:
            self._mark_unavailable()
            raise
        except asyncio.CancelledError:
            self._mark_unavailable()
            raise
        except (ConnectionError, OSError) as error:
            connection_failed = True
            call_failed = False
            root_cause = error
        except Exception as error:
            connection_failed = False
            call_failed = True
            root_cause = error
        else:
            connection_failed = call_failed = False
        if connection_failed:
            self._mark_unavailable()
            assert root_cause is not None
            raise ProviderConnectionError(
                f"provider connection failed ({bounded_error_detail(root_cause)})"
            ) from None
        if call_failed:
            assert root_cause is not None
            raise ProviderToolError(
                f"provider tool call failed ({bounded_error_detail(root_cause)})"
            ) from None
        return bounded_result(raw_result)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._mark_unavailable()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._close_timeout_seconds
            try:
                drained = await drain_pending_tasks(
                    self._pending_tasks, deadline - loop.time()
                )
            except asyncio.CancelledError:
                raise
            if not drained:
                raise ProviderCleanupError("provider cleanup failed")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ProviderCleanupError("provider cleanup failed")
            try:
                await run_bounded(
                    self._transport.close, remaining, self._pending_tasks
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                cleanup_failed = True
            else:
                cleanup_failed = False
            if cleanup_failed:
                raise ProviderCleanupError("provider cleanup failed") from None
            self._closed = True

    def _require_available(self) -> None:
        if not self._available:
            raise ProviderUnavailableError("provider client unavailable")

    def _mark_unavailable(self) -> None:
        self._available = False
        self._unavailable.set()


def _descriptor(provider_name: str, tool: object) -> ProviderToolDescriptor:
    name = _field(tool, "name")
    description = _field(tool, "description", default=None)
    if description is None or description == "":
        description = "Provider tool"
    elif isinstance(description, str):
        description = description[:MAX_PROVIDER_DESCRIPTION_LENGTH]
    input_schema = _field(tool, "input_schema", "inputSchema")
    output_schema = _field(tool, "output_schema", "outputSchema", default=None)
    # The driver remains the sole validator: upstream schemas are announced
    # as-is; only transport bounds (object, size, depth) are enforced here.
    input_schema_failure = _schema_failure_category(input_schema)
    output_schema_failure = (
        _schema_failure_category(output_schema) if output_schema is not None else None
    )
    schema_failure = None
    if input_schema_failure is not None:
        schema_failure = f"input-schema-{input_schema_failure}"
    elif output_schema_failure is not None:
        schema_failure = f"output-schema-{output_schema_failure}"
    annotations = _field(tool, "annotations", default={})
    if annotations is None:
        annotations = {}
    try:
        return ProviderToolDescriptor.model_validate(
            {
                "provider_name": provider_name,
                "tool_name": name,
                "description": description,
                "input_schema": input_schema,
                "output_schema": output_schema,
                "annotations": _model_data(annotations),
            }
        )
    except Exception as error:
        category = schema_failure or "model"
        error_details = getattr(error, "errors", None)
        if callable(error_details):
            try:
                details = error_details()
                if isinstance(details, list):
                    for detail in details:
                        if not isinstance(detail, Mapping):
                            continue
                        location = detail.get("loc", ())
                        if location:
                            field = str(location[0])
                            if field in {
                                "inputSchema",
                                "input_schema",
                                "outputSchema",
                                "output_schema",
                                "annotations",
                                "description",
                                "name",
                                "tool_name",
                            } and schema_failure is None:
                                category = field.replace("_", "-")
                                break
            except Exception:
                pass
        _debug_descriptor_failure(provider_name, category)
        raise


async def _list_page(
    transport: McpTransport,
    cursor: str | None,
    timeout_seconds: float,
    pending_tasks: set[asyncio.Task[object]],
) -> object:
    try:
        response = await run_bounded(
            lambda: transport.list_tools(cursor), timeout_seconds, pending_tasks
        )
    except _ProviderDeadlineExceeded:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as error:
        failure = ProviderConnectionError(
            f"provider connection failed ({bounded_error_detail(error)})"
        )
    else:
        return response
    # Raise outside the handler: the handler's raw exception must not become
    # an implicit __context__ on the sanitized provider error.
    raise failure from None


_MISSING = object()


def _field(value: object, *names: str, default: object = _MISSING) -> object:
    """Read the first present field; canonical SDK v2 name comes first.

    The first name is the SDK v2 canonical attribute and the only one read
    on SDK objects — their deprecated alias properties
    (``nextCursor``, ``inputSchema``, ...) emit ``FastMCPDeprecationWarning``
    on access. Later names are wire aliases accepted on raw mappings only.
    """
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    elif names and hasattr(value, names[0]):
        return getattr(value, names[0])
    if default is not _MISSING:
        return default
    raise ValueError("provider response is missing a required field")


def _model_data(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


__all__ = [
    "McpProviderToolClient",
    "McpTransport",
]
