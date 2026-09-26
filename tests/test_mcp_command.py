"""Tests for the third-party command execution path (``relay_mcp_command``).

The command module validates the closed envelope, resolves the exact
``(alias, tool)`` target through the catalog reservation, executes once
against the reserved route, and returns the native ``ProviderToolResult``
untouched. Every refusal before the MCP send carries ``not_started``; the
plan's ``unknown`` state is produced by the Server when a response is lost
after send, not here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from mcp_relay.json_bounds import JsonValue
from mcp_relay.mcp_catalog import CatalogError, ClientCatalog
from mcp_relay.mcp_command import CommandError, execute_command
from mcp_relay.output_models import ProviderToolResult
from mcp_relay.provider_tools import ProviderToolDescriptor
from mcp_relay.providers.base import (
    ProviderResultTooLargeError,
    ProviderStaleInventoryError,
    ProviderTimeoutError,
    ProviderToolError,
    ProviderUnavailableError,
    UnknownProviderToolError,
)
from mcp_relay.providers.mcp_client import McpProviderToolClient


class _RouteProvider:
    """Route stand-in recording invocations; never called on refusals."""

    def __init__(
        self,
        result: ProviderToolResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._result = result
        self._error = error

    async def call_tool(
        self, tool_name: str, arguments: dict[str, object]
    ) -> ProviderToolResult:
        self.calls.append((tool_name, dict(arguments)))
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _descriptor(name: str = "echo") -> ProviderToolDescriptor:
    return ProviderToolDescriptor.model_validate(
        {
            "provider_name": "probe",
            "tool_name": name,
            "description": "Echo back",
            "input_schema": {"type": "object", "properties": {}},
        }
    )


def _seeded_catalog(provider: object) -> ClientCatalog:
    from mcp_relay.mcp_catalog import AliasCatalog

    catalog = ClientCatalog()
    catalog.update_alias(
        AliasCatalog(
            alias="probe",
            enabled=True,
            runtime_state="running",
            transport="stdio",
            entry={"command": ["/bin/probe"]},
            last_error=None,
            catalog_available=True,
            discovery_error=None,
            descriptors=(_descriptor(),),
            provider=provider,
        )
    )
    return catalog


def _result() -> ProviderToolResult:
    return ProviderToolResult.model_validate(
        {
            "content": [{"type": "text", "text": "hello"}],
            "structuredContent": {"native": True},
            "isError": False,
        }
    )


# ---------------------------------------------------------------------------
# Envelope validation
# ---------------------------------------------------------------------------


def _run(arguments: object, catalog: ClientCatalog) -> object:
    return asyncio.run(execute_command(arguments, catalog=catalog))


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"alias": "probe", "tool": "echo", "catalog_revision": "r"},
        {
            "alias": "probe",
            "tool": "echo",
            "arguments": {},
            "catalog_revision": "r",
            "extra": 1,
        },
    ],
)
def test_envelope_is_closed_and_all_fields_required(arguments: dict) -> None:
    with pytest.raises(CommandError) as error:
        _run(arguments, catalog=_seeded_catalog(_RouteProvider()))
    assert error.value.code == "invalid_arguments"


def test_arguments_must_be_a_bounded_json_object() -> None:
    provider = _RouteProvider(result=_result())
    catalog = _seeded_catalog(provider)
    with pytest.raises(CommandError) as error:
        _run(
            {
                "alias": "probe",
                "tool": "echo",
                "arguments": "not-an-object",
                "catalog_revision": catalog.revision,
            },
            catalog=catalog,
        )
    assert error.value.code == "invalid_arguments"
    assert provider.calls == []


# ---------------------------------------------------------------------------
# Validation order: revision → alias → availability → tool (no provider call)
# ---------------------------------------------------------------------------


def test_stale_revision_refuses_before_any_provider_call() -> None:
    provider = _RouteProvider(result=_result())
    catalog = _seeded_catalog(provider)
    with pytest.raises(CommandError) as error:
        _run(
            {
                "alias": "probe",
                "tool": "echo",
                "arguments": {"text": "hello"},
                "catalog_revision": f"{'0' * 32}:0",
            },
            catalog=catalog,
        )
    assert error.value.code == "catalog_stale"
    assert provider.calls == []


def test_oversized_result_maps_to_result_too_large_with_tool_name() -> None:
    class OversizedProvider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call_tool(
            self, tool_name: str, arguments: dict[str, object]
        ) -> ProviderToolResult:
            self.calls.append((tool_name, dict(arguments)))
            raise ProviderResultTooLargeError()

    provider = OversizedProvider()
    catalog = _seeded_catalog(provider)
    with pytest.raises(CommandError) as oversized:
        _run(
            {
                "alias": "probe",
                "tool": "echo",
                "arguments": {},
                "catalog_revision": catalog.revision,
            },
            catalog=catalog,
        )
    # The tool WAS executed: the refusal concerns its result, not its name.
    assert provider.calls == [("echo", {})]
    assert oversized.value.code == "result_too_large"
    assert oversized.value.execution_state == "unknown"
    assert "echo" in oversized.value.message


def test_oversized_result_message_carries_bound_and_payload() -> None:
    """The relayed error says WHICH bound and HOW BIG the payload was."""

    class OversizedProvider:
        async def call_tool(
            self, tool_name: str, arguments: dict[str, object]
        ) -> ProviderToolResult:
            raise ProviderResultTooLargeError(
                "RELAY_MAX_TOOL_RESULT_BYTES: 64 < payload: 200 bytes"
            )

    catalog = _seeded_catalog(OversizedProvider())
    with pytest.raises(CommandError) as oversized:
        _run(
            {
                "alias": "probe",
                "tool": "echo",
                "arguments": {},
                "catalog_revision": catalog.revision,
            },
            catalog=catalog,
        )
    assert oversized.value.code == "result_too_large"
    assert oversized.value.message == (
        "tool 'echo': RELAY_MAX_TOOL_RESULT_BYTES: 64 < payload: 200 bytes"
    )


def test_unknown_alias_and_tool_refuse_without_provider_call() -> None:
    provider = _RouteProvider(result=_result())
    catalog = _seeded_catalog(provider)
    with pytest.raises(CommandError) as unknown:
        _run(
            {
                "alias": "nope",
                "tool": "echo",
                "arguments": {},
                "catalog_revision": catalog.revision,
            },
            catalog=catalog,
        )
    assert unknown.value.code == "alias_unknown"

    with pytest.raises(CommandError) as tool_unknown:
        _run(
            {
                "alias": "probe",
                "tool": "zap",
                "arguments": {},
                "catalog_revision": catalog.revision,
            },
            catalog=catalog,
        )
    assert tool_unknown.value.code == "tool_unknown"
    assert provider.calls == []


def test_unavailable_alias_refuses_without_provider_call() -> None:
    provider = _RouteProvider(result=_result())
    catalog = _seeded_catalog(provider)
    from mcp_relay.mcp_catalog import AliasCatalog

    catalog.update_alias(
        AliasCatalog(
            alias="probe",
            enabled=True,
            runtime_state="unavailable",
            transport="stdio",
            entry={"command": ["/bin/probe"]},
            last_error={"code": "spawn_failed", "message": "no"},
            catalog_available=False,
            discovery_error={"code": "spawn_failed", "message": "no"},
            descriptors=(),
            provider=None,
        )
    )
    with pytest.raises(CommandError) as error:
        _run(
            {
                "alias": "probe",
                "tool": "echo",
                "arguments": {},
                "catalog_revision": catalog.revision,
            },
            catalog=catalog,
        )
    assert error.value.code == "alias_unavailable"
    assert provider.calls == []


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def test_happy_path_sends_exact_tool_and_preserves_native_result() -> None:
    provider = _RouteProvider(result=_result())
    catalog = _seeded_catalog(provider)

    outcome = asyncio.run(
        execute_command(
            {
                "alias": "probe",
                "tool": "echo",
                "arguments": {"text": "hello"},
                "catalog_revision": catalog.revision,
            },
            catalog=catalog,
        )
    )

    assert outcome.execution_state == "not_started"
    assert outcome.result is _result.__defaults__ or outcome.result is not None
    assert outcome.result.structured_content == {"native": True}
    assert outcome.result.is_error is False
    assert provider.calls == [("echo", {"text": "hello"})]


def test_provider_failure_maps_to_execution_failed_without_replay() -> None:
    provider = _RouteProvider(error=RuntimeError("mcp exploded"))
    catalog = _seeded_catalog(provider)

    with pytest.raises(CommandError) as error:
        asyncio.run(
            execute_command(
                {
                    "alias": "probe",
                    "tool": "echo",
                    "arguments": {"text": "hello"},
                    "catalog_revision": catalog.revision,
                },
                catalog=catalog,
            )
        )

    assert error.value.code == "execution_failed"
    assert error.value.execution_state == "unknown"
    assert len(provider.calls) == 1  # exactly one send, no replay


def test_invalidation_between_reservation_and_dispatch_cancels_the_call() -> None:
    provider = _RouteProvider(result=_result())
    catalog = _seeded_catalog(provider)

    async def scenario() -> None:
        from mcp_relay.mcp_catalog import AliasCatalog

        revision = catalog.revision

        async def invalidate_midway() -> None:
            # Simulate a route replacement landing after the reservation was
            # validated but before the dispatch ran.
            catalog.update_alias(
                AliasCatalog(
                    alias="probe",
                    enabled=True,
                    runtime_state="running",
                    transport="stdio",
                    entry={"command": ["/bin/probe-v2"]},
                    last_error=None,
                    catalog_available=True,
                    discovery_error=None,
                    descriptors=(_descriptor(),),
                    provider=_RouteProvider(result=_result()),
                )
            )

        with pytest.raises(CommandError) as error:
            await execute_command(
                {
                    "alias": "probe",
                    "tool": "echo",
                    "arguments": {},
                    "catalog_revision": revision,
                },
                catalog=catalog,
                on_reservation_check=invalidate_midway,
            )

        assert error.value.code == "catalog_stale"
        assert error.value.execution_state == "not_started"
        assert provider.calls == []  # reservation cancelled, nothing sent

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Errors carry the closed code/message/execution_state shape
# ---------------------------------------------------------------------------


def test_command_error_payload_is_safe_and_closed() -> None:
    error = CommandError("alias_unknown", "no such alias")
    payload = error.to_payload()
    assert set(payload) == {"code", "message", "execution_state"}
    assert payload["code"] == "alias_unknown"
    assert payload["execution_state"] == "not_started"
    assert len(payload["message"]) <= 512


def test_catalog_errors_are_reraised_as_command_errors() -> None:
    with pytest.raises(CommandError) as error:
        asyncio.run(
            execute_command(
                {
                    "alias": "x",
                    "tool": "y",
                    "arguments": {},
                    "catalog_revision": "missing",
                },
                catalog=ClientCatalog(),
            )
        )
    assert error.value.code == "catalog_stale"


def test_mcp_iserror_results_are_relayed_intact_not_mapped_to_relay_codes() -> None:
    provider = _RouteProvider(
        result=ProviderToolResult.model_validate(
            {
                "content": [{"type": "text", "text": "tool says no"}],
                "isError": True,
            }
        )
    )
    catalog = _seeded_catalog(provider)

    outcome = asyncio.run(
        execute_command(
            {
                "alias": "probe",
                "tool": "echo",
                "arguments": {},
                "catalog_revision": catalog.revision,
            },
            catalog=catalog,
        )
    )

    assert outcome.execution_state == "not_started"
    assert outcome.result.is_error is True
    assert outcome.result.content[0].text == "tool says no"


def test_reserved_target_words_never_bypass_the_catalog() -> None:
    provider = _RouteProvider(result=_result())
    catalog = _seeded_catalog(provider)
    for alias in ("client", "mcp", "server"):
        with pytest.raises(CommandError) as error:
            _run(
                {
                    "alias": alias,
                    "tool": "status",
                    "arguments": {},
                    "catalog_revision": catalog.revision,
                },
                catalog=catalog,
            )
        # The closed envelope refuses reserved words outright — they are not
        # third-party targets and can never reach a reservation.
        assert error.value.code == "invalid_arguments"
    assert provider.calls == []


def test_catalog_error_codes_flow_through_command_errors() -> None:
    error = CommandError.from_catalog_error(
        CatalogError("tool_unknown", "no such tool")
    )
    assert error.code == "tool_unknown"
    assert error.execution_state == "not_started"


# ---------------------------------------------------------------------------
# Step 8: typed provider refusals — no message-fragment recognition
# ---------------------------------------------------------------------------


def test_stale_inventory_refuses_as_alias_unavailable_not_started() -> None:
    """A stale inventory refusal is an unavailable alias, not bad arguments."""
    provider = _RouteProvider(
        error=ProviderStaleInventoryError("provider tool inventory is stale")
    )
    catalog = _seeded_catalog(provider)

    with pytest.raises(CommandError) as error:
        _run(
            {
                "alias": "probe",
                "tool": "echo",
                "arguments": {},
                "catalog_revision": catalog.revision,
            },
            catalog=catalog,
        )

    assert error.value.code == "alias_unavailable"
    assert error.value.execution_state == "not_started"


def test_unavailable_provider_refuses_as_alias_unavailable_not_started() -> None:
    """An unavailable provider client is an alias refusal, not tool_unknown."""
    provider = _RouteProvider(
        error=ProviderUnavailableError("provider client unavailable")
    )
    catalog = _seeded_catalog(provider)

    with pytest.raises(CommandError) as error:
        _run(
            {
                "alias": "probe",
                "tool": "echo",
                "arguments": {},
                "catalog_revision": catalog.revision,
            },
            catalog=catalog,
        )

    assert error.value.code == "alias_unavailable"
    assert error.value.execution_state == "not_started"


class _CongestedClockTransport:
    """Fake MCP transport for the congestion scenario: the pre-send reread
    succeeds, but the caller's clock is past the deadline at resumption."""

    def __init__(self) -> None:
        self.call_count = 0
        self.clock: dict[str, float] = {"t": 100.0}

    async def list_tools(self, cursor: str | None = None) -> dict[str, object]:
        tool: dict[str, object] = {
            "name": "echo",
            "description": "Echo back",
            "inputSchema": {"type": "object", "properties": {}},
        }
        response: dict[str, object] = {"tools": [tool]}
        # Task-done scheduled before the wait timeout, yet the clock already
        # passed the deadline: pure loop congestion, no send either way.
        self.clock["t"] += 2.0
        return response

    async def call_tool(
        self, name: str, arguments: Mapping[str, JsonValue]
    ) -> dict[str, object]:
        self.call_count += 1
        return {"content": [{"type": "text", "text": "no"}], "isError": False}

    async def close(self) -> None:
        return None


class _CongestedMcpRouteProvider:
    """Route through the real client: reread OK, budget spent at resumption."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.transport = _CongestedClockTransport()
        self._client = McpProviderToolClient(
            self.transport, provider_name="probe", timeout_seconds=1
        )

    async def call_tool(
        self, tool_name: str, arguments: dict[str, object]
    ) -> ProviderToolResult:
        self.calls.append((tool_name, dict(arguments)))
        loop = asyncio.get_running_loop()
        real_time = loop.time

        def congested_time() -> float:
            return self.transport.clock["t"]

        loop.time = congested_time  # type: ignore[method-assign]
        try:
            return await self._client.call_tool(tool_name, arguments)
        finally:
            loop.time = real_time  # type: ignore[method-assign]


def test_congested_budget_exhaustion_maps_alias_unavailable_not_started() -> None:
    """A spent pre-send budget (reread OK) is an unavailable refusal, not a
    post-send timeout: the MCP send provably never happened."""
    provider = _CongestedMcpRouteProvider()
    catalog = _seeded_catalog(provider)

    with pytest.raises(CommandError) as error:
        _run(
            {
                "alias": "probe",
                "tool": "echo",
                "arguments": {},
                "catalog_revision": catalog.revision,
            },
            catalog=catalog,
        )

    assert error.value.code == "alias_unavailable"
    assert error.value.execution_state == "not_started"
    assert provider.transport.call_count == 0


def test_unknown_provider_tool_stays_tool_unknown_not_started() -> None:
    """A truly absent tool is tool_unknown, refused before any send."""
    provider = _RouteProvider(
        error=UnknownProviderToolError("unknown provider tool")
    )
    catalog = _seeded_catalog(provider)

    with pytest.raises(CommandError) as error:
        _run(
            {
                "alias": "probe",
                "tool": "zap",
                "arguments": {},
                "catalog_revision": catalog.revision,
            },
            catalog=catalog,
        )

    assert error.value.code == "tool_unknown"
    assert error.value.execution_state == "not_started"


def test_post_send_failure_is_never_reclassified_by_error_text() -> None:
    """A post-send failure whose detail mentions 'stale' stays unknown.

    The send happened; text fragments in provider detail must never demote
    the honest ``unknown`` state or rename the closed code.
    """
    provider = _RouteProvider(
        error=ProviderToolError(
            "provider tool call failed (RuntimeError: stale cache left over)"
        )
    )
    catalog = _seeded_catalog(provider)

    with pytest.raises(CommandError) as error:
        _run(
            {
                "alias": "probe",
                "tool": "echo",
                "arguments": {},
                "catalog_revision": catalog.revision,
            },
            catalog=catalog,
        )

    assert error.value.code == "execution_failed"
    assert error.value.execution_state == "unknown"
    assert len(provider.calls) == 1  # exactly one send, no replay


def test_provider_deadline_maps_to_timeout_unknown() -> None:
    """A provider deadline after dispatch is the closed timeout code."""
    provider = _RouteProvider(
        error=ProviderTimeoutError("provider operation timed out")
    )
    catalog = _seeded_catalog(provider)

    with pytest.raises(CommandError) as error:
        _run(
            {
                "alias": "probe",
                "tool": "echo",
                "arguments": {},
                "catalog_revision": catalog.revision,
            },
            catalog=catalog,
        )

    assert error.value.code == "timeout"
    assert error.value.execution_state == "unknown"
