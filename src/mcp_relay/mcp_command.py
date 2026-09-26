"""Third-party command execution: reserve, send once, return native result.

This module is the execution half of the fixed facade's ``relay_mcp_command``
operation, running inside the Relay Client. It validates the closed envelope,
resolves the exact ``(alias, tool)`` target through a catalog route
reservation acquired under snapshot synchronization, executes the upstream
tool exactly once, and returns the native ``ProviderToolResult`` untouched —
never passing through the control-result converter, never replaying.

Execution states on this path (plan "Erreurs et résultat incertain"):
- every refusal before the MCP send is ``not_started``;
- any failure after the send (transport/provider errors, timeout, lost
  response) is ``unknown`` — the relay cannot prove the tool had no effect;
- MCP-native ``isError`` results are relayed intact and are not mapped to
  Relay error codes.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from .json_bounds import JsonBoundsError, JsonObject, validate_json_bounds
from .mcp_catalog import CatalogError, ClientCatalog
from .output_models import ProviderToolResult
from .providers.base import (
    ProviderResultTooLargeError,
    ProviderStaleInventoryError,
    ProviderTimeoutError,
    ProviderToolError,
    ProviderUnavailableError,
    UnknownProviderToolError,
)

__all__ = ["CommandError", "execute_command"]

#: Closed Relay error codes the command/discovery paths may produce. The
#: catalog codes (``invalid_cursor``, ``result_too_large``) belong to the
#: fixed ``relay_mcp_list`` contract.
_COMMAND_CODES = frozenset(
    {
        "invalid_arguments",
        "catalog_stale",
        "invalid_cursor",
        "result_too_large",
        "alias_unknown",
        "alias_unavailable",
        "tool_unknown",
        "execution_failed",
        "timeout",
    }
)

_RESERVED_TARGET_WORDS = frozenset({"client", "mcp", "server"})

#: A hook run after the reservation was acquired but before the send, for
#: callers that must re-check volatile state under the same synchronization.
PostReservationCheck = Callable[[], Awaitable[None]]


class CommandError(Exception):
    """A closed-code command failure: {code, message, execution_state}."""

    def __init__(
        self, code: str, message: str, *, execution_state: str = "not_started"
    ) -> None:
        if code not in _COMMAND_CODES:  # pragma: no cover - developer guard
            raise AssertionError(f"unknown command error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message[:512]
        self.execution_state = execution_state

    @classmethod
    def from_catalog_error(cls, error: CatalogError) -> "CommandError":
        """Catalog refusals happen strictly before any send: not_started."""
        return cls(error.code, error.message, execution_state="not_started")

    def to_payload(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "execution_state": self.execution_state,
        }


def _validate_envelope(arguments: object) -> dict[str, Any]:
    """Closed envelope: alias, tool, arguments (object), catalog_revision."""
    if not isinstance(arguments, dict):
        raise CommandError(
            "invalid_arguments", "command arguments must be an object",
            execution_state="not_started",
        )
    if set(arguments) != {"alias", "tool", "arguments", "catalog_revision"}:
        raise CommandError(
            "invalid_arguments",
            "command requires exactly alias, tool, arguments, catalog_revision",
            execution_state="not_started",
        )
    alias = arguments["alias"]
    tool = arguments["tool"]
    revision = arguments["catalog_revision"]
    inner = arguments["arguments"]
    if not isinstance(alias, str) or not alias or alias in _RESERVED_TARGET_WORDS:
        raise CommandError(
            "invalid_arguments", "alias is not a valid target",
            execution_state="not_started",
        )
    if not isinstance(tool, str) or not tool:
        raise CommandError(
            "invalid_arguments", "tool is not a valid name",
            execution_state="not_started",
        )
    if not isinstance(revision, str) or not revision or len(revision) > 128:
        raise CommandError(
            "invalid_arguments", "catalog_revision is not a valid revision",
            execution_state="not_started",
        )
    try:
        validate_json_bounds(inner, require_object=True, label="arguments")
    except (JsonBoundsError, TypeError, ValueError):
        raise CommandError(
            "invalid_arguments", "arguments exceed transport bounds",
            execution_state="not_started",
        ) from None
    return {
        "alias": alias,
        "tool": tool,
        "arguments": dict(inner),
        "catalog_revision": revision,
    }


async def execute_command(
    arguments: object,
    *,
    catalog: ClientCatalog,
    on_reservation_check: PostReservationCheck | None = None,
) -> "CommandOutcome":
    """Validate, reserve, send once, and return the native result.

    The reservation is acquired through the catalog (which validates the
    revision, alias, availability, tool, and generation under its own
    synchronization). If ``on_reservation_check`` invalidates the catalog
    before the send, the reservation is cancelled and the call refuses with
    ``catalog_stale`` / ``not_started`` — never a redirect to a new route.
    """
    envelope = _validate_envelope(arguments)
    alias = envelope["alias"]
    tool = envelope["tool"]
    revision = envelope["catalog_revision"]
    inner_arguments: JsonObject = envelope["arguments"]
    try:
        reservation = catalog.reserve_route(alias, tool, revision)
    except CatalogError as error:
        raise CommandError.from_catalog_error(error) from None
    if on_reservation_check is not None:
        await on_reservation_check()
        if not reservation.still_valid(catalog):
            raise CommandError(
                "catalog_stale",
                "the catalog changed before dispatch",
                execution_state="not_started",
            )
    try:
        result = await reservation.provider.call_tool(tool, inner_arguments)
    except asyncio.CancelledError:
        raise
    except ProviderResultTooLargeError as error:
        # The tool itself ran and answered; the relay refused its RESULT
        # at the transport bound. Naming it tool_unknown would be false
        # (the tool is in the inventory) — use the reserved code instead,
        # carrying the measured-vs-bound detail when available.
        detail = getattr(error, "detail", None)
        raise CommandError(
            "result_too_large",
            f"tool '{tool}': {detail}" if detail else (
                f"the result of tool '{tool}' exceeded the relay transport bound"
            ),
            execution_state="unknown",
        ) from None
    except ProviderStaleInventoryError:
        # Provable pre-send refusal: the invalid inventory is not executable
        # and the MCP target was never contacted.
        raise CommandError(
            "alias_unavailable",
            "the alias inventory is not executable",
            execution_state="not_started",
        ) from None
    except UnknownProviderToolError:
        # The provider proved the tool absent before any send.
        raise CommandError(
            "tool_unknown",
            "the tool is not in the provider inventory",
            execution_state="not_started",
        ) from None
    except ProviderUnavailableError:
        # Provable pre-send refusal: the provider client never accepted the
        # operation, so the MCP target was never contacted.
        raise CommandError(
            "alias_unavailable",
            "the alias is not executable",
            execution_state="not_started",
        ) from None
    except (ProviderTimeoutError, asyncio.TimeoutError):
        # After dispatch the target's answer is missing: the relay cannot
        # prove the tool had no effect. Never replayed.
        raise CommandError(
            "timeout", "the target did not answer in time",
            execution_state="unknown",
        ) from None
    except ProviderToolError:
        # Post-send provider failure (transport or tool call): the request
        # may have reached the third-party MCP server.
        raise CommandError(
            "execution_failed",
            "the target failed after dispatch",
            execution_state="unknown",
        ) from None
    except Exception:
        raise CommandError(
            "execution_failed",
            "the target failed after dispatch",
            execution_state="unknown",
        ) from None
    return CommandOutcome(execution_state="not_started", result=result)


class CommandOutcome:
    """The successful execution result plus its declared execution state.

    ``not_started`` is the honest state here: the call returned a correlated
    MCP result, but the relay makes no claim about side effects — the state
    field is not an idempotency guarantee.
    """

    __slots__ = ("execution_state", "result")

    def __init__(self, *, execution_state: str, result: ProviderToolResult) -> None:
        self.execution_state = execution_state
        self.result = result
