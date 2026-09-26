"""Phase 2 slice 3: agent-facing control capability on the Relay Client."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from mcp_relay.capabilities.control import (
    CONTROL_TOOL_WIRE_NAMES,
    ControlCapability,
)
from mcp_relay.config import (
    MAX_MCP_COMMAND_ITEM_LENGTH,
    MAX_MCP_URL_LENGTH,
    mcp_entry_add,
    mcp_entry_set_enabled,
)
from mcp_relay.diagnostics import set_log_file
from mcp_relay.mcp_catalog import ClientCatalog
from mcp_relay.mcp_command import CommandError
from mcp_relay.mcp_hub import AliasLaunch, AliasState, HubError, McpHub
from mcp_relay.protocol import InvokeMessage
from mcp_relay.provider_tools import ProviderToolDescriptor
from mcp_relay.providers.base import validate_provider_arguments


def _write_yaml(path: Path, document: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _client_yaml(path: Path) -> Path:
    return _write_yaml(
        path,
        {
            "relay_url": "wss://relay.example.test/ws",
            "workspace": str(path.parent / "workspace"),
        },
    )


class FakeTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.closed = False
        self.list_count = 0

    async def list_tools(self, cursor: str | None = None) -> object:
        del cursor
        self.list_count += 1
        if self.fail:
            raise ConnectionError("synthetic failure")
        return {
            "tools": [
                {
                    "name": "ping",
                    "description": "synthetic ping",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
            ]
        }

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        del name, arguments
        return {"content": [{"type": "text", "text": "pong"}]}

    async def close(self) -> None:
        self.closed = True


class Factory:
    def __init__(self, *, fail_aliases: set[str] | None = None) -> None:
        self.launches: list[AliasLaunch] = []
        self.transports: list[FakeTransport] = []
        self.fail_aliases = fail_aliases or set()

    def __call__(self, launch: AliasLaunch) -> FakeTransport:
        self.launches.append(launch)
        transport = FakeTransport(fail=launch.alias in self.fail_aliases)
        self.transports.append(transport)
        return transport

    def for_alias(self, alias: str) -> list[FakeTransport]:
        return [
            transport
            for transport, launch in zip(self.transports, self.launches)
            if launch.alias == alias
        ]


class Inventory:
    """Records list_changed notifications triggered by mutations."""

    def __init__(self) -> None:
        self.notifications = 0

    async def __call__(self) -> None:
        self.notifications += 1


def _capability(
    config_path: Path,
    workspace: Path,
    factory: Factory,
    inventory: Inventory | None = None,
    admin_enabled: bool = True,
) -> ControlCapability:
    hub = McpHub(
        config_path,
        workspace,
        transport_factory=factory,
        spawn_attempts=1,
        provider_timeout_seconds=2.0,
    )
    return ControlCapability(
        hub=hub,
        workspace=workspace,
        client_version="0.1.0",
        on_inventory_change=None if inventory is None else inventory,
        admin_enabled=admin_enabled,
    )


def _invoke(
    capability: ControlCapability, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    message = InvokeMessage(
        version=2,
        type="invoke",
        request_id="req-1",
        tool_name=tool_name,
        arguments=arguments,
    )

    async def scenario() -> dict[str, Any]:
        return await capability.invoke(message)

    return asyncio.run(scenario())


# --------------------------------------------------------------------------
# Inventory and descriptors
# --------------------------------------------------------------------------


def test_capability_exposes_the_control_surface(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory()
    capability = _capability(config_path, workspace, factory)

    assert capability.tools == CONTROL_TOOL_WIRE_NAMES
    descriptors = asyncio.run(capability.list_tools())
    assert {f"{d.provider_name}.{d.tool_name}" for d in descriptors} == set(
        CONTROL_TOOL_WIRE_NAMES
    )
    derived_public_names = {
        f"relay_{descriptor.provider_name}_{descriptor.tool_name}"
        for descriptor in descriptors
    }
    assert {
        "relay_client_status",
        "relay_mcp_list",
        "relay_mcp_add",
        "relay_mcp_modify",
        "relay_mcp_delete",
        "relay_mcp_enable",
        "relay_mcp_disable",
    } == derived_public_names


def test_every_input_schema_is_closed(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    factory = Factory()
    capability = _capability(config_path, tmp_path, factory)
    for descriptor in asyncio.run(capability.list_tools()):
        schema = descriptor.input_schema
        assert schema.get("type") == "object"
        assert schema.get("additionalProperties") is False


# --------------------------------------------------------------------------
# Published schema / closed model boundary alignment
# --------------------------------------------------------------------------


def _add_descriptor(capability: ControlCapability) -> ProviderToolDescriptor:
    return next(
        descriptor
        for descriptor in asyncio.run(capability.list_tools())
        if (descriptor.provider_name, descriptor.tool_name) == ("mcp", "add")
    )


def test_published_schema_never_rejects_model_valid_entries(tmp_path: Path) -> None:
    """The generic entry schema must not be tighter than the closed model.

    A 2048-char url, a 512-char command item, or a long .env value is valid
    YAML/CLI input and must reach the capability — which owns the structured
    ``invalid_entry`` code — instead of dying at the argument boundary with a
    generic error.
    """
    config_path = _client_yaml(tmp_path / "config.yaml")
    capability = _capability(config_path, tmp_path, Factory())
    descriptor = _add_descriptor(capability)
    model_valid_entries = [
        # url: the closed model accepts up to MAX_MCP_URL_LENGTH characters.
        {"url": "https://a.test/" + "x" * (MAX_MCP_URL_LENGTH - len("https://a.test/"))},
        # command: single items up to MAX_MCP_COMMAND_ITEM_LENGTH characters.
        {"command": ["/bin/" + "x" * (MAX_MCP_COMMAND_ITEM_LENGTH - len("/bin/"))]},
        # env: values up to the .env byte budget, not the published 256 cap.
        {"url": "http://a.test/m", "env": {"KEY": "v" * 300}},
    ]
    for entry in model_valid_entries:
        arguments = validate_provider_arguments(
            descriptor, {"alias": "cua", "entry": entry}
        )
        assert arguments["entry"] == entry


def test_published_schema_defers_alias_bounds_to_the_model(tmp_path: Path) -> None:
    """An over-long alias passes the frontier and fails closed in the model."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    capability = _capability(config_path, tmp_path, Factory())
    descriptor = _add_descriptor(capability)
    validate_provider_arguments(
        descriptor, {"alias": "c" * 17, "entry": {"command": ["/bin/tool"]}}
    )
    result = _invoke(
        capability,
        "mcp.add",
        {"alias": "c" * 17, "entry": {"command": ["/bin/tool"]}},
    )
    assert result["code"] == "invalid_alias"


# --------------------------------------------------------------------------
# relay_client_status
# --------------------------------------------------------------------------


def test_client_status_reports_safe_runtime_metadata(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory()
    capability = _capability(config_path, workspace, factory)

    result = _invoke(capability, "client.status", {})

    client = result["client"]
    assert client["version"] == "0.1.0"
    assert client["protocol"] == 1
    assert isinstance(client["uptime_s"], int) and client["uptime_s"] >= 0
    assert client["workspace"] == str(workspace)
    assert result["disk_differs"] == []


# --------------------------------------------------------------------------
# relay_mcp_add
# --------------------------------------------------------------------------


def test_add_writes_yaml_starts_and_notifies(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory()
    inventory = Inventory()
    capability = _capability(config_path, workspace, factory, inventory)

    result = _invoke(
        capability,
        "mcp.add",
        {
            "alias": "cua",
            "entry": {"command": ["/absolute/cua-driver"], "env": {"API_KEY": "s3cret"}},
        },
    )

    assert result["alias"] == "cua"
    # The engine completes the bounded startup handshake before answering, so
    # the reported status is the reached runtime state.
    assert result["status"] == "running"
    assert factory.launches[-1].argv == ["/absolute/cua-driver"]
    assert factory.launches[-1].env["API_KEY"] == "s3cret"
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    stored = document["mcp_servers"]["cua"]
    assert stored == {"command": ["/absolute/cua-driver"], "enabled": True}
    assert "s3cret" not in config_path.read_text(encoding="utf-8")
    # Secret values never appear in tool results.
    assert "s3cret" not in str(result)
    assert inventory.notifications == 1


def test_add_rejects_existing_alias_without_touching_config(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory()
    mcp_entry_add(config_path, "cua", {"command": ["/bin/tool"]}, None)
    capability = _capability(config_path, workspace, factory)

    result = _invoke(
        capability,
        "mcp.add",
        {"alias": "cua", "entry": {"url": "http://127.0.0.1:9/mcp"}},
    )

    assert result["code"] == "alias_conflict"
    assert factory.launches == []
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["mcp_servers"]["cua"] == {
        "command": ["/bin/tool"],
        "enabled": True,
    }


@pytest.mark.parametrize(
    "alias",
    ["CUA", "cua_driver", "cua2", "c" * 17, ""],
)
def test_add_rejects_invalid_aliases(alias: str, tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _capability(config_path, workspace, Factory())

    result = _invoke(
        capability, "mcp.add", {"alias": alias, "entry": {"command": ["/bin/tool"]}}
    )
    assert result["code"] == "invalid_alias"


def test_add_rejects_entries_with_two_kinds(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _capability(config_path, workspace, Factory())

    result = _invoke(
        capability,
        "mcp.add",
        {
            "alias": "cua",
            "entry": {
                "command": ["/bin/tool"],
                "url": "http://127.0.0.1:9/mcp",
            },
        },
    )
    assert result["code"] == "invalid_entry"


def test_add_reports_spawn_failure_but_keeps_the_commit(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory(fail_aliases={"cua"})
    inventory = Inventory()
    capability = _capability(config_path, workspace, factory, inventory)

    result = _invoke(
        capability,
        "mcp.add",
        {"alias": "cua", "entry": {"command": ["/absolute/cua-driver"]}},
    )

    assert result["code"] == "spawn_failed"
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["mcp_servers"]["cua"]["command"] == [
        "/absolute/cua-driver"
    ]
    # The write committed, so the inventory did change.
    assert inventory.notifications == 1


# --------------------------------------------------------------------------
# relay_mcp_list
# --------------------------------------------------------------------------


def test_list_reports_hub_state_without_secret_values(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory()
    mcp_entry_add(config_path, "cua", {"command": ["/bin/cua"]}, {"API_KEY": "s3cret"})
    capability = _capability(config_path, workspace, factory)
    capability.bind_catalog(ClientCatalog())
    capability.hub.publish_catalog(capability._catalog)
    asyncio.run(capability.hub.reconcile_all())
    capability.hub.publish_catalog(capability._catalog)

    result = _invoke(capability, "mcp.list", {})

    servers = result["items"]
    assert result["level"] == "servers"
    assert len(servers) == 1
    server = servers[0]
    assert server["alias"] == "cua"
    assert server["enabled"] is True
    assert server["runtime_state"] == "running"
    assert server["transport"] == "stdio"
    assert server["entry"]["command"] == ["/bin/cua"]
    assert server["env_keys"] == ["API_KEY"]
    assert "s3cret" not in str(result)
    assert server["last_error"] is None


def test_list_filters_one_alias(tmp_path: Path) -> None:
    """The alias filter selects one server at the servers level; at the tools
    level it lists that alias's exact tool names."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    mcp_entry_add(config_path, "two", {"command": ["/bin/two"]}, None)
    capability = _capability(config_path, workspace, factory)
    capability.bind_catalog(ClientCatalog())
    capability.hub.publish_catalog(capability._catalog)
    asyncio.run(capability.hub.reconcile_all())
    capability.hub.publish_catalog(capability._catalog)

    servers = _invoke(capability, "mcp.list", {})
    assert [server["alias"] for server in servers["items"]] == ["one", "two"]

    tools = _invoke(capability, "mcp.list", {"alias": "one"})
    assert tools["level"] == "tools"
    assert tools["alias"] == "one"
    assert [tool["name"] for tool in tools["items"]] == ["ping"]


def test_list_reports_unavailable_alias_with_last_error(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory(fail_aliases={"broken"})
    mcp_entry_add(config_path, "broken", {"command": ["/bin/broken"]}, None)
    capability = _capability(config_path, workspace, factory)
    capability.bind_catalog(ClientCatalog())
    capability.hub.publish_catalog(capability._catalog)
    asyncio.run(capability.hub.reconcile_all())
    capability.hub.publish_catalog(capability._catalog)

    # The servers level keeps the hub state: unavailable + safe error.
    servers = _invoke(capability, "mcp.list", {})
    server = servers["items"][0]
    assert server["alias"] == "broken"
    assert server["runtime_state"] == "unavailable"
    assert server["catalog_available"] is False
    assert server["last_error"]["code"] == "spawn_failed"

    # At the tools level the same alias answers alias_unavailable (raised:
    # the Client transports it as a ClientError frame).
    with pytest.raises(CommandError) as refused:
        _invoke(capability, "mcp.list", {"alias": "broken"})
    assert refused.value.code == "alias_unavailable"


# --------------------------------------------------------------------------
# relay_mcp_modify / relay_mcp_delete
# --------------------------------------------------------------------------


def test_modify_full_replaces_and_bounces(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory()
    mcp_entry_add(config_path, "cua", {"command": ["/bin/cua"]}, {"A": "1"})
    capability = _capability(config_path, workspace, factory)
    asyncio.run(capability.hub.reconcile_all())

    result = _invoke(
        capability,
        "mcp.modify",
        {
            "alias": "cua",
            "entry": {"url": "http://127.0.0.1:9/mcp", "env": {"B": "2"}},
        },
    )

    assert result["status"] in {"starting", "running"}
    transports = factory.for_alias("cua")
    assert len(transports) == 2
    assert transports[0].closed
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    stored = document["mcp_servers"]["cua"]
    assert stored == {"url": "http://127.0.0.1:9/mcp", "enabled": True}
    assert "A" not in str(result)
    from mcp_relay.config import read_alias_env

    assert read_alias_env(config_path, "cua") == {"B": "2"}


def test_modify_unknown_alias_is_alias_unknown(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _capability(config_path, workspace, Factory())

    result = _invoke(
        capability,
        "mcp.modify",
        {"alias": "ghost", "entry": {"command": ["/bin/tool"]}},
    )
    assert result["code"] == "alias_unknown"


def test_delete_stops_removes_and_requires_existence(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory()
    mcp_entry_add(config_path, "cua", {"command": ["/bin/cua"]}, {"A": "1"})
    capability = _capability(config_path, workspace, factory)
    asyncio.run(capability.hub.reconcile_all())

    result = _invoke(capability, "mcp.delete", {"alias": "cua"})
    assert result == {"alias": "cua", "status": "deleted"}
    assert factory.for_alias("cua")[0].closed
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "cua" not in document["mcp_servers"]
    from mcp_relay.config import alias_dotenv_path

    assert not alias_dotenv_path(config_path, "cua").exists()

    result = _invoke(capability, "mcp.delete", {"alias": "cua"})
    assert result["code"] == "alias_unknown"


def test_delete_commits_before_stopping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed YAML write leaves the alias declared and running.

    Invariant 2: validate → write (commit) → apply. Stopping the process
    before the commit would leave a declared-but-stopped alias when the
    write fails, with nothing in the tool result explaining the drift.
    """
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory()
    mcp_entry_add(config_path, "cua", {"command": ["/bin/cua"]}, None)
    capability = _capability(config_path, workspace, factory)
    asyncio.run(capability.hub.reconcile_all())
    assert capability.hub.state_of("cua") is AliasState.RUNNING

    from mcp_relay.capabilities import control as control_module
    from mcp_relay.config import ConfigError

    def failing_remove(path: object, alias: str) -> None:
        del path, alias
        raise ConfigError("synthetic write failure")

    monkeypatch.setattr(control_module, "mcp_entry_remove", failing_remove)

    result = _invoke(capability, "mcp.delete", {"alias": "cua"})

    assert result["code"] == "config_invalid"
    # The commit failed, so nothing was applied: the process keeps running.
    assert not factory.for_alias("cua")[0].closed
    assert capability.hub.state_of("cua") is AliasState.RUNNING
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "cua" in document["mcp_servers"]


def test_delete_cleans_the_alias_launcher_cache(tmp_path: Path) -> None:
    """Delete removes the deleted alias's launcher cache directory.

    ``<config dir>/mcp/<alias>`` (by default ``~/.mcp-relay/mcp/<alias>``)
    holds uvx/npx downloads spawned for the alias; once the alias is deleted
    from YAML and runtime the directory is garbage and is removed
    best-effort.
    """
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory()
    mcp_entry_add(config_path, "cua", {"command": ["/bin/cua"]}, None)
    cache_dir = tmp_path / "mcp" / "cua"
    cache_dir.mkdir(parents=True)
    (cache_dir / "package-blob").write_text("cached", encoding="utf-8")
    capability = _capability(config_path, workspace, factory)
    asyncio.run(capability.hub.reconcile_all())

    result = _invoke(capability, "mcp.delete", {"alias": "cua"})

    assert result == {"alias": "cua", "status": "deleted"}
    assert not cache_dir.exists()


# --------------------------------------------------------------------------
# relay_mcp_enable / relay_mcp_disable (idempotent, one parameter)
# --------------------------------------------------------------------------


def test_enable_and_disable_reconcile_idempotently(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory()
    mcp_entry_add(config_path, "cua", {"command": ["/bin/cua"]}, None)
    mcp_entry_set_enabled(config_path, "cua", False)
    capability = _capability(config_path, workspace, factory)

    result = _invoke(capability, "mcp.enable", {"alias": "cua"})
    assert result == {"alias": "cua", "enabled": True, "runtime_state": "running"}
    assert len(factory.for_alias("cua")) == 1

    again = _invoke(capability, "mcp.enable", {"alias": "cua"})
    assert again == result
    assert len(factory.for_alias("cua")) == 1

    disabled = _invoke(capability, "mcp.disable", {"alias": "cua"})
    assert disabled == {"alias": "cua", "enabled": False, "runtime_state": "disabled"}
    assert factory.for_alias("cua")[0].closed

    disabled_again = _invoke(capability, "mcp.disable", {"alias": "cua"})
    assert disabled_again == disabled

    result = _invoke(capability, "mcp.enable", {"alias": "ghost"})
    assert result["code"] == "alias_unknown"


# --------------------------------------------------------------------------
# Administration gating (fail-closed)
# --------------------------------------------------------------------------


def test_locked_admin_refuses_mutating_verbs_with_permission_denied(
    tmp_path: Path,
) -> None:
    """Absent/false ``client.admin`` refuses exactly the five admin verbs."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _capability(config_path, workspace, Factory(), admin_enabled=False)

    for verb in ("mcp.add", "mcp.modify", "mcp.delete", "mcp.enable", "mcp.disable"):
        arguments: dict[str, Any] = {"alias": "any"} if verb != "mcp.add" else {
            "alias": "any",
            "entry": {"command": ["/bin/x"]},
        }
        result = _invoke(capability, verb, arguments)
        assert result["code"] == "permission_denied", verb

    # Read-only verbs stay available regardless of the switch.
    assert _invoke(capability, "client.status", {})["admin"] is False
    assert _invoke(capability, "client.status", {})["client"]["version"] == "0.1.0"


def test_unlocked_admin_allows_mutating_verbs(tmp_path: Path) -> None:
    """Explicit ``admin_enabled=True`` (from ``client.admin: true``) unlocks."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _capability(config_path, workspace, Factory(), admin_enabled=True)

    result = _invoke(capability, "mcp.add", {"alias": "one", "entry": {"command": ["/bin/one"]}})
    assert result.get("code") != "permission_denied"


def test_default_capability_construction_is_fail_closed(
    tmp_path: Path,
) -> None:
    """ControlCapability's own default is locked (admin_enabled defaults False)."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hub = McpHub(
        config_path,
        workspace,
        transport_factory=Factory(),
        spawn_attempts=1,
        provider_timeout_seconds=2.0,
    )
    capability = ControlCapability(
        hub=hub,
        workspace=workspace,
        client_version="0.1.0",
    )

    assert capability._admin_enabled is False
    result = _invoke(capability, "mcp.enable", {"alias": "any"})
    assert result["code"] == "permission_denied"


# --------------------------------------------------------------------------
# Dispatch hardening
# --------------------------------------------------------------------------


def test_unknown_wire_tool_is_rejected(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    capability = _capability(config_path, tmp_path, Factory())

    result = _invoke(capability, "mcp.unknown", {})
    assert result["code"] == "invalid_entry"
    assert "unknown" in result["message"]


def test_capabilities_survive_start_and_close(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _capability(config_path, workspace, Factory())
    asyncio.run(capability.start())
    asyncio.run(capability.aclose())
    asyncio.run(capability.aclose())


def test_descriptor_inventory_matches_wire_names(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    factory = Factory()
    capability = _capability(config_path, tmp_path, factory)
    descriptors: tuple[ProviderToolDescriptor, ...] = tuple(
        asyncio.run(capability.list_tools())
    )
    wire_names = {f"{d.provider_name}.{d.tool_name}" for d in descriptors}
    assert wire_names == set(CONTROL_TOOL_WIRE_NAMES)


# --------------------------------------------------------------------------
# RelayClient integration: dynamic providers and re-announcement
# --------------------------------------------------------------------------


class _RecordingSocket:
    """Minimal TextSocket double capturing outbound frames."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.sent: list[dict[str, Any]] = []

    async def send(self, payload: str) -> None:
        import json as _json

        self.sent.append(_json.loads(payload))

    async def recv(self) -> str:

        if self._replies:
            return self._replies.pop(0)
        await asyncio.sleep(3600)
        raise AssertionError("no reply queued")


def _client_settings(workspace: Path) -> Any:
    from mcp_relay.client import ClientSettings

    return ClientSettings(
        server_url="ws://localhost/ws",
        client_id="d",
        client_token='client-synthetic-credential-0000000000000000',
        workspace=workspace,
    )


def test_reannounce_sends_updated_capabilities_frame(tmp_path: Path) -> None:
    from mcp_relay.client import RelayClient

    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    capability = _capability(config_path, workspace, factory)
    asyncio.run(capability.hub.reconcile_all())

    async def resolver() -> dict[str, Any]:
        return capability.hub.provider_clients()

    client = RelayClient(
        _client_settings(workspace),
        capabilities=[capability],
        provider_resolver=resolver,
    )
    capability.bind_inventory_change(client.reannounce)
    socket = _RecordingSocket(
        [
            '{"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": 1}',
        ]
    )

    async def scenario() -> None:
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(200):
            if any(frame.get("type") == "capabilities" for frame in socket.sent):
                break
            await asyncio.sleep(0.005)
        initial = next(
            frame for frame in socket.sent if frame.get("type") == "capabilities"
        )
        assert "client.status" in initial["tools"]
        assert "mcp.add" in initial["tools"]

        # A mutation that changes the inventory re-announces over the socket.
        result = await capability.invoke(
            InvokeMessage(
                version=2,
                type="invoke",
                request_id="add-1",
                tool_name="mcp.add",
                arguments={
                    "alias": "two",
                    "entry": {"command": ["/bin/two"]},
                },
            )
        )
        assert result.get("status") == "running"
        client.stop()
        await task
        await client.aclose()

    asyncio.run(scenario())

    frames = [frame for frame in socket.sent if frame.get("type") == "capabilities"]
    assert len(frames) >= 2
    latest = frames[-1]
    # The re-announced frame is invariant: exactly the fixed wire operations,
    # independent of the alias mutation (the facade no longer publishes
    # per-alias descriptors; the catalog carries the third-party inventory).
    from mcp_relay.relay_tools import WIRE_OPERATION_NAMES

    assert set(latest["tools"]) == set(WIRE_OPERATION_NAMES)


def test_reannounce_without_socket_is_a_safe_no_op(tmp_path: Path) -> None:
    from mcp_relay.client import RelayClient

    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _capability(config_path, workspace, Factory())
    client = RelayClient(_client_settings(workspace), capabilities=[capability])
    capability.bind_inventory_change(client.reannounce)

    asyncio.run(client.reannounce())  # no session yet; must not raise
    asyncio.run(client.aclose())


# --------------------------------------------------------------------------
# Step 7B: administration-triggered startup under the shorter call deadline.
# --------------------------------------------------------------------------


def test_admin_startup_exceeding_the_call_deadline_is_honest(
    tmp_path: Path,
) -> None:
    """mcp.enable whose startup outlives the caller's (shorter) deadline.

    The ordinary invocation deadline is shorter than the 120 s startup
    budget. When it fires first, the call is cancelled: no fabricated
    success, no job system. The hub keeps the committed YAML, closes the
    in-flight transport (proven cleanup), reports the ``spawn_cancelled``
    uncertainty, and the published catalog never shows a fake running
    state.
    """
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "slow", {"command": ["/bin/slow"]}, None)
    mcp_entry_set_enabled(config_path, "slow", False)

    class SlowTransport(FakeTransport):
        async def list_tools(self, cursor: str | None = None) -> object:
            await asyncio.sleep(2.0)
            return await super().list_tools(cursor)

    handed: list[FakeTransport] = []

    def factory(launch: AliasLaunch) -> FakeTransport:
        del launch
        transport = SlowTransport()
        handed.append(transport)
        return transport

    hub = McpHub(
        config_path,
        workspace,
        transport_factory=factory,
        spawn_attempts=1,
        provider_timeout_seconds=2.0,
    )
    catalog = ClientCatalog()
    hub.bind_on_change(lambda: hub.publish_catalog(catalog))
    capability = ControlCapability(
        hub=hub,
        workspace=workspace,
        client_version="0.1.0",
        admin_enabled=True,
    )
    capability.bind_catalog(catalog)

    message = InvokeMessage(
        version=2,
        type="invoke",
        request_id="req-1",
        tool_name="mcp.enable",
        arguments={"alias": "slow"},
    )

    async def scenario() -> None:
        call = asyncio.create_task(capability.invoke(message))
        with pytest.raises(asyncio.TimeoutError):
            # The caller's deadline is shorter than the startup budget.
            await asyncio.wait_for(call, timeout=0.2)
        assert call.cancelled()
        # Honest uncertainty: cancelled, never a fake startup success.
        assert hub.state_of("slow") is AliasState.UNAVAILABLE
        error = hub.last_error("slow")
        assert error is not None
        assert error["code"] == "spawn_cancelled"
        # Proven cleanup: the in-flight transport was closed.
        assert handed
        assert handed[0].closed is True
        # The YAML commit of mcp.enable is kept.
        from mcp_relay.config import mcp_entries

        entries = mcp_entries(config_path)
        assert entries["slow"]["enabled"] is True
        # The catalog was re-published after the cancellation: no fake
        # running state lingers from the STARTING publication.
        record = catalog._records["slow"]
        assert record.runtime_state == "unavailable"
        assert record.catalog_available is False

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Step 9: hub counters in client.status (configured / running / tools)
# --------------------------------------------------------------------------


class DelayedTransport(FakeTransport):
    """Synthetic transport whose inventory waits an injected delay."""

    def __init__(self, *, delay: float = 0.0, fail: bool = False) -> None:
        super().__init__(fail=fail)
        self.delay = delay

    async def list_tools(self, cursor: str | None = None) -> object:
        if self.delay:
            await asyncio.sleep(self.delay)
        return await super().list_tools(cursor)


def _catalog_capability(
    config_path: Path,
    workspace: Path,
    factory: Any,
    *,
    admin_enabled: bool = True,
) -> tuple[ControlCapability, ClientCatalog]:
    hub = McpHub(
        config_path,
        workspace,
        transport_factory=factory,
        spawn_attempts=1,
        provider_timeout_seconds=2.0,
    )
    capability = ControlCapability(
        hub=hub,
        workspace=workspace,
        client_version="0.1.0",
        admin_enabled=admin_enabled,
    )
    catalog = ClientCatalog()
    hub.bind_on_change(lambda: hub.publish_catalog(catalog))
    capability.bind_catalog(catalog)
    return capability, catalog


def _status(capability: ControlCapability) -> dict:
    return _invoke(capability, "client.status", {})


async def _status_async(capability: ControlCapability) -> dict:
    message = InvokeMessage(
        version=2,
        type="invoke",
        request_id="req-s",
        tool_name="client.status",
        arguments={},
    )
    return await capability.invoke(message)


def test_status_hub_counters_with_mixed_alias_states(tmp_path: Path) -> None:
    """Counters separate configured, actually-available and tool totals."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "dis", {"command": ["/bin/dis"]}, None)
    mcp_entry_set_enabled(config_path, "dis", False)
    mcp_entry_add(config_path, "run", {"command": ["/bin/run"]}, None)
    mcp_entry_add(config_path, "str", {"command": ["/bin/str"]}, None)
    mcp_entry_add(config_path, "una", {"command": ["/bin/una"]}, None)

    transports = {
        "run": FakeTransport(),
        "str": DelayedTransport(delay=0.5),
        "una": FakeTransport(fail=True),
    }

    def factory(launch: AliasLaunch) -> FakeTransport:
        return transports[launch.alias]

    capability, catalog = _catalog_capability(config_path, workspace, factory)
    hub = capability.hub

    async def scenario() -> None:
        task = asyncio.create_task(hub.reconcile_all())
        for _ in range(200):
            if hub.state_of("str") is AliasState.STARTING:
                break
            await asyncio.sleep(0.01)
        mid = (await _status_async(capability))["hub"]
        # Four configured third-party servers in the YAML; only "run" is
        # actually available mid-flight ("str" is starting, "una" and "dis"
        # are not executable), so exactly one tool is available.
        assert mid == {
            "configured_aliases": 4,
            "running_aliases": 1,
            "available_tools": 1,
        }
        await asyncio.wait_for(task, timeout=5)
        final = (await _status_async(capability))["hub"]
        # "str" landed as running and "una" as unavailable, "dis" stays
        # disabled: the counters keep reflecting real availability.
        assert final == {
            "configured_aliases": 4,
            "running_aliases": 2,
            "available_tools": 2,
        }
        states = {
            view["alias"]: view["runtime_state"]
            for view in catalog.snapshot.servers_view()
        }
        assert states == {
            "dis": "disabled",
            "run": "running",
            "str": "running",
            "una": "unavailable",
        }

    asyncio.run(scenario())


def test_status_running_counter_follows_availability_not_stale_records(
    tmp_path: Path,
) -> None:
    """A RUNNING alias with a dead inventory is not counted as available."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "good", {"command": ["/bin/good"]}, None)
    transports = {"good": FakeTransport()}

    def factory(launch: AliasLaunch) -> FakeTransport:
        return transports[launch.alias]

    capability, catalog = _catalog_capability(config_path, workspace, factory)
    hub = capability.hub

    asyncio.run(hub.reconcile_all())
    assert _status(capability)["hub"] == {
        "configured_aliases": 1,
        "running_aliases": 1,
        "available_tools": 1,
    }

    # The provider became unavailable: upstream inventory invalidated.
    provider = hub.provider_clients()["good"]
    provider.invalidate_inventory()
    hub.publish_catalog(catalog)
    record = catalog._records["good"]
    assert record.runtime_state == "running"  # stale record on purpose
    assert record.catalog_available is False

    status = _status(capability)
    assert status["hub"] == {
        "configured_aliases": 1,
        "running_aliases": 0,
        "available_tools": 0,
    }


def test_status_counters_never_count_internal_tools_as_servers(
    tmp_path: Path,
) -> None:
    """An empty hub reports zero servers and zero tools — never the 7
    internal control tools; the catalog carries third-party aliases only."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = Factory()
    capability, catalog = _catalog_capability(config_path, workspace, factory)

    status = _status(capability)
    assert status["hub"] == {
        "configured_aliases": 0,
        "running_aliases": 0,
        "available_tools": 0,
    }
    assert catalog.snapshot.servers_view() == []


# --------------------------------------------------------------------------
# Task 5: sanitized admin operation events (unified logging)
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _captured_admin_logs() -> Iterator[tuple[io.StringIO, Path]]:
    """Capture both diagnostics sinks around one capability invocation."""
    import tempfile

    stderr = io.StringIO()
    handle = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
    handle.close()
    log_path = Path(handle.name)
    old_stderr = sys.stderr
    old_env = os.environ.get("LOG_LEVEL")
    os.environ["LOG_LEVEL"] = "INFO"
    sys.stderr = stderr
    set_log_file(log_path)
    try:
        yield stderr, log_path
    finally:
        set_log_file(None)
        sys.stderr = old_stderr
        if old_env is None:
            os.environ.pop("LOG_LEVEL", None)
        else:
            os.environ["LOG_LEVEL"] = old_env


def _admin_lines(text: str) -> list[str]:
    return [
        line for line in text.splitlines() if "mcp.admin" in line and line.strip()
    ]


def test_admin_success_logs_one_sanitized_line(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _capability(config_path, workspace, Factory())

    with _captured_admin_logs() as (stderr, log_path):
        result = _invoke(
            capability,
            "mcp.add",
            {
                "alias": "cua",
                "entry": {"command": ["/absolute/cua-driver"], "env": {"API_KEY": "s3cret"}},
            },
        )
    assert result["status"] == "running"

    file_lines = _admin_lines(log_path.read_text(encoding="utf-8"))
    stderr_lines = _admin_lines(stderr.getvalue())
    assert len(file_lines) == 1, file_lines
    assert stderr_lines == file_lines
    line = file_lines[0]
    assert "operation=mcp.add" in line
    assert "request_id=req-1" in line
    assert "alias=cua" in line
    assert "result=ok" in line
    # Sanitization contract: never entry, command, args, env, config URL.
    assert "entry" not in line
    assert "cua-driver" not in line
    assert "s3cret" not in line
    assert "API_KEY" not in line


def test_admin_refusal_when_disabled_is_logged_with_closed_code(
    tmp_path: Path,
) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _capability(
        config_path, workspace, Factory(), admin_enabled=False
    )

    with _captured_admin_logs() as (stderr, log_path):
        result = _invoke(
            capability,
            "mcp.delete",
            {"alias": "cua"},
        )
    assert result["code"] == "permission_denied"

    file_lines = _admin_lines(log_path.read_text(encoding="utf-8"))
    assert len(file_lines) == 1, file_lines
    line = file_lines[0]
    assert "operation=mcp.delete" in line
    assert "request_id=req-1" in line
    assert "alias=cua" in line
    assert "code=permission_denied" in line
    assert "result=ok" not in line
    assert "[ERROR]" in line
    assert _admin_lines(stderr.getvalue()) == file_lines


def test_admin_structured_error_return_is_logged_with_its_code(
    tmp_path: Path,
) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _capability(config_path, workspace, Factory())

    with _captured_admin_logs() as (stderr, log_path):
        result = _invoke(capability, "mcp.delete", {"alias": "ghost"})
    assert result["code"] == "alias_unknown"

    file_lines = _admin_lines(log_path.read_text(encoding="utf-8"))
    assert len(file_lines) == 1, file_lines
    line = file_lines[0]
    assert "operation=mcp.delete" in line
    assert "alias=ghost" in line
    assert "code=alias_unknown" in line
    assert "[ERROR]" in line
    assert _admin_lines(stderr.getvalue()) == file_lines


def test_admin_invalid_alias_line_carries_no_unvalidated_identifier(
    tmp_path: Path,
) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _capability(config_path, workspace, Factory())

    with _captured_admin_logs() as (stderr, log_path):
        result = _invoke(
            capability,
            "mcp.add",
            {"alias": "BAD ALIAS", "entry": {"command": ["/bin/tool"]}},
        )
    assert result["code"] == "invalid_alias"

    file_lines = _admin_lines(log_path.read_text(encoding="utf-8"))
    assert len(file_lines) == 1, file_lines
    line = file_lines[0]
    assert "code=invalid_alias" in line
    assert "alias=" not in line
    assert "BAD ALIAS" not in line


def test_admin_raised_error_is_logged_once_with_its_code(
    tmp_path: Path,
) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "cua", {"command": ["/bin/tool"]}, None)
    capability = _capability(config_path, workspace, Factory())

    async def failing_reconcile(alias: str) -> object:
        raise HubError("execution_failed", "synthetic startup failure")

    capability.hub.reconcile_alias = failing_reconcile  # type: ignore[method-assign]

    with _captured_admin_logs() as (stderr, log_path):
        with pytest.raises(CommandError) as excinfo:
            _invoke(capability, "mcp.enable", {"alias": "cua"})
    assert excinfo.value.code == "execution_failed"

    file_lines = _admin_lines(log_path.read_text(encoding="utf-8"))
    assert len(file_lines) == 1, file_lines
    line = file_lines[0]
    assert "operation=mcp.enable" in line
    assert "alias=cua" in line
    assert "code=execution_failed" in line
    assert "synthetic startup failure" not in line


def test_read_verbs_are_never_logged_as_admin_mutations(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = _capability(config_path, workspace, Factory())

    with _captured_admin_logs() as (stderr, log_path):
        _invoke(capability, "client.status", {})
        with pytest.raises(CommandError):
            _invoke(capability, "mcp.list", {})

    assert _admin_lines(stderr.getvalue()) == []
    assert _admin_lines(log_path.read_text(encoding="utf-8")) == []
