"""Hub diagnostics identify technical causes; secret-scan filters are retired."""
import asyncio

from mcp_relay.config import mcp_entry_add
from mcp_relay.mcp_hub import AliasState, McpHub


def test_spawn_logs_real_provider_cause_chain_and_cleanup_alias(tmp_path, monkeypatch):
    logs = []
    monkeypatch.setattr("mcp_relay.mcp_hub._debug_log", logs.append)

    class Transport:
        async def list_tools(self, cursor=None):
            raise ExceptionGroup("synthetic group", [
                FileNotFoundError("/opt/bin/synthetic-driver"),
                PermissionError("driver refused"),
            ])

        async def close(self):
            raise OSError("close-token=synthetic")

    async def scenario():
        cfg = tmp_path / "relay.yaml"
        cfg.write_text("relay_url: ws://localhost:9999/ws\n", encoding="utf-8")
        cfg.chmod(0o600)
        mcp_entry_add(cfg, "probe", {"command": ["synthetic"]}, {})
        hub = McpHub(cfg, tmp_path, transport_factory=lambda launch: Transport())
        await hub.reconcile_all()
        assert hub.last_error("probe") == {
            "code": "spawn_failed", "message": "the local MCP server could not be started",
        }
        assert hub.state_of("probe") == AliasState.UNAVAILABLE

    asyncio.run(scenario())
    spawns = [line for line in logs if "spawn failed" in line]
    assert len(spawns) == 3
    for attempt, line in enumerate(spawns, 1):
        assert f"attempt={attempt}/3" in line
        assert "alias=probe" in line
        assert "ProviderConnectionError" in line
        assert "ExceptionGroup" in line
        assert "synthetic group" in line
        assert "FileNotFoundError" in line
        assert "PermissionError" in line
    closes = [line for line in logs if "close failed" in line]
    assert len(closes) == 3
    assert all("alias=probe" in line for line in closes)
    assert all("\n" not in line and len(line) <= 400 for line in logs)


def test_exception_chain_is_bounded_and_cycle_safe():
    from mcp_relay.providers.base import exception_type_chain

    first = RuntimeError("secret")
    second = ValueError("secret")
    first.__cause__ = second
    second.__cause__ = first
    assert exception_type_chain(first) == ("RuntimeError", "ValueError")
    many = ExceptionGroup("secret", [ValueError("secret") for _ in range(100)])
    result = exception_type_chain(many)
    assert result[-1] == "..."
    assert len(result) == 9
