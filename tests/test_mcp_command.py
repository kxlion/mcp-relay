"""Third-party execution: closed envelope, pre-send refusals, one send, native result.

Every refusal before the MCP send is ``not_started`` and never touches the
provider; every failure after it is ``unknown``; nothing is replayed.
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog
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


class _Provider:
    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._result = result
        self._error = error

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        self.calls.append((name, dict(arguments)))
        if self._error is not None:
            raise self._error
        return self._result


def _result(*, is_error: bool = False) -> ProviderToolResult:
    return ProviderToolResult.model_validate(
        {
            "content": [{"type": "text", "text": "hello"}],
            "structuredContent": {"native": True},
            "isError": is_error,
        }
    )


def _catalog(provider: object, *, available: bool = True, tool_filter=None) -> ClientCatalog:
    catalog = ClientCatalog()
    catalog.update_alias(
        AliasCatalog(
            alias="probe",
            enabled=True,
            runtime_state="running",
            transport="stdio",
            catalog_available=available,
            error=None,
            descriptors=(
                ProviderToolDescriptor(
                    provider_name="probe",
                    tool_name="echo",
                    description="Echo back",
                    input_schema={"type": "object"},
                ),
            ),
            provider=provider if available else None,
            tool_filter=tool_filter,
        )
    )
    return catalog


def _run(arguments: object, catalog: ClientCatalog) -> ProviderToolResult:
    return asyncio.run(execute_command(arguments, catalog=catalog))


def _envelope(**overrides: object) -> dict[str, object]:
    return {"alias": "probe", "tool": "echo", "arguments": {"text": "hi"}, **overrides}


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"alias": "probe", "tool": "echo"},
        {**_envelope(), "catalog_revision": "r"},
        _envelope(arguments="not-an-object"),
        _envelope(alias=""),
        _envelope(tool=7),
    ],
)
def test_envelope_is_closed(arguments: object) -> None:
    provider = _Provider(_result())
    with pytest.raises(CommandError) as error:
        _run(arguments, _catalog(provider))
    assert (error.value.code, error.value.execution_state) == (
        "invalid_arguments",
        "not_started",
    )
    assert provider.calls == []


@pytest.mark.parametrize(
    ("envelope", "catalog_kwargs", "code"),
    [
        (_envelope(alias="other"), {}, "alias_unknown"),
        (_envelope(tool="zap"), {}, "tool_unknown"),
        (_envelope(), {"available": False}, "alias_unavailable"),
        (_envelope(), {"tool_filter": {"other": None}}, "tool_unknown"),
    ],
)
def test_catalog_refusals_never_reach_the_provider(
    envelope: dict[str, object], catalog_kwargs: dict[str, object], code: str
) -> None:
    provider = _Provider(_result())
    with pytest.raises(CommandError) as error:
        _run(envelope, _catalog(provider, **catalog_kwargs))
    assert (error.value.code, error.value.execution_state) == (code, "not_started")
    assert provider.calls == []


def test_one_send_with_the_exact_tool_and_native_result() -> None:
    provider = _Provider(_result())
    result = _run(_envelope(), _catalog(provider))
    assert provider.calls == [("echo", {"text": "hi"})]
    assert result.structured_content == {"native": True}


def test_mcp_is_error_results_are_relayed_intact() -> None:
    provider = _Provider(_result(is_error=True))
    result = _run(_envelope(), _catalog(provider))
    assert result.is_error is True
    assert result.content[0].text == "hello"


@pytest.mark.parametrize(
    ("error", "code", "state"),
    [
        (ProviderStaleInventoryError("stale"), "alias_unavailable", "not_started"),
        (ProviderUnavailableError("down"), "alias_unavailable", "not_started"),
        (UnknownProviderToolError("gone"), "tool_unknown", "not_started"),
        (ProviderTimeoutError("slow"), "timeout", "unknown"),
        (asyncio.TimeoutError(), "timeout", "unknown"),
        (ProviderResultTooLargeError("RELAY_MAX_X: 1 < payload: 2 bytes"), "result_too_large", "unknown"),
        # A post-send failure is never reclassified by its text.
        (ProviderToolError("unknown provider tool"), "execution_failed", "unknown"),
        (RuntimeError("boom"), "execution_failed", "unknown"),
    ],
)
def test_provider_failures_map_to_closed_codes_without_replay(
    error: Exception, code: str, state: str
) -> None:
    provider = _Provider(error=error)
    with pytest.raises(CommandError) as raised:
        _run(_envelope(), _catalog(provider))
    assert (raised.value.code, raised.value.execution_state) == (code, state)
    assert len(provider.calls) == 1


def test_result_too_large_names_the_tool_and_the_bound() -> None:
    provider = _Provider(error=ProviderResultTooLargeError("RELAY_MAX_X: 1 < payload: 2 bytes"))
    with pytest.raises(CommandError) as raised:
        _run(_envelope(), _catalog(provider))
    assert "echo" in raised.value.message
    assert "RELAY_MAX_X" in raised.value.message
