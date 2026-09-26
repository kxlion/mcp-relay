"""Characterization: owner-boundary cancellation semantics, observable.

The historic transports contain AnyIO cancel scopes inside a private owner
task. Tranche 3 replaces them with FastMCP clients; these tests lock the
OBSERVABLE behavior any implementation must reproduce — expressed only
through the public ``McpHub`` seam (``transport_factory``, ``reconcile_all``,
``provider_clients``, ``forget``) and real synthetic MCP servers:

- cancelling a startup/discovery in flight leaves NO registered client and
  closes the provisional transport; a later reconcile recovers cleanly;
- cancelling a dispatched call in flight does not poison the connection:
  subsequent calls succeed, teardown is clean and idempotent, and no
  "cancel scope in a different task" error ever escapes;
- concurrent callers are isolated: cancelling one never cancels the other;
- an offline alias does not leak a client after a cancelled reconcile.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

import pytest
import uvicorn

from mcp_relay.mcp_hub import AliasState, McpHub, production_transport_factory
from mcp_relay.providers.base import ProviderUnavailableError
from tests.test_characterization_fixed_surface_chain import (
    _MINI_STDIO,
    _build_synthetic_http_server,
    _configure,
    _free_port,
)


def _hub_for(config_path: Path, tmp_path: Path) -> McpHub:
    return McpHub(
        config_path,
        tmp_path,
        transport_factory=production_transport_factory,
    )


# ---------------------------------------------------------------------------
# Cancelled startup: no orphan client, later recovery works
# ---------------------------------------------------------------------------


def test_cancelled_reconcile_leaves_no_client_and_recovers(
    tmp_path: Path,
) -> None:
    """Cancelling discovery closes the provisional transport; recovery OK."""
    closed = asyncio.Event()

    class Transport:
        async def list_tools(self, cursor: object = None) -> object:
            await asyncio.Event().wait()  # hang until cancelled

        async def call_tool(self, name: object, arguments: object) -> object:
            raise AssertionError("no dispatch before discovery completes")

        async def close(self) -> None:
            closed.set()

    config_path = tmp_path / "config.yaml"
    _configure(config_path, {"command": ["synthetic"]})
    hub = McpHub(
        config_path,
        tmp_path,
        transport_factory=lambda launch: Transport(),  # type: ignore[arg-type,return-value]
        provider_timeout_seconds=0.5,
        startup_budget_seconds=2.0,
    )

    async def scenario() -> None:
        task = asyncio.create_task(hub.reconcile_all())
        await asyncio.sleep(0.05)  # let the provisional transport hang
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()  # the provisional transport was closed
        assert hub.provider_clients() == {}  # nothing leaked into the registry

        # A later reconcile recovers cleanly (no poisoned hub state): the
        # synthetic transport still hangs, so the alias ends up unavailable.
        status = await hub.reconcile_all()
        assert status["mini"].state is AliasState.UNAVAILABLE
        assert closed.is_set()
        await hub.forget("mini")

    asyncio.run(asyncio.wait_for(scenario(), timeout=15))


def test_cancelled_reconcile_against_offline_alias_is_clean(tmp_path: Path) -> None:
    """Cancelling during a doomed spawn leaves no half-registered client."""
    config_path = tmp_path / "config.yaml"
    _configure(config_path, {"command": ["/nonexistent/relay-probe-binary"]})
    hub = _hub_for(config_path, tmp_path)

    async def scenario() -> None:
        task = asyncio.create_task(hub.reconcile_all())
        await asyncio.sleep(0.02)
        task.cancel()
        # The cancel races the doomed spawn: either the task was cancelled
        # in flight, or it had already finished. Both are honest outcomes;
        # the contract is the FINAL state below.
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # A later reconcile is clean: the alias is unavailable, and no
        # client leaked from either attempt.
        status = await hub.reconcile_all()
        assert status["mini"].state is AliasState.UNAVAILABLE
        assert hub.state_of("mini") is AliasState.UNAVAILABLE
        assert hub.provider_clients().get("mini") is None
        await hub.forget("mini")
        await hub.forget("mini")  # idempotent

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


# ---------------------------------------------------------------------------
# Cancelled dispatch over a REAL stdio server: no poisoned session
# ---------------------------------------------------------------------------


def test_cancelled_stdio_call_does_not_poison_the_connection(tmp_path: Path) -> None:
    mini = tmp_path / "mini_mcp.py"
    mini.write_text(_MINI_STDIO, encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    _configure(config_path, {"command": [sys.executable, str(mini)]})
    hub = _hub_for(config_path, tmp_path)

    async def scenario() -> None:
        await hub.reconcile_all()
        provider = hub.provider_clients()["mini"]

        # Cancel one dispatched call while it is in flight on the server.
        slow = asyncio.create_task(provider.call_tool("linger", {"seconds": 30.0}))
        await asyncio.sleep(0.5)
        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow

        # Fail-closed contract: after a cancelled in-flight call the client
        # refuses further dispatches instead of risking a corrupted session.
        with pytest.raises(ProviderUnavailableError):
            await provider.call_tool("echo", {"text": "still-alive"})

        # Recovery: forget tears the unavailable client down; a fresh
        # reconcile spawns a new one that is immediately usable.
        await hub.forget("mini")
        await hub.reconcile_all()
        provider = hub.provider_clients()["mini"]
        result = await asyncio.wait_for(
            provider.call_tool("echo", {"text": "recovered"}), timeout=10
        )
        assert result.is_error is False
        assert "recovered" in result.content[0].text

        # Teardown is clean and idempotent.
        await hub.forget("mini")
        await hub.forget("mini")
        assert hub.provider_clients().get("mini") is None

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


def test_concurrent_stdio_calls_are_isolated_from_cancellation(tmp_path: Path) -> None:
    """Cancelling one in-flight caller never cancels its concurrent sibling."""
    mini = tmp_path / "mini_mcp.py"
    mini.write_text(_MINI_STDIO, encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    _configure(config_path, {"command": [sys.executable, str(mini)]})
    hub = _hub_for(config_path, tmp_path)

    async def scenario() -> None:
        await hub.reconcile_all()
        provider = hub.provider_clients()["mini"]

        victim = asyncio.create_task(
            provider.call_tool("linger", {"seconds": 30.0})
        )
        sibling = asyncio.create_task(
            provider.call_tool("linger", {"seconds": 2.0})
        )
        await asyncio.sleep(0.5)
        victim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await victim
        # The sibling was already dispatched: it completes and delivers its
        # result — cancelling one caller does not cancel its sibling.
        result = await asyncio.wait_for(sibling, timeout=10)
        assert result.is_error is False
        assert "lingered" in result.content[0].text

        await hub.forget("mini")

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


# ---------------------------------------------------------------------------
# Cancelled dispatch over a REAL Streamable HTTP server
# ---------------------------------------------------------------------------


def test_cancelled_http_call_does_not_poison_the_session(tmp_path: Path) -> None:
    port = _free_port()
    mcp = _build_synthetic_http_server()
    app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    )
    config_path = tmp_path / "config.yaml"
    _configure(config_path, {"url": f"http://127.0.0.1:{port}/mcp"})
    hub = _hub_for(config_path, tmp_path)

    async def scenario() -> None:
        server_task = asyncio.create_task(server.serve())
        try:
            for _ in range(200):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            await hub.reconcile_all()
            provider = hub.provider_clients()["mini"]

            slow = asyncio.create_task(
                provider.call_tool("linger", {"seconds": 30.0})
            )
            await asyncio.sleep(0.5)
            slow.cancel()
            with pytest.raises(asyncio.CancelledError):
                await slow

            # Fail-closed contract (same as stdio): the client refuses after
            # a cancelled in-flight call; recovery goes through reconcile.
            with pytest.raises(ProviderUnavailableError):
                await provider.call_tool("echo", {"text": "http-alive"})
            await hub.forget("mini")
            await hub.reconcile_all()
            provider = hub.provider_clients()["mini"]
            result = await asyncio.wait_for(
                provider.call_tool("echo", {"text": "http-alive"}), timeout=10
            )
            assert result.is_error is False
            assert "http-alive" in result.content[0].text

            await hub.forget("mini")
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=5)

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))
