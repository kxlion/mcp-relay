"""Minimal, bounded provider tool client boundary."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol

from pydantic import ValidationError

from ..diagnostics import debug as _debug_log
from ..json_bounds import JsonBoundsError, JsonValue, validate_json_bounds
from ..output_models import ProviderToolResult
from ..provider_tools import (
    MAX_PROVIDER_TOOLS,
    ProviderToolCatalog,
    ProviderToolDescriptor,
)

DEFAULT_PROVIDER_TIMEOUT_SECONDS = 30.0
DEFAULT_PROVIDER_CLOSE_TIMEOUT_SECONDS = 3.0


class ProviderToolError(RuntimeError):
    """A provider operation failed without exposing provider details."""


class ProviderResultTooLargeError(ProviderToolError):
    """A provider result exceeded a protocol size bound.

    Carries the closed wire code so the client can answer the caller with
    an honest ``result_too_large`` instead of an opaque failure. The
    optional ``detail`` names the binding RELAY_* bound and the payload
    measurement (e.g. ``RELAY_MAX_RESULT_NODES: 65536 < payload: at least
    65537 nodes``); node traversal reports a safe lower bound while byte
    measurements are exact. It is relay-constructed text, never provider
    material.
    """

    code = "result_too_large"
    wire_message = "provider result exceeds the protocol size bound"

    def __init__(self, detail: str | None = None) -> None:
        if detail:
            super().__init__(detail)
        else:
            super().__init__()
        self.detail = detail


def _catalog_failure_category(
    values: Sequence[ProviderToolDescriptor | Mapping[str, object]],
    error: Exception,
) -> str:
    """Map a catalog validation failure to one closed diagnostic category.

    Mirrors the categorized ``provider inventory failure`` debug lines of
    the page reader: the operator log must name the refusal reason without
    leaking provider payloads.
    """
    entries = list(values)
    if len(entries) > MAX_PROVIDER_TOOLS:
        return "too-many-tools"
    names: list[str] = []
    for entry in entries:
        if isinstance(entry, ProviderToolDescriptor):
            names.append(entry.tool_name)
        elif isinstance(entry, Mapping):
            raw_name = entry.get("name", entry.get("tool_name"))
            if isinstance(raw_name, str):
                names.append(raw_name)
    if len(names) != len(entries) or len(set(names)) != len(names):
        return "duplicate-tool-names"
    if isinstance(error, ValidationError):
        return "invalid-entry"
    return "catalog-bounds"


class ProviderConnectionError(ProviderToolError):
    """The locally configured provider transport was unavailable."""


class ProviderUnavailableError(ProviderToolError):
    """The provider client refused before any MCP send: alias unavailable."""


class ProviderStaleInventoryError(ProviderToolError):
    """The cached inventory is not executable; refused before any MCP send."""



def bounded_error_detail(error: BaseException) -> str:
    """Bounded single-line root cause: type, message, and group leaf types."""
    detail = f"{type(error).__name__}: {error}".replace("\n", " ")[:200]
    if isinstance(error, BaseExceptionGroup):
        leaves = ", ".join(
            type(leaf).__name__ for leaf in error.exceptions[:8]
        )
        if len(error.exceptions) > 8:
            leaves += ", ..."
        detail = f"{detail} [{leaves}]"[:300]
    return detail


def exception_type_chain(error: BaseException) -> tuple[str, ...]:
    """Bounded exception types, including SDK groups; never messages or args.

    Raw exceptions can embed argv, URLs, credentials or upstream payloads.
    Keep their causal structure useful without copying that material to logs.
    """
    names: list[str] = []
    seen: set[int] = set()
    # ExceptionGroup leaves first (Luna's review): SDK root causes must not
    # be starved by a long outer cause chain under the shared node budget.
    pending: list[BaseException] = [error]
    while pending and len(names) < 8:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        names.append(type(current).__name__[:64])
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions[:8])
            continue
        cause = current.__cause__
        if cause is None and not current.__suppress_context__:
            cause = current.__context__
        if cause is not None:
            pending.append(cause)
    if pending:
        names.append("...")
    return tuple(names)


class ProviderTimeoutError(ProviderToolError):
    """A provider operation exceeded its configured deadline."""


class _ProviderDeadlineExceeded(ProviderTimeoutError):
    """Internal marker emitted only by the adapter's bounded runner."""


class ProviderCleanupError(ProviderToolError):
    """Provider cleanup failed without exposing provider details."""


class UnknownProviderToolError(ProviderToolError):
    """A call named a tool outside the provider's bounded inventory."""


class ProviderToolClient(Protocol):
    async def list_tools(self) -> Sequence[ProviderToolDescriptor]: ...

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> ProviderToolResult: ...

    async def close(self) -> None: ...


class ProviderAvailability(Protocol):
    """Optional lifecycle signal for clients that can become unavailable."""

    async def wait_unavailable(self) -> None: ...


def validate_timeout(value: float, *, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label} must be greater than zero")
    return float(value)


def bounded_descriptors(
    values: Sequence[ProviderToolDescriptor | Mapping[str, object]],
) -> tuple[ProviderToolDescriptor, ...]:
    try:
        catalog = ProviderToolCatalog.model_validate({"tools": list(values)})
    except (ValidationError, JsonBoundsError, TypeError, ValueError) as error:
        _debug_log(f"provider catalog failure: category={_catalog_failure_category(values, error)}")
        invalid = True
    else:
        return tuple(catalog.tools)
    if invalid:
        raise ProviderToolError("invalid provider tool inventory")


def bounded_arguments(arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    try:
        validate_json_bounds(arguments, require_object=True, label="provider arguments")
    except (JsonBoundsError, TypeError, ValueError):
        invalid = True
    else:
        return arguments
    if invalid:
        raise ProviderToolError("invalid provider arguments")


def validate_provider_arguments(
    descriptor: ProviderToolDescriptor,
    arguments: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue]:
    """Bound one argument object for transport to a provider tool.

    The driver remains the sole validator; the relay enforces transport
    bounds only (a JSON object of finite size and depth) and never
    interprets the tool's declared schema. ``descriptor`` is accepted to
    keep the per-tool invocation boundary explicit at call sites.
    """
    del descriptor
    return bounded_arguments(arguments)


_SIZE_ERROR_TYPES = frozenset(
    {"string_too_long", "too_long", "list_too_long", "set_too_long"}
)


def _result_failure_category(error: Exception) -> str:
    """Map a result validation failure to one closed diagnostic category.

    Mirrors the catalog-side categorized debug lines: the operator log
    must name the refusal reason without leaking provider payloads.
    Per-block size refusals surface as pydantic ``ValidationError`` with a
    ``*_too_long`` type; aggregate bound refusals raise ``JsonBoundsError``
    (directly, or wrapped by pydantic as a ``value_error`` entry whose
    ``ctx.error`` is the original ``JsonBoundsError``).
    """
    if isinstance(error, JsonBoundsError):
        return "result-oversized"
    if isinstance(error, ValidationError):
        for entry in error.errors():
            if entry.get("type") in _SIZE_ERROR_TYPES:
                return "result-oversized"
            wrapped = entry.get("ctx", {}).get("error")
            if isinstance(wrapped, JsonBoundsError) or (
                isinstance(wrapped, ValueError)
                and isinstance(wrapped.__cause__, JsonBoundsError)
            ):
                return "result-oversized"
        return "result-invalid-shape"
    return "result-unexpected"


def _bound_detail(error: JsonBoundsError) -> str:
    """Return the measured-vs-bound detail when the error carries one.

    Honest-refusal contract (2026-09-09): a result refusal must say WHICH
    RELAY_* bound and HOW BIG the payload was. Detail text is produced by
    ``json_bounds`` itself (relay wording, no provider material).
    """
    message = str(error)
    if "RELAY_MAX_" in message and "payload" in message:
        return message
    return ""


def bounded_result(value: object) -> ProviderToolResult:
    try:
        return ProviderToolResult.model_validate(_model_data(value))
    except (ValidationError, JsonBoundsError, TypeError, ValueError) as error:
        category = _result_failure_category(error)
        detail = _bound_detail(error) if isinstance(error, JsonBoundsError) else ""
        if not detail and isinstance(error, ValidationError):
            for entry in error.errors():
                wrapped = entry.get("ctx", {}).get("error")
                if isinstance(wrapped, JsonBoundsError):
                    detail = _bound_detail(wrapped)
                    if detail:
                        break
        _debug_log(
            "provider result failure: "
            f"category={category}"
            + (f" | {detail}" if detail else "")
        )
        if category == "result-oversized":
            raise ProviderResultTooLargeError(detail) from None
        raise ProviderToolError("invalid provider result") from None


async def run_bounded(
    operation: Callable[[], Awaitable[object]],
    timeout_seconds: float,
    pending_tasks: set[asyncio.Task[object]],
) -> object:
    task = asyncio.create_task(operation())
    pending_tasks.add(task)
    task.add_done_callback(
        lambda completed: _release_task(completed, pending_tasks)
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if task in done:
        return task.result()

    # A provider may suppress cancellation. Detach it so the public operation
    # still returns at its deadline. The owning client keeps it registered until
    # it actually finishes because Python cannot force-kill an in-process task.
    task.cancel()
    raise _ProviderDeadlineExceeded("provider operation timed out")


async def drain_pending_tasks(
    pending_tasks: set[asyncio.Task[object]], timeout_seconds: float
) -> bool:
    tasks = set(pending_tasks)
    if not tasks:
        return True
    for task in tasks:
        task.cancel()
    try:
        done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout_seconds))
    except asyncio.CancelledError:
        for task in pending_tasks:
            task.cancel()
        raise
    for task in done:
        _release_task(task, pending_tasks)
    return not pending


def _release_task(
    task: asyncio.Task[object], pending_tasks: set[asyncio.Task[object]]
) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass
    pending_tasks.discard(task)


def _model_data(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


__all__ = [
    "DEFAULT_PROVIDER_CLOSE_TIMEOUT_SECONDS",
    "DEFAULT_PROVIDER_TIMEOUT_SECONDS",
    "ProviderConnectionError",
    "ProviderCleanupError",
    "ProviderAvailability",
    "ProviderStaleInventoryError",
    "ProviderToolClient",
    "ProviderToolError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "UnknownProviderToolError",
    "validate_provider_arguments",
]
