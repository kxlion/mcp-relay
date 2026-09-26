"""Characterization: command cut semantics and the no-replay contract.

These tests lock the OBSERVABLE product semantics of ``relay_mcp_command``
execution (``mcp_command.execute_command``) without naming any transport
class. They must survive the Tranche 3 replacement of the historic
transports by FastMCP clients:

- every refusal BEFORE the MCP send carries ``execution_state="not_started"``
  and provably never touches the provider (zero ``call_tool`` invocations);
- every failure AFTER the send carries ``execution_state="unknown"`` and the
  provider was invoked EXACTLY ONCE — the relay never replays an action
  whose result is uncertain;
- MCP-native ``isError`` results are relayed intact, never mapped to Relay
  error codes, and never interpreted;
- unknown third-party top-level fields survive the round trip untouched;
- an oversized result is refused with the honest ``result_too_large`` code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog
from mcp_relay.mcp_command import CommandError, execute_command
from mcp_relay.output_models import ProviderToolResult
from mcp_relay.provider_tools import ProviderToolDescriptor
from mcp_relay.providers.base import (
    ProviderResultTooLargeError,
    ProviderTimeoutError,
    ProviderToolError,
)


class _RecordingProvider:
    """Route stand-in that records every invocation (replay detector)."""

    def __init__(
        self,
        *,
        result: ProviderToolResult | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.cancelled = asyncio.Event()
        self._result = result
        self._error = error
        self._delay = delay

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, object]
    ) -> ProviderToolResult:
        self.calls.append((tool_name, dict(arguments)))
        if self._delay:
            await asyncio.sleep(self._delay)
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


def _seeded_catalog(
    provider: object, *, runtime_state: str = "running"
) -> ClientCatalog:
    catalog = ClientCatalog()
    catalog.update_alias(
        AliasCatalog(
            alias="probe",
            enabled=True,
            runtime_state=runtime_state,
            transport="stdio",
            entry={"command": ["/bin/probe"]},
            last_error=None,
            catalog_available=True,
            discovery_error=None,
            descriptors=(_descriptor(), _descriptor("fail")),
            provider=provider,
        )
    )
    return catalog


def _text_result(payload: dict[str, Any] | None = None) -> ProviderToolResult:
    body: dict[str, Any] = {
        "content": [{"type": "text", "text": "hello"}],
        "isError": False,
    }
    if payload:
        body.update(payload)
    return ProviderToolResult.model_validate(body)


def _run(arguments: object, catalog: ClientCatalog, **kwargs: object) -> object:
    return asyncio.run(execute_command(arguments, catalog=catalog, **kwargs))  # type: ignore[arg-type]


def _command_arguments(
    *, revision: str | None = None, tool: str = "echo", alias: str = "probe"
) -> dict[str, Any]:
    return {
        "alias": alias,
        "tool": tool,
        "arguments": {"text": "hello"},
        "catalog_revision": revision if revision is not None else "unused",
    }


# ---------------------------------------------------------------------------
# Refusals strictly before dispatch: not_started, provider untouched
# ---------------------------------------------------------------------------


def test_unknown_tool_is_refused_before_dispatch() -> None:
    provider = _RecordingProvider(result=_text_result())
    catalog = _seeded_catalog(provider)
    arguments = _command_arguments(revision=catalog.revision, tool="zap")

    with pytest.raises(CommandError) as excinfo:
        _run(arguments, catalog)

    assert excinfo.value.code == "tool_unknown"
    assert excinfo.value.execution_state == "not_started"
    assert provider.calls == []  # the MCP target was never contacted


def test_unknown_alias_is_refused_before_dispatch() -> None:
    provider = _RecordingProvider(result=_text_result())
    catalog = _seeded_catalog(provider)
    arguments = _command_arguments(revision=catalog.revision, alias="ghost")

    with pytest.raises(CommandError) as excinfo:
        _run(arguments, catalog)

    assert excinfo.value.code == "alias_unknown"
    assert excinfo.value.execution_state == "not_started"
    assert provider.calls == []


def test_offline_alias_is_refused_before_dispatch() -> None:
    """An offline alias (no executable route) refuses with zero sends."""
    provider = _RecordingProvider(result=_text_result())
    catalog = ClientCatalog()
    catalog.update_alias(
        AliasCatalog(
            alias="probe",
            enabled=True,
            runtime_state="unavailable",
            transport="stdio",
            entry={"command": ["/bin/probe"]},
            last_error={"code": "alias_unavailable", "message": "spawn failed"},
            catalog_available=False,
            discovery_error={"code": "alias_unavailable", "message": "offline"},
            provider=None,
        )
    )
    arguments = _command_arguments(revision=catalog.revision)

    with pytest.raises(CommandError) as excinfo:
        _run(arguments, catalog)

    assert excinfo.value.code == "alias_unavailable"
    assert excinfo.value.execution_state == "not_started"
    assert provider.calls == []  # the MCP target was never contacted


def test_stale_catalog_revision_is_refused_before_dispatch() -> None:
    provider = _RecordingProvider(result=_text_result())
    catalog = _seeded_catalog(provider)
    stale_revision = catalog.revision
    # The catalog moved after the caller took its revision.
    catalog.remove_alias("probe")
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
    assert catalog.revision != stale_revision
    arguments = _command_arguments(revision=stale_revision)

    with pytest.raises(CommandError) as excinfo:
        _run(arguments, catalog)

    assert excinfo.value.code == "catalog_stale"
    assert excinfo.value.execution_state == "not_started"
    assert provider.calls == []


def test_catalog_invalidated_between_reservation_and_send_is_refused() -> None:
    """The reservation check cancels the route before dispatch: no redirect."""
    provider = _RecordingProvider(result=_text_result())
    catalog = _seeded_catalog(provider)
    arguments = _command_arguments(revision=catalog.revision)

    async def invalidate() -> None:
        catalog.update_alias(
            AliasCatalog(
                alias="probe",
                enabled=True,
                runtime_state="running",
                transport="stdio",
                entry={"command": ["/bin/probe2"]},
                last_error=None,
                catalog_available=True,
                discovery_error=None,
                descriptors=(_descriptor(),),
                provider=provider,
            )
        )

    with pytest.raises(CommandError) as excinfo:
        _run(arguments, catalog, on_reservation_check=invalidate)

    assert excinfo.value.code == "catalog_stale"
    assert excinfo.value.execution_state == "not_started"
    assert provider.calls == []  # cancelled, never redirected to the new route


# ---------------------------------------------------------------------------
# Failures after dispatch: unknown, exactly one send, never replayed
# ---------------------------------------------------------------------------


def test_timeout_after_dispatch_is_unknown_and_never_replayed() -> None:
    provider = _RecordingProvider(error=ProviderTimeoutError("late"), delay=0.5)
    catalog = _seeded_catalog(provider)
    arguments = _command_arguments(revision=catalog.revision)

    with pytest.raises(CommandError) as excinfo:
        _run(arguments, catalog)

    assert excinfo.value.code == "timeout"
    assert excinfo.value.execution_state == "unknown"
    assert len(provider.calls) == 1  # sent once; no automatic re-emission


def test_post_dispatch_provider_failure_is_unknown_and_never_replayed() -> None:
    provider = _RecordingProvider(error=ProviderToolError("transport died"))
    catalog = _seeded_catalog(provider)
    arguments = _command_arguments(revision=catalog.revision)

    with pytest.raises(CommandError) as excinfo:
        _run(arguments, catalog)

    assert excinfo.value.code == "execution_failed"
    assert excinfo.value.execution_state == "unknown"
    assert len(provider.calls) == 1


def test_oversized_result_after_dispatch_is_result_too_large_and_unknown() -> None:
    provider = _RecordingProvider(
        error=ProviderResultTooLargeError(
            "RELAY_MAX_RESULT_BYTES: 2097152 < payload: 2097153 bytes"
        )
    )
    catalog = _seeded_catalog(provider)
    arguments = _command_arguments(revision=catalog.revision)

    with pytest.raises(CommandError) as excinfo:
        _run(arguments, catalog)

    assert excinfo.value.code == "result_too_large"
    assert excinfo.value.execution_state == "unknown"
    assert "RELAY_MAX_RESULT_BYTES" in excinfo.value.message
    assert len(provider.calls) == 1


def test_cancellation_of_a_dispatched_call_propagates() -> None:
    """Cancelling a waiting caller cancels the call; no silent swallow."""
    provider = _RecordingProvider(result=_text_result(), delay=5.0)
    catalog = _seeded_catalog(provider)
    arguments = _command_arguments(revision=catalog.revision)

    async def scenario() -> None:
        task = asyncio.create_task(execute_command(arguments, catalog=catalog))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=3))
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# Success: the native result is returned untouched
# ---------------------------------------------------------------------------


def test_success_returns_the_native_result_untouched() -> None:
    native = _text_result(
        {
            "structuredContent": {"echo": "hello"},
            "resultType": "complete",
            "futureField": {"a": [1, 2, 3]},
        }
    )
    provider = _RecordingProvider(result=native)
    catalog = _seeded_catalog(provider)
    arguments = _command_arguments(revision=catalog.revision)

    outcome = _run(arguments, catalog)

    assert provider.calls == [("echo", {"text": "hello"})]
    dumped = outcome.result.model_dump(mode="json", by_alias=True, exclude_none=True)  # type: ignore[attr-defined]
    # Unknown third-party fields survive verbatim; native isError stays.
    assert dumped["resultType"] == "complete"
    assert dumped["futureField"] == {"a": [1, 2, 3]}
    assert dumped["structuredContent"] == {"echo": "hello"}
    assert dumped["isError"] is False
    # The declared execution state is not an idempotency guarantee.
    assert outcome.execution_state == "not_started"  # type: ignore[attr-defined]


def test_native_mcp_error_result_is_relayed_intact() -> None:
    """An MCP isError result is a RESULT, not a Relay error code."""
    native = ProviderToolResult.model_validate(
        {
            "content": [{"type": "text", "text": "tool says no"}],
            "isError": True,
        }
    )
    provider = _RecordingProvider(result=native)
    catalog = _seeded_catalog(provider)
    arguments = _command_arguments(revision=catalog.revision, tool="fail")

    outcome = _run(arguments, catalog)

    # No CommandError raised: the native error result passes through.
    assert outcome.result.is_error is True  # type: ignore[attr-defined]
    assert outcome.result.content[0].text == "tool says no"  # type: ignore[attr-defined]
    assert outcome.execution_state == "not_started"  # type: ignore[attr-defined]
