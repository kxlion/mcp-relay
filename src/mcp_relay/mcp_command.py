"""Third-party command execution: resolve, send once, return the native result.

Runs inside the Relay Client. Every refusal before the MCP send is
``not_started``; any failure after it is ``unknown`` because the relay cannot
prove the tool had no effect. MCP-native ``isError`` results are relayed
intact. Nothing is ever replayed.
"""

from __future__ import annotations

import asyncio
from typing import Any

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


class CommandError(Exception):
    """A closed-code command failure: {code, message, execution_state}."""

    def __init__(
        self, code: str, message: str, *, execution_state: str = "not_started"
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message[:512]
        self.execution_state = execution_state

    def to_payload(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "execution_state": self.execution_state,
        }


def _validate_envelope(arguments: object) -> tuple[str, str, JsonObject]:
    """Closed envelope: exactly alias, tool and an arguments object."""
    if not isinstance(arguments, dict) or set(arguments) != {
        "alias",
        "tool",
        "arguments",
    }:
        raise CommandError(
            "invalid_arguments", "command requires exactly alias, tool, arguments"
        )
    alias, tool, inner = arguments["alias"], arguments["tool"], arguments["arguments"]
    if not isinstance(alias, str) or not alias or not isinstance(tool, str) or not tool:
        raise CommandError("invalid_arguments", "alias and tool must be names")
    try:
        validate_json_bounds(inner, require_object=True, label="arguments")
    except (JsonBoundsError, TypeError, ValueError):
        raise CommandError(
            "invalid_arguments", "arguments exceed transport bounds"
        ) from None
    return alias, tool, dict(inner)


async def execute_command(
    arguments: object, *, catalog: ClientCatalog
) -> ProviderToolResult:
    """Validate, resolve the current route, send once, return the result.

    The route is resolved and the call issued without an intervening await,
    so a catalog change can refuse the call but never redirect it.
    """
    alias, tool, inner_arguments = _validate_envelope(arguments)
    try:
        provider: Any = catalog.route(alias, tool)
    except CatalogError as error:
        raise CommandError(error.code, error.message) from None
    try:
        return await provider.call_tool(tool, inner_arguments)
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
