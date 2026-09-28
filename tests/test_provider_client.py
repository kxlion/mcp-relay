from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import cast

import pytest

from mcp_relay import diagnostics
from mcp_relay.json_bounds import (
    MAX_JSON_BYTES,
    MAX_JSON_DEPTH,
    MAX_TOOL_RESULT_BYTES,
    JsonValue,
)
from mcp_relay.output_models import ProviderToolResult
from mcp_relay.provider_tools import (
    MAX_PROVIDER_DESCRIPTION_LENGTH,
    ProviderToolDescriptor,
)
from mcp_relay.providers.base import (
    ProviderCleanupError,
    ProviderConnectionError,
    ProviderResultTooLargeError,
    ProviderStaleInventoryError,
    ProviderTimeoutError,
    ProviderToolError,
    ProviderUnavailableError,
    UnknownProviderToolError,
    validate_provider_arguments,
)
from mcp_relay.providers.mcp_client import (
    McpProviderToolClient,
    _schema_failure_category,
)

_OPAQUE_SCHEMA = {
    "anyOf": [
        {"type": "string", "maxLength": 256},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
        {
            "type": "array",
            "items": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "maxItems": 8,
        },
    ]
}
_NESTED_ITEM_SCHEMA = {
    "anyOf": [
        {"type": "integer"},
        {"type": "boolean"},
        {"type": "null"},
        {
            "type": "object",
            "properties": {"native": {"type": "string", "maxLength": 64}},
            "required": ["native"],
            "additionalProperties": False,
            "maxProperties": 1,
        },
    ]
}


def descriptor(name: str = "snapshot") -> ProviderToolDescriptor:
    return ProviderToolDescriptor(
        provider_name="cua",
        tool_name=name,
        description="A locally owned test tool",
        input_schema={
            "type": "object",
            "properties": {
                "nested": {
                    "type": "array",
                    "items": _NESTED_ITEM_SCHEMA,
                    "maxItems": 8,
                },
                "opaque": _OPAQUE_SCHEMA,
                "value": _OPAQUE_SCHEMA,
            },
            "additionalProperties": False,
        },
    )


def result(text: str = "ok") -> ProviderToolResult:
    return ProviderToolResult(content=[{"type": "text", "text": text}])


def assert_bounded_real_detail(error: BaseException, message: str) -> None:
    """Kevin (2026-09-07): real detail is welcome; the boundary is structure.

    No secret-scanning filter: provider text may appear, but it must stay
    single-line, bounded, and carry no raw cause/context in tracebacks.
    """
    assert str(error) == message
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "\n" not in str(error)
    assert len(str(error)) <= 200
    if "(" in str(error):
        # The wrapped root-cause type name leads the bounded detail.
        assert str(error).split("(")[1].split(":")[0].isidentifier()


def test_descriptors_cannot_serialize_execution_configuration() -> None:
    payload = descriptor().model_dump(mode="json", by_alias=True)
    for field in ("handler", "module", "executable", "method", "endpoint"):
        with pytest.raises(ValueError):
            ProviderToolDescriptor.model_validate(payload | {field: "secret"})


def test_provider_arguments_within_bounds_pass_without_schema_validation() -> None:
    """The driver remains the sole validator.

    Arguments within transport bounds reach the provider even when they do
    not match the declared schema; only bound violations are refused.
    """
    tool = ProviderToolDescriptor(
        provider_name="cua",
        tool_name="browser_type",
        description="type into a browser field",
        input_schema={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "minLength": 1},
                "tab_id": {"type": "string", "minLength": 1},
                "ref": {"type": "string", "minLength": 1},
                "text": {"type": "string", "maxLength": 8},
            },
            "required": ["target_id", "tab_id", "ref", "text"],
            "additionalProperties": False,
        },
    )

    valid_arguments: dict[str, JsonValue] = {
        "target_id": "target",
        "tab_id": "tab",
        "ref": "p1:0",
        "text": "hello",
    }
    assert validate_provider_arguments(tool, valid_arguments) == valid_arguments
    schema_nonconforming: tuple[dict[str, JsonValue], ...] = (
        {},
        {"target_id": "target", "tab_id": "tab", "ref": "p1:0"},
        {"target_id": "target", "tab_id": "tab", "ref": "p1:0", "text": "too long!"},
        {"target_id": "target", "tab_id": "tab", "ref": "p1:0", "text": "ok", "extra": True},
        {"target_id": "target", "tab_id": "tab", "ref": "p1:0", "text": 1},
    )
    for arguments in schema_nonconforming:
        assert validate_provider_arguments(tool, arguments) == arguments


def test_provider_arguments_beyond_transport_bounds_are_refused() -> None:
    tool = ProviderToolDescriptor(
        provider_name="cua",
        tool_name="browser_type",
        description="type into a browser field",
        input_schema={"type": "object", "additionalProperties": True},
    )
    with pytest.raises(ProviderToolError, match="invalid provider arguments"):
        validate_provider_arguments(tool, {"value": object()})  # type: ignore[dict-item]
    with pytest.raises(ProviderToolError, match="invalid provider arguments"):
        validate_provider_arguments(tool, {"value": "x" * (MAX_JSON_BYTES + 1)})


def test_provider_arguments_unbounded_keys_are_allowed_when_schema_is_open() -> None:
    descriptor = ProviderToolDescriptor(
        provider_name="cua",
        tool_name="search",
        description="open-schema test tool",
        input_schema=cast(
            "dict[str, JsonValue]",
            {
                "type": "object",
                "properties": {
                    "valid_upstream_key": {"type": "string", "maxLength": 64}
                },
                "additionalProperties": True,
            },
        ),
    )

    arguments: dict[str, JsonValue] = {
        "valid_upstream_key": "value",
        "extra": {"deep": [1, "two", None]},
    }
    assert validate_provider_arguments(descriptor, arguments) == arguments


def test_provider_arguments_absent_additional_properties_defaults_to_open() -> None:
    descriptor = ProviderToolDescriptor(
        provider_name="cua",
        tool_name="search",
        description="schema without additionalProperties",
        input_schema=cast(
            "dict[str, JsonValue]",
            {
                "type": "object",
                "properties": {
                    "valid_upstream_key": {"type": "string", "maxLength": 64}
                },
            },
        ),
    )

    arguments: dict[str, JsonValue] = {"valid_upstream_key": "value", "extra": True}
    assert validate_provider_arguments(descriptor, arguments) == arguments


def test_mcp_inventory_timeout_cannot_be_suppressed_into_success() -> None:
    class UncooperativeTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return await super().list_tools(cursor)

    async def scenario() -> None:
        client = McpProviderToolClient(
            UncooperativeTransport(),
            provider_name="cua-driver",
            timeout_seconds=0.01,
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(ProviderToolError) as caught:
            await client.list_tools()
        assert loop.time() - started < 0.05
        assert_bounded_real_detail(caught.value, "provider operation timed out")
        await asyncio.sleep(0)

    asyncio.run(scenario())


class FakeMcpTransport:
    def __init__(self) -> None:
        self.call_count = 0
        self.close_count = 0

    async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
        assert cursor is None
        return {
            "tools": [
                {
                    "name": "capture",
                    "description": "Capture the synthetic desktop",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"opaque": _OPAQUE_SCHEMA},
                        "additionalProperties": False,
                    },
                }
            ]
        }

    async def call_tool(
        self, name: str, arguments: Mapping[str, JsonValue]
    ) -> dict[str, object]:
        self.call_count += 1
        return {
            "content": [{"type": "text", "text": str(arguments["opaque"])}],
            "structuredContent": {"native": arguments["opaque"]},
            "isError": False,
        }

    async def close(self) -> None:
        self.close_count += 1


def test_mcp_adapter_maps_inventory_and_passes_native_result() -> None:
    async def scenario() -> None:
        transport = FakeMcpTransport()
        client = McpProviderToolClient(
            transport, provider_name="cua-driver"
        )
        tools = await client.list_tools()
        assert [(tool.provider_name, tool.tool_name) for tool in tools] == [
            ("cua-driver", "capture")
        ]
        output = await client.call_tool("capture", {"opaque": [1, None]})
        assert output.structured_content == {"native": [1, None]}
        assert output.content[0].type == "text"

    asyncio.run(scenario())


def test_mcp_adapter_relays_all_dynamic_tools_without_name_filtering() -> None:
    class ProviderWithMultipleTools(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            return {
                "tools": [
                    {
                        "name": "capture",
                        "description": "Capture the synthetic desktop",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"opaque": _OPAQUE_SCHEMA},
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "execute_javascript",
                        "description": "Synthetic opaque tool",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "javascript": {"type": "string", "maxLength": 256}
                            },
                            "additionalProperties": False,
                        },
                    },
                ]
            }

    async def scenario() -> None:
        client = McpProviderToolClient(
            ProviderWithMultipleTools(),
            provider_name="cua-driver",
        )
        assert [tool.tool_name for tool in await client.list_tools()] == ["capture", "execute_javascript"]

    asyncio.run(scenario())


def test_mcp_inventory_follows_pagination_and_caches_complete_result() -> None:
    class PaginatedTransport(FakeMcpTransport):
        def __init__(self) -> None:
            super().__init__()
            self.cursors: list[str | None] = []

        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            self.cursors.append(cursor)
            if cursor is None:
                return {
                    "tools": [self.tool("capture")],
                    "next_cursor": "page-2",
                }
            return {"tools": [self.tool("click")]}

        @staticmethod
        def tool(name: str) -> dict[str, object]:
            return {
                "name": name,
                "inputSchema": {"type": "object", "additionalProperties": False},
            }

    async def scenario() -> None:
        transport = PaginatedTransport()
        client = McpProviderToolClient(transport, provider_name="cua-driver")
        assert [tool.tool_name for tool in await client.list_tools()] == [
            "capture",
            "click",
        ]
        assert [tool.tool_name for tool in await client.list_tools()] == [
            "capture",
            "click",
        ]
        assert transport.cursors == [None, "page-2"]

    asyncio.run(scenario())


def test_mcp_inventory_uses_one_deadline_across_all_pages() -> None:
    class SlowPaginatedTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            await asyncio.sleep(0.035)
            return {"tools": [], "next_cursor": "page-2"} if cursor is None else {"tools": []}

    async def scenario() -> None:
        client = McpProviderToolClient(
            SlowPaginatedTransport(),
            provider_name="cua-driver",
            timeout_seconds=0.05,
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(ProviderToolError) as caught:
            await client.list_tools()
        elapsed = loop.time() - started
        assert_bounded_real_detail(caught.value, "provider operation timed out")
        assert elapsed < 0.07
        with pytest.raises(ProviderToolError, match="provider client unavailable"):
            await client.list_tools()

    asyncio.run(scenario())


def test_mcp_call_uses_one_deadline_for_inventory_and_invocation() -> None:
    class SlowTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            await asyncio.sleep(0.035)
            return await super().list_tools(cursor)

        async def call_tool(
            self, name: str, arguments: Mapping[str, JsonValue]
        ) -> dict[str, object]:
            await asyncio.sleep(0.035)
            return await super().call_tool(name, arguments)

    async def scenario() -> None:
        client = McpProviderToolClient(
            SlowTransport(), provider_name="cua-driver", timeout_seconds=0.05
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(ProviderToolError) as caught:
            await client.call_tool("capture", {"opaque": "value"})
        assert loop.time() - started < 0.07
        assert_bounded_real_detail(caught.value, "provider operation timed out")

    asyncio.run(scenario())


@pytest.mark.parametrize("bad_cursor", ["repeat", "", 7])
def test_mcp_inventory_rejects_bad_or_repeated_cursor_without_looping(
    bad_cursor: object,
) -> None:
    class BadCursorTransport(FakeMcpTransport):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            self.calls += 1
            next_cursor = cursor if bad_cursor == "repeat" and cursor else bad_cursor
            if bad_cursor == "repeat" and cursor is None:
                next_cursor = "same"
            return {"tools": [], "next_cursor": next_cursor}

    async def scenario() -> None:
        transport = BadCursorTransport()
        client = McpProviderToolClient(transport, provider_name="cua-driver")
        with pytest.raises(ProviderToolError, match="invalid provider tool inventory"):
            await client.list_tools()
        assert transport.calls <= 2

    asyncio.run(scenario())


def test_mcp_inventory_catalog_failure_logs_categorized_debug_line(
    tmp_path: Path,
) -> None:
    class DuplicateNamedToolsTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            return {
                "tools": [
                    {
                        "name": "capture",
                        "description": "Capture the synthetic desktop",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"opaque": _OPAQUE_SCHEMA},
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "capture",
                        "description": "Same name announced twice",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"opaque": _OPAQUE_SCHEMA},
                            "additionalProperties": False,
                        },
                    },
                ]
            }

    log_path = Path(tmp_path) / "client.log"
    diagnostics.set_log_file(log_path)
    try:
        async def scenario() -> None:
            transport = DuplicateNamedToolsTransport()
            client = McpProviderToolClient(transport, provider_name="cua-driver")
            with pytest.raises(
                ProviderToolError, match="invalid provider tool inventory"
            ):
                await client.list_tools()

        asyncio.run(scenario())
        file_text = log_path.read_text(encoding="utf-8")
    finally:
        diagnostics.set_log_file(None)
    assert "[DEBUG] provider catalog failure: category=duplicate-tool-names" in (
        file_text
    )


def test_provider_result_refusal_logs_categorized_debug_line(
    tmp_path: Path,
) -> None:
    class OversizedResultTransport(FakeMcpTransport):
        async def call_tool(
            self, name: str, arguments: Mapping[str, JsonValue]
        ) -> dict[str, object]:
            self.call_count += 1
            return {
                "content": [
                    {"type": "text", "text": "x" * (MAX_TOOL_RESULT_BYTES + 1)}
                ],
                "isError": False,
            }

    log_path = Path(tmp_path) / "client.log"
    diagnostics.set_log_file(log_path)
    try:
        async def scenario() -> None:
            transport = OversizedResultTransport()
            client = McpProviderToolClient(transport, provider_name="bigres")
            with pytest.raises(ProviderResultTooLargeError):
                await client.call_tool("capture", {"opaque": "x"})

        asyncio.run(scenario())
        file_text = log_path.read_text(encoding="utf-8")
    finally:
        diagnostics.set_log_file(None)
    assert "[DEBUG] provider result failure: category=result-oversized" in (
        file_text
    )


def test_mcp_unknown_tool_is_rejected_before_tools_call() -> None:
    async def scenario() -> None:
        transport = FakeMcpTransport()
        client = McpProviderToolClient(transport, provider_name="cua-driver")
        with pytest.raises(UnknownProviderToolError):
            await client.call_tool("missing", {})
        assert transport.call_count == 0

    asyncio.run(scenario())


def test_mcp_call_failure_message_includes_root_cause_detail() -> None:
    class FailingTransport(FakeMcpTransport):
        async def call_tool(
            self, name: str, arguments: Mapping[str, JsonValue]
        ) -> dict[str, object]:
            raise RuntimeError("boom-detail")

    async def scenario() -> None:
        client = McpProviderToolClient(
            FailingTransport(), provider_name="cua-driver"
        )
        with pytest.raises(ProviderToolError) as caught:
            await client.call_tool("capture", {"opaque": "value"})
        message = str(caught.value)
        assert "RuntimeError" in message
        assert "boom-detail" in message
        # Kevin (2026-09-07): no raw cause/context on provider errors.
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    asyncio.run(scenario())


def test_mcp_call_failure_detail_is_bounded_and_single_line() -> None:
    class FailingTransport(FakeMcpTransport):
        async def call_tool(
            self, name: str, arguments: Mapping[str, JsonValue]
        ) -> dict[str, object]:
            raise RuntimeError("boom line one\nboom line two " + "x" * 250)

    async def scenario() -> None:
        client = McpProviderToolClient(
            FailingTransport(), provider_name="cua-driver"
        )
        with pytest.raises(ProviderToolError) as caught:
            await client.call_tool("capture", {"opaque": "value"})
        message = str(caught.value)
        assert message.startswith(
            "provider tool call failed (RuntimeError: boom line one boom line two"
        )
        assert "\n" not in message
        assert len(message) <= len("provider tool call failed ()") + 200
        assert "x" * 201 not in message

    asyncio.run(scenario())


def test_mcp_connection_failure_message_includes_root_cause_detail() -> None:
    class FailingTransport(FakeMcpTransport):
        async def call_tool(
            self, name: str, arguments: Mapping[str, JsonValue]
        ) -> dict[str, object]:
            raise ConnectionError("refused-detail")

    async def scenario() -> None:
        client = McpProviderToolClient(
            FailingTransport(), provider_name="cua-driver"
        )
        with pytest.raises(ProviderConnectionError) as caught:
            await client.call_tool("capture", {"opaque": "value"})
        message = str(caught.value)
        assert message.startswith("provider connection failed")
        assert "ConnectionError" in message
        assert "refused-detail" in message
        assert "\n" not in message
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("path", "error_kind", "expected_error", "expected_message"),
    [
        pytest.param(
            "list", "OSError", ProviderConnectionError, "provider connection failed (OSError: wss://user:password@host/?token=very-secret)", id="mcp-list-connection"
        ),
        pytest.param(
            "call", "OSError", ProviderConnectionError, "provider connection failed (OSError: wss://user:password@host/?token=very-secret)", id="mcp-call-connection"
        ),
        pytest.param(
            "list", "ProviderToolError", ProviderConnectionError, "provider connection failed (ProviderToolError: wss://user:password@host/?token=very-secret)", id="mcp-list-tool"
        ),
        pytest.param(
            "call", "ProviderToolError", ProviderToolError, "provider tool call failed (ProviderToolError: wss://user:password@host/?token=very-secret)", id="mcp-call-tool"
        ),
    ],
)
def test_provider_errors_carry_bounded_real_detail_per_path(
    path: str,
    error_kind: str,
    expected_error: type[Exception],
    expected_message: str,
) -> None:
    secret_url = "wss://user:password@host/?token=very-secret"

    class BrokenTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            if path != "list":
                return await FakeMcpTransport.list_tools(self, cursor)
            raise OSError(secret_url) if error_kind == "OSError" else ProviderToolError(secret_url)

        async def call_tool(
            self, name: str, arguments: Mapping[str, JsonValue]
        ) -> dict[str, object]:
            if path != "call":
                return await FakeMcpTransport.call_tool(self, name, arguments)
            raise OSError(secret_url) if error_kind == "OSError" else ProviderToolError(secret_url)

    async def scenario() -> None:
        client = McpProviderToolClient(BrokenTransport(), provider_name="cua-driver")
        if path == "call":
            # Prime the inventory so the call path exercises a genuine
            # post-send failure, not the pre-send reread refusal.
            await client.list_tools()
        operation = client.list_tools() if path == "list" else client.call_tool("capture", {})
        with pytest.raises(expected_error) as caught:
            await operation
        assert_bounded_real_detail(caught.value, expected_message)

    asyncio.run(scenario())


def test_malformed_mcp_descriptor_is_inventory_error_without_sensitive_context() -> None:
    class MalformedTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            return {
                "tools": [
                    {
                        "name": "capture",
                        "inputSchema": "wss://user:password@host/?token=very-secret",
                    }
                ]
            }

    async def scenario() -> None:
        client = McpProviderToolClient(MalformedTransport(), provider_name="cua-driver")
        with pytest.raises(ProviderToolError) as caught:
            await client.list_tools()
        assert type(caught.value) is ProviderToolError
        assert_bounded_real_detail(caught.value, "invalid provider tool inventory")

    asyncio.run(scenario())


def test_provider_description_is_bounded_before_descriptor_validation() -> None:
    class LongDescriptionTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            return {
                "tools": [
                    {
                        "name": "capture",
                        "description": "x" * (MAX_PROVIDER_DESCRIPTION_LENGTH + 100),
                        "inputSchema": {"type": "object", "additionalProperties": False},
                    }
                ]
            }

    async def scenario() -> None:
        client = McpProviderToolClient(
            LongDescriptionTransport(), provider_name="cua"
        )
        tools = await client.list_tools()
        assert len(tools) == 1
        assert len(tools[0].description) == MAX_PROVIDER_DESCRIPTION_LENGTH

    asyncio.run(scenario())


def test_cua_array_schema_passes_through_without_added_bounds() -> None:
    """The upstream array schema is announced as-is; the driver validates it."""
    class UnboundedCuaArrayTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            return {
                "tools": [
                    {
                        "name": "click",
                        "description": "click",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "modifier": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                }
                            },
                            "additionalProperties": False,
                        },
                    }
                ]
            }

    async def scenario() -> None:
        client = McpProviderToolClient(
            UnboundedCuaArrayTransport(), provider_name="cua"
        )
        tools = await client.list_tools()
        # Pass-through: the upstream schema is announced as-is; the relay
        # adds no caps of its own and the driver stays the sole validator.
        assert tools[0].input_schema == {
            "type": "object",
            "properties": {
                "modifier": {
                    "type": "array",
                    "items": {"type": "string"},
                }
            },
            "additionalProperties": False,
        }

    asyncio.run(scenario())


def test_cua_schema_passes_through_unchanged() -> None:
    unbounded = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "string"}},
            "options": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        },
        "additionalProperties": True,
    }

    async def scenario() -> None:
        class SchemaTransport(FakeMcpTransport):
            async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
                del cursor
                return {
                    "tools": [
                        {
                            "name": "browser_navigate",
                            "description": "navigate",
                            "inputSchema": unbounded,
                        }
                    ]
                }

        client = McpProviderToolClient(SchemaTransport(), provider_name="custom")
        tools = await client.list_tools()
        # Pass-through contract: Relay publishes the provider's schema as-is
        # within transport bounds; the provider stays the sole validator.
        assert tools[0].input_schema == unbounded

    asyncio.run(scenario())


def test_cua_partial_combinator_branches_are_kept_not_dropped() -> None:
    """Driver 0.22.0 publishes conditional schemas like browser_prepare's.

    A combinator branch without ``type`` or its own ``additionalProperties``
    is a partial predicate. Relay is a neutral pass-through at the schema
    boundary: it keeps such branches as-is instead of dropping them or adding
    bounds, and the driver remains the sole validator of argument
    conditionality (pass-through contract).
    """

    class ConditionalCuaSchemaTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            return {
                "tools": [
                    {
                        "name": "browser_prepare",
                        "description": "prepare a browser",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "pid": {"type": "integer"},
                                "session": {"type": "string"},
                            },
                            "anyOf": [
                                {"required": ["pid"]},
                                {
                                    "properties": {
                                        "allow_launch": {"const": True},
                                        "profile": {
                                            "properties": {"mode": {"type": "string"}}
                                        },
                                    },
                                    "required": ["allow_launch", "profile"],
                                },
                            ],
                        },
                    }
                ]
            }

    async def scenario() -> None:
        client = McpProviderToolClient(
            ConditionalCuaSchemaTransport(), provider_name="cua"
        )
        (descriptor,) = await client.list_tools()
        schema = descriptor.input_schema

        assert "anyOf" in schema
        branches = schema["anyOf"]
        # The bare predicate is published exactly as the driver declared it:
        # kept, not dropped, and without relay-added bounds.
        first_branch = branches[0]
        assert first_branch == {"required": ["pid"]}

        validated = validate_provider_arguments(
            descriptor, {"pid": 4242, "session": "e2e"}
        )
        assert validated["pid"] == 4242

    asyncio.run(scenario())


def test_cua_open_object_schemas_pass_through_unchanged() -> None:
    """Relay is a neutral pass-through at the schema-semantics boundary.

    The provider's openness (``additionalProperties`` true, or inherited by
    omission) is preserved verbatim; Relay adds no caps and stays the memory
    guard through the global JSON bounds on raw arguments.
    """

    class OpenCuaSchemaTransport(FakeMcpTransport):
        async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
            return {
                "tools": [
                    {
                        "name": "browser_prepare",
                        "description": "prepare a browser",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "profile": {
                                    "type": "object",
                                    "properties": {"mode": {"type": "string"}},
                                },
                                "value": {"description": "JSON value"},
                            },
                        },
                        "outputSchema": {
                            "type": "object",
                            "additionalProperties": True,
                            "properties": {"status": {"type": "string"}},
                        },
                    }
                ]
            }

    async def scenario() -> None:
        client = McpProviderToolClient(
            OpenCuaSchemaTransport(), provider_name="cua"
        )
        tools = await client.list_tools()
        descriptor = tools[0]
        # Pass-through: input and output schemas are announced exactly as the
        # driver declared them, without relay-added caps or fallbacks.
        assert descriptor.input_schema == {
            "type": "object",
            "properties": {
                "profile": {
                    "type": "object",
                    "properties": {"mode": {"type": "string"}},
                },
                "value": {"description": "JSON value"},
            },
        }
        assert descriptor.output_schema == {
            "type": "object",
            "additionalProperties": True,
            "properties": {"status": {"type": "string"}},
        }

        validated = validate_provider_arguments(
            descriptor, {"profile": {"mode": "dark"}, "extra": [1, 2]}
        )
        assert validated["extra"] == [1, 2]

    asyncio.run(scenario())


def test_schema_failure_category_names_transport_bounds() -> None:
    assert _schema_failure_category(
        {"type": "object", "additionalProperties": False}
    ) is None
    too_deep: dict[str, object] = {"type": "object"}
    node = too_deep
    for _ in range(MAX_JSON_DEPTH + 8):
        child: dict[str, object] = {"type": "object"}
        node["properties"] = {"nested": child}
        node = child
    assert _schema_failure_category(too_deep) == "transport-bounds"
    assert _schema_failure_category(["not", "an", "object"]) == "transport-bounds"


def test_successful_close_is_idempotent() -> None:
    async def scenario() -> None:
        transport = FakeMcpTransport()
        client = McpProviderToolClient(transport, provider_name="cua-driver")
        await client.close()
        await client.close()
        assert transport.close_count == 1

    asyncio.run(scenario())


def test_successful_close_rejects_later_operations() -> None:
    async def scenario() -> None:
        client = McpProviderToolClient(
            FakeMcpTransport(), provider_name="cua-driver"
        )
        tool_name = "capture"
        await client.close()
        for operation in (client.list_tools(), client.call_tool(tool_name, {})):
            with pytest.raises(ProviderToolError) as caught:
                await operation
            assert_bounded_real_detail(caught.value, "provider client unavailable")

    async def _async_result() -> ProviderToolResult:
        return result()

    asyncio.run(scenario())


@pytest.mark.parametrize("first_outcome", ["cancel", "failure", "timeout"])
def test_close_can_retry_after_cancelled_or_failed_cleanup(
    first_outcome: str
) -> None:
    async def scenario() -> None:
        attempts = 0
        started = asyncio.Event()

        async def cleanup() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1 and first_outcome == "cancel":
                started.set()
                await asyncio.Event().wait()
            if attempts == 1 and first_outcome == "timeout":
                await asyncio.Event().wait()
            if attempts == 1:
                raise RuntimeError("synthetic cleanup failure")

        transport = FakeMcpTransport()
        transport.close = cleanup  # type: ignore[method-assign]
        client = McpProviderToolClient(
            transport,
            provider_name="cua-driver",
            close_timeout_seconds=0.01,
        )

        if first_outcome == "cancel":
            task = asyncio.create_task(client.close())
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ProviderCleanupError) as caught:
                await client.close()
            assert_bounded_real_detail(caught.value, "provider cleanup failed")

        await client.close()
        await client.close()
        assert attempts == 2

    async def _async_result() -> ProviderToolResult:
        return result()

    asyncio.run(scenario())


def test_close_does_not_overlap_uncooperative_cleanup() -> None:
    async def scenario() -> None:
        attempts = 0
        cancelled = asyncio.Event()
        release = asyncio.Event()

        async def cleanup() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        cancelled.set()

        transport = FakeMcpTransport()
        transport.close = cleanup  # type: ignore[method-assign]
        client = McpProviderToolClient(
            transport,
            provider_name="cua-driver",
            close_timeout_seconds=0.01,
        )

        with pytest.raises(ProviderCleanupError) as first:
            await client.close()
        assert_bounded_real_detail(first.value, "provider cleanup failed")
        await cancelled.wait()

        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(ProviderCleanupError) as second:
            await client.close()
        assert loop.time() - started < 0.05
        assert_bounded_real_detail(second.value, "provider cleanup failed")
        assert attempts == 1

        release.set()
        await asyncio.sleep(0)
        await client.close()
        await client.close()
        assert attempts == 2

    async def _async_result() -> ProviderToolResult:
        return result()

    asyncio.run(scenario())


def test_close_waits_for_uncooperative_provider_operation_before_cleanup(
) -> None:
    async def scenario() -> None:
        operation_cancelled = asyncio.Event()
        release = asyncio.Event()
        cleanup_attempts = 0

        async def operation() -> ProviderToolResult:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    operation_cancelled.set()
            return result("late")

        async def cleanup() -> None:
            nonlocal cleanup_attempts
            cleanup_attempts += 1

        transport = FakeMcpTransport()

        async def list_tools(cursor: str | None = None) -> object:
            return await operation()

        transport.list_tools = list_tools  # type: ignore[method-assign]
        transport.close = cleanup  # type: ignore[method-assign]
        client = McpProviderToolClient(
            transport,
            provider_name="cua-driver",
            timeout_seconds=0.01,
            close_timeout_seconds=0.01,
        )
        invoke = client.list_tools()

        with pytest.raises(ProviderToolError) as timed_out:
            await invoke
        assert_bounded_real_detail(timed_out.value, "provider operation timed out")
        await operation_cancelled.wait()

        with pytest.raises(ProviderCleanupError) as caught:
            await client.close()
        assert_bounded_real_detail(caught.value, "provider cleanup failed")
        assert cleanup_attempts == 0

        release.set()
        await asyncio.sleep(0)
        await client.close()
        await client.close()
        assert cleanup_attempts == 1


class _ChangeNotifyingTransport(FakeMcpTransport):
    """Emits the upstream ``tools/list_changed`` notification to the client."""

    def __init__(self) -> None:
        super().__init__()
        self.notifications: list[str] = []
        self.reads = 0
        self._changed_tools: list[dict[str, object]] | None = None
        self.on_tools_changed: Callable[[], Awaitable[None]] | None = None

    async def notify_tools_changed(
        self, tools: list[dict[str, object]] | None
    ) -> None:
        self._changed_tools = tools

    async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
        self.reads += 1
        if self._changed_tools is not None:
            return {"tools": self._changed_tools}
        return await super().list_tools(cursor)


class _CongestedClockTransport(FakeMcpTransport):
    """Mimics loop congestion: the reread succeeds, but by the time the
    task-done callback runs, the caller's clock is past its deadline."""

    def __init__(self) -> None:
        super().__init__()
        self.clock: dict[str, float] = {"t": 100.0}

    async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
        response = await super().list_tools(cursor)
        # The task completes; the done callback is scheduled before the wait
        # timeout, yet the loop clock has already passed the deadline.
        self.clock["t"] += 2.0
        return response


def test_notification_invalidates_inventory_and_forces_bounded_reread() -> None:
    async def scenario() -> None:
        transport = _ChangeNotifyingTransport()
        client = McpProviderToolClient(transport, provider_name="probe")
        first = await client.list_tools()
        assert [tool.name for tool in first] == ["capture"]

        client.invalidate_inventory()
        client.invalidate_inventory()  # idempotent
        assert not client.inventory_valid

        second = await client.list_tools()
        assert [tool.name for tool in second] == ["capture"]
        assert client.inventory_valid
        # Two full reads: the initial discovery and the post-notification reread.
        assert transport.reads == 2

    asyncio.run(scenario())


def test_notification_marks_inventory_unavailable_immediately() -> None:
    """A stale inventory is not executable between notification and reread."""

    async def scenario() -> None:
        transport = _ChangeNotifyingTransport()
        client = McpProviderToolClient(transport, provider_name="probe")
        await client.list_tools()

        client.invalidate_inventory()

        with pytest.raises(ProviderStaleInventoryError):
            await client.call_tool("capture", {"opaque": "x"})
        # No upstream call happened on the stale inventory.
        assert transport.call_count == 0

    asyncio.run(scenario())


def test_pre_send_reread_failure_refuses_as_unavailable_before_dispatch() -> None:
    """A pre-send inventory reread failure is one typed, honest refusal.

    The MCP target was never contacted: the provider says so with a typed
    exception instead of leaving the caller to sniff message fragments.
    """

    async def scenario() -> None:
        transport = _ChangeNotifyingTransport()

        async def broken_list_tools(cursor: str | None = None) -> object:
            raise ConnectionError("down")

        transport.list_tools = broken_list_tools  # type: ignore[method-assign]
        client = McpProviderToolClient(transport, provider_name="probe")
        # No prior discovery: call_tool must reread the inventory first; the
        # failure happens strictly before any MCP send.
        with pytest.raises(ProviderUnavailableError) as caught:
            await client.call_tool("capture", {"opaque": "x"})
        assert "before dispatch" in str(caught.value)
        assert transport.call_count == 0

    asyncio.run(scenario())


def test_pre_send_budget_exhaustion_after_reread_refuses_unavailable() -> None:
    """Loop congestion: the reread succeeds, the budget is spent at resumption.

    The reread task completes before the wait timeout handle runs, so the
    caller resumes with the clock already past the deadline. The MCP send was
    provably never reached: the refusal must stay a typed pre-send unavailable,
    not a post-send timeout/unknown.
    """

    async def scenario() -> None:
        transport = _CongestedClockTransport()
        client = McpProviderToolClient(
            transport, provider_name="probe", timeout_seconds=1
        )
        loop = asyncio.get_running_loop()
        real_time = loop.time

        def congested_time() -> float:
            return transport.clock["t"]

        loop.time = congested_time  # type: ignore[method-assign]
        try:
            with pytest.raises(ProviderUnavailableError) as caught:
                await client.call_tool("capture", {"opaque": "x"})
        finally:
            loop.time = real_time  # type: ignore[method-assign]
        assert "before dispatch" in str(caught.value)
        assert transport.call_count == 0

    asyncio.run(scenario())


def test_concurrent_notifications_coalesce_into_one_reread() -> None:
    async def scenario() -> None:
        transport = _ChangeNotifyingTransport()
        client = McpProviderToolClient(transport, provider_name="probe")
        await client.list_tools()
        baseline = transport.reads

        # Concurrent notifications are the same synchronous invalidation; the
        # shared reread happens at the next discovery, exactly once.
        client.invalidate_inventory()
        client.invalidate_inventory()
        await client.list_tools()

        assert transport.reads == baseline + 1
        assert client.inventory_valid

    asyncio.run(scenario())


def test_failed_reread_after_notification_reports_unavailable_inventory() -> None:
    async def scenario() -> None:
        transport = _ChangeNotifyingTransport()
        client = McpProviderToolClient(transport, provider_name="probe")
        await client.list_tools()

        async def broken_list_tools(cursor: str | None = None) -> object:
            raise ProviderConnectionError("provider connection failed")

        transport.list_tools = broken_list_tools  # type: ignore[method-assign]
        client.invalidate_inventory()

        assert not client.inventory_valid
        with pytest.raises(ProviderToolError):
            await client.list_tools()
        assert not client.inventory_valid

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# The startup-budget inventory override bounds one call only; the
# ordinary client deadline (30 s in production) stays untouched.
# --------------------------------------------------------------------------


def test_list_tools_without_override_uses_the_client_timeout() -> None:
    class Transport:
        async def list_tools(self, cursor: str | None = None) -> object:
            return {"tools": []}

        async def call_tool(
            self, name: str, arguments: Mapping[str, JsonValue]
        ) -> object:
            raise AssertionError("not used")

        async def close(self) -> None:
            return None

    client = McpProviderToolClient(
        Transport(), provider_name="probe", timeout_seconds=30.0
    )
    tools = asyncio.run(client.list_tools())
    assert list(tools) == []
    assert client._timeout_seconds == 30.0


def test_list_tools_timeout_override_bounds_only_that_call() -> None:
    class SlowTransport:
        async def list_tools(self, cursor: str | None = None) -> object:
            await asyncio.sleep(0.5)
            return {"tools": []}

        async def call_tool(
            self, name: str, arguments: Mapping[str, JsonValue]
        ) -> object:
            raise AssertionError("not used")

        async def close(self) -> None:
            return None

    client = McpProviderToolClient(
        SlowTransport(), provider_name="probe", timeout_seconds=30.0
    )

    async def scenario() -> None:
        with pytest.raises(ProviderTimeoutError):
            await asyncio.wait_for(
                client.list_tools(timeout_seconds=0.05), timeout=5.0
            )

    asyncio.run(scenario())
    # The client default is untouched: only the one startup call was bounded.
    assert client._timeout_seconds == 30.0
