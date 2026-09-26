"""Regression: unknown top-level result fields pass through, bounded and opaque.

Root cause of the relayed-invocation bug (2026-09-05/06): the MCP SDK adds
``resultType`` to ``CallToolResult``; the closed ``ProviderToolResult`` model
rejected it, failing every relayed call to a real third-party MCP server.
The relay stays neutral: unknown fields are preserved (pass-through) within
transport bounds, never interpreted.
"""

from __future__ import annotations

from typing import Any

import pytest

from mcp_relay.json_bounds import MAX_TOOL_RESULT_BYTES
from mcp_relay.output_models import ProviderToolResult
from mcp_relay.providers.base import ProviderResultTooLargeError, bounded_result

# Captured verbatim from the SDK's CallToolResult for a real
# ``uvx mcp-server-fetch`` call (fetch https://example.com, 2026-09-06).
REAL_FETCH_SDK_RESULT: dict[str, Any] = {
    "content": [
        {
            "type": "text",
            "text": (
                "Contents of https://example.com/:\n"
                "This domain is for use in documentation examples without "
                "needing permission. Avoid use in operations.\n\n"
                "[Learn more](https://iana.org/domains/example)"
            ),
        }
    ],
    "isError": False,
    "resultType": "complete",
}


def test_real_sdk_result_with_result_type_validates() -> None:
    validated = ProviderToolResult.model_validate(REAL_FETCH_SDK_RESULT)

    assert validated.content[0].text.startswith("Contents of https://example.com/")
    assert validated.is_error is False


def test_unknown_top_level_field_survives_round_trip() -> None:
    payload = {
        "content": [{"type": "text", "text": "x"}],
        "resultType": "complete",
        "futureField": {"a": [1, 2, 3]},
    }

    validated = ProviderToolResult.model_validate(payload)
    dumped = validated.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert dumped["resultType"] == "complete"
    assert dumped["futureField"] == {"a": [1, 2, 3]}
    assert dumped["content"] == [{"type": "text", "text": "x"}]


def test_bounded_result_preserves_sdk_extras() -> None:
    class FakeSdkResult:
        def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
            return dict(REAL_FETCH_SDK_RESULT)

    result = bounded_result(FakeSdkResult())

    assert result.model_dump(mode="json", by_alias=True)["resultType"] == "complete"


def test_oversized_unknown_field_is_still_rejected() -> None:
    payload = {
        "content": [{"type": "text", "text": "x"}],
        "futureField": "y" * (MAX_TOOL_RESULT_BYTES + 1),
    }

    with pytest.raises(Exception):
        ProviderToolResult.model_validate(payload)


def test_deeply_nested_unknown_field_is_still_rejected() -> None:
    deep: dict[str, Any] = {"value": 1}
    for _ in range(32):  # beyond MAX_JSON_DEPTH
        deep = {"nested": deep}
    payload = {
        "content": [{"type": "text", "text": "x"}],
        "futureField": deep,
    }

    with pytest.raises(Exception):
        ProviderToolResult.model_validate(payload)


def test_oversized_extras_fail_bounded_result_with_sanitized_error() -> None:
    class FakeSdkResult:
        def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "content": [{"type": "text", "text": "x"}],
                "futureField": "y" * (MAX_TOOL_RESULT_BYTES + 1),
            }

    with pytest.raises(ProviderResultTooLargeError):
        bounded_result(FakeSdkResult())
