"""The bounded result becomes native MCP at the bridge boundary — once.

The bounded ``ProviderToolResult`` (input validation, wire extras
passthrough) is rendered through the official SDK model (``mcp.types.CallToolResult``)
exactly once, and the facade hands that native result to MCP clients.
The closed Relay error result (``relay_error_result``) is Relay-owned and
stays.
"""

from __future__ import annotations

import pytest
from mcp.types import CallToolResult, EmbeddedResource, ImageContent, TextContent

from mcp_relay.mcp_results import (
    RelayToolError,
    native_result,
    relay_error_result,
)
from mcp_relay.output_models import ProviderToolResult

# ---------------------------------------------------------------------------
# Native rendering through the official SDK model
# ---------------------------------------------------------------------------


def test_native_result_preserves_text_structured_and_is_error() -> None:
    provider_result = ProviderToolResult.model_validate(
        {
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
            ],
            "structuredContent": {"answer": 42},
            "isError": False,
        }
    )

    result = native_result(provider_result)

    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    assert result.structured_content == {"answer": 42}
    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text == "hello"
    assert isinstance(result.content[1], ImageContent)


def test_native_result_wire_dump_uses_official_aliases() -> None:
    """The rendered result serializes with the SDK's own wire aliases."""
    provider_result = ProviderToolResult.model_validate(
        {
            "content": [{"type": "text", "text": "x"}],
            "structuredContent": {"a": 1},
            "isError": True,
            "_meta": {"k": "v"},
        }
    )

    dumped = native_result(provider_result).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )

    assert dumped["structuredContent"] == {"a": 1}
    assert dumped["isError"] is True
    assert dumped["_meta"] == {"k": "v"}
    assert "structured_content" not in dumped
    assert "is_error" not in dumped


def test_embedded_resource_content_is_preserved() -> None:
    provider_result = ProviderToolResult.model_validate(
        {
            "content": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "https://assets.example.test/report.txt",
                        "mimeType": "text/plain",
                        "text": "report body",
                    },
                }
            ],
        }
    )

    result = native_result(provider_result)

    assert isinstance(result.content[0], EmbeddedResource)
    assert result.content[0].resource.mime_type == "text/plain"


def test_blob_resource_content_is_preserved() -> None:
    provider_result = ProviderToolResult.model_validate(
        {
            "content": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "https://assets.example.test/icon.png",
                        "mimeType": "image/png",
                        "blob": "aGVsbG8=",
                    },
                }
            ],
        }
    )

    result = native_result(provider_result)

    resource = result.content[0]
    assert isinstance(resource, EmbeddedResource)
    assert getattr(resource.resource, "blob", None) == "aGVsbG8="


def test_mcp_is_error_results_are_relayed_not_mapped() -> None:
    provider_result = ProviderToolResult.model_validate(
        {
            "content": [{"type": "text", "text": "tool says no"}],
            "isError": True,
        }
    )

    result = native_result(provider_result)

    assert result.is_error is True
    assert result.content[0].text == "tool says no"


def test_empty_content_result_is_valid() -> None:
    result = native_result(ProviderToolResult(content=[]))

    assert result.content == []
    assert result.is_error is False


def test_meta_is_preserved_when_present() -> None:
    provider_result = ProviderToolResult.model_validate(
        {
            "content": [{"type": "text", "text": "x"}],
            "_meta": {"cursor": "abc"},
        }
    )

    result = native_result(provider_result)

    assert (result.meta or {}).get("cursor") == "abc"


def test_native_result_is_idempotent_on_native_input() -> None:
    native = CallToolResult(content=[TextContent(type="text", text="x")])

    assert native_result(native) is native


# ---------------------------------------------------------------------------
# Relay error mapping (Relay-owned closed models are kept)
# ---------------------------------------------------------------------------


def test_relay_error_becomes_is_error_text_result_with_closed_json() -> None:
    error = RelayToolError(
        "alias_unavailable", "the alias is not executable", execution_state="not_started"
    )

    result = relay_error_result(error)

    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    assert result.structured_content is None
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextContent)
    assert "alias_unavailable" in result.content[0].text
    assert "not_started" in result.content[0].text


def test_relay_error_message_is_bounded_to_512_characters() -> None:
    error = RelayToolError("execution_failed", "x" * 6000, execution_state="unknown")

    result = relay_error_result(error)

    text = result.content[0].text
    assert len(text) <= 4096
    assert "x" * 6000 not in text


def test_no_internal_exception_ever_reaches_the_public_result() -> None:
    error = RelayToolError("internal_error", "unexpected failure", execution_state="not_started")

    result = relay_error_result(error)

    text = result.content[0].text
    assert "Traceback" not in text
    assert "Exception" not in text


@pytest.mark.parametrize(
    ("code", "state"),
    [("catalog_stale", "not_started"), ("timeout", "unknown")],
)
def test_relay_error_codes_round_trip(code: str, state: str) -> None:
    error = RelayToolError(code, "m", execution_state=state)

    text = relay_error_result(error).content[0].text

    assert code in text
    assert state in text
