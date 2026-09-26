"""Native MCP results at the bridge boundary, and Relay's closed errors.

The relay keeps ONE representation at the bridge: the bounded
``ProviderToolResult`` model (validation d'entrée: transport bounds, safe
URIs, wire-alias acceptance, unknown-field passthrough) going over the WS
frames, and the official SDK ``CallToolResult`` going to MCP clients.
``native_result`` renders the validated bounded result through the official
SDK model exactly once — content blocks (text, image, audio, embedded
resource, resource link), ``structuredContent``, ``isError`` and ``_meta``
are preserved by the SDK itself, never flattened to text, never wrapped in
``{result: ...}``, and never re-constructed by relay-side mapping code.

``relay_error_result`` renders a closed Relay failure
``{code, message, execution_state}`` as a single safe text block on an
``isError=true`` result, per the plan's error contract for list/command.
Those closed Relay models are Relay-owned and stay.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent

from .output_models import ProviderToolResult

__all__ = ["RelayToolError", "native_result", "relay_error_result"]

_MAX_ERROR_TEXT_LENGTH = 4096


class RelayToolError(Exception):
    """A closed-code Relay failure to render as an MCP error result."""

    def __init__(self, code: str, message: str, *, execution_state: str) -> None:
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


def native_result(result: ProviderToolResult | CallToolResult) -> CallToolResult:
    """Render the bounded result through the official SDK model, once.

    The SDK owns the MCP types: the validated bounded wire dump is handed to
    ``CallToolResult.model_validate`` and the relay adds no interpretation.
    A result that is already native is returned unchanged.
    """
    if isinstance(result, CallToolResult):
        return result
    payload: dict[str, Any] = result.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    return CallToolResult.model_validate(payload)


def relay_error_result(error: RelayToolError) -> CallToolResult:
    """Render a Relay failure as one bounded text block with ``isError``."""
    payload = json.dumps(error.to_payload(), ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(
        content=[TextContent(type="text", text=payload[:_MAX_ERROR_TEXT_LENGTH])],
        is_error=True,
    )
