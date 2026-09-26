"""The admin CRUD tools answer a native CallToolResult (e2e bug fix).

The e2e bench proved that ``relay_mcp_add/modify/delete/enable/disable``
declared ``structured_output=True`` and returning the client's raw
``ProviderToolResult`` payload always failed the SDK's output validation
(the payload is not a plain dict: it wraps content lists), so every admin
mutation answered ``isError=true`` even when committed. The contract is
now the same as ``relay_mcp_command``: ``structured_output=False`` and the
native ``convert_result`` rendering, whose ``structuredContent`` carries
the capability's closed dict.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import Client
from mcp import ClientSession  # noqa: F401  (SDK idiom parity with test_mcp_facade)

from mcp_relay.mcp_facade import create_mcp_facade
from mcp_relay.output_models import ProviderToolResult
from mcp_relay.registry import (
    ClientOfflineError,
    RelayError,
    RemoteClientError,
)


class _StubRegistry:
    """Registry stub returning a ProviderToolResult like the real one."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def invoke(
        self, client_id: str | None, message: object, timeout_seconds: float
    ) -> object:
        # Mirrors the real registry: the bounded wire mirror becomes the
        # native MCP result exactly once (Tranche 4).
        from mcp_relay.mcp_results import native_result

        return native_result(ProviderToolResult.model_validate(self._payload))

    def status_snapshot(self) -> object:  # pragma: no cover - unused here
        raise RelayError("unused")

    def set_public_tools_count(self, count: int) -> None:  # noqa: ARG002
        """Track the facade surface like the real registry."""

    def set_progress_listener(self, listener: object) -> None:  # noqa: ARG002
        """Progress wiring no-op for the duck-typed test registry."""


_ADD_RESULT: dict[str, Any] = {
    "content": [{"type": "text", "text": "ok"}],
    "structuredContent": {"alias": "mini", "status": "running"},
    "isError": False,
}


def _admin_registry() -> _StubRegistry:
    return _StubRegistry(_ADD_RESULT)


def test_admin_tools_answer_native_results_without_output_validation_error() -> None:
    """mcp.add through the real facade pipeline must not raise ValidationError."""

    async def scenario() -> tuple[str, dict[str, Any], bool]:
        registry = _admin_registry()
        mcp = create_mcp_facade(
            registry=registry,  # type: ignore[arg-type]
            client_id="one",
            timeout_seconds=1,
        )
        async with Client(mcp) as session:
            result = await session.call_tool(
                "relay_mcp_add",
                {"alias": "mini", "entry": {"command": ["/bin/mini"]}},
            )
            assert result.structured_content is not None
            return (
                result.structured_content["alias"],
                dict(result.structured_content),
                result.is_error,
            )

    alias, structured, is_error = asyncio.run(scenario())

    assert alias == "mini"
    assert structured == {"alias": "mini", "status": "running"}
    assert is_error is False


def test_admin_tools_have_no_output_schema() -> None:
    """structured_output=False: no outputSchema is published for the CRUD."""

    async def scenario() -> dict[str, object]:
        registry = _admin_registry()
        mcp = create_mcp_facade(
            registry=registry,  # type: ignore[arg-type]
            client_id="one",
            timeout_seconds=1,
        )
        async with Client(mcp) as session:
            tools = await session.list_tools()
        return {tool.name: tool.output_schema for tool in tools}

    schemas = asyncio.run(scenario())

    for name in (
        "relay_mcp_add",
        "relay_mcp_modify",
        "relay_mcp_delete",
        "relay_mcp_enable",
        "relay_mcp_disable",
    ):
        assert schemas[name] is None, name


def test_admin_tool_relay_failure_is_an_is_error_native_result() -> None:
    """Dispatch failures render the closed error object, not an exception."""

    class _FailingRegistry:
        def set_public_tools_count(self, count: int) -> None:  # noqa: ARG002
            pass

        def set_progress_listener(self, listener: object) -> None:  # noqa: ARG002
            pass

        async def invoke(
            self, client_id: str | None, message: object, timeout_seconds: float
        ) -> ProviderToolResult:
            raise ClientOfflineError("client is offline")

        def status_snapshot(self) -> object:  # pragma: no cover
            raise RelayError("unused")

    async def scenario() -> tuple[bool, str]:
        mcp = create_mcp_facade(
            registry=_FailingRegistry(),  # type: ignore[arg-type]
            client_id="one",
            timeout_seconds=1,
        )
        async with Client(mcp) as session:
            result = await session.call_tool(
                "relay_mcp_delete", {"alias": "mini"}, raise_on_error=False
            )
            text = result.content[0].text
            return result.is_error, text

    is_error, text = asyncio.run(scenario())

    assert is_error is True
    assert '"code":"client_unavailable"' in text


def test_admin_tool_client_refusal_is_an_is_error_native_result() -> None:
    """A permission_denied refusal surfaces as isError with the closed code."""

    class _RefusingRegistry:
        def set_public_tools_count(self, count: int) -> None:  # noqa: ARG002
            pass

        def set_progress_listener(self, listener: object) -> None:  # noqa: ARG002
            pass

        async def invoke(
            self, client_id: str | None, message: object, timeout_seconds: float
        ) -> ProviderToolResult:
            # Mirror the real pipeline: the Client's permission_denied refusal
            # arrives as a ClientError frame, which the registry turns into
            # RemoteClientError before the facade maps it.
            raise RemoteClientError(
                "permission_denied",
                "administration is disabled on this client",
                execution_state="not_started",
            )

        def status_snapshot(self) -> object:  # pragma: no cover
            raise RelayError("unused")

    async def scenario() -> tuple[bool, str]:
        mcp = create_mcp_facade(
            registry=_RefusingRegistry(),  # type: ignore[arg-type]
            client_id="one",
            timeout_seconds=1,
        )
        async with Client(mcp) as session:
            result = await session.call_tool(
                "relay_mcp_enable", {"alias": "mini"}, raise_on_error=False
            )
            return result.is_error, result.content[0].text

    is_error, text = asyncio.run(scenario())

    assert is_error is True
    assert "permission_denied" in text
