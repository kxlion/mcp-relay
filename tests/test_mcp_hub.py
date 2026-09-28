"""Per-alias reconcile engine (``McpHub``) contracts."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from mcp_relay import config
from mcp_relay.config import (
    mcp_entry_add,
    mcp_entry_replace,
    mcp_entry_set_enabled,
    write_alias_env,
)
from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog
from mcp_relay.mcp_hub import (
    AliasLaunch,
    AliasState,
    HubError,
    McpHub,
    remove_alias_cache,
)
from mcp_relay.mcp_registry import RegistryUnreachableError
from mcp_relay.output_models import ProviderToolResult
from mcp_relay.provider_tools import ProviderToolDescriptor
from mcp_relay.providers.base import DEFAULT_PROVIDER_TIMEOUT_SECONDS
from mcp_relay.providers.mcp_client import McpProviderToolClient

# One loop per module: the hub keeps per-alias watch tasks across calls.
_LOOP = asyncio.new_event_loop()


def _run(coroutine: Any) -> Any:
    return _LOOP.run_until_complete(coroutine)


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
    """Synthetic McpTransport; bounded inventory, no real process or network."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.closed = False
        self.descriptor = ProviderToolDescriptor(
            provider_name="probe",
            tool_name="ping",
            description="synthetic ping",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )

    async def list_tools(self, cursor: str | None = None) -> object:
        self.calls += 1
        if self.fail:
            raise ConnectionError("synthetic transport failure")
        # MCP wire shape: each tool carries name/description/inputSchema.
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
    ) -> ProviderToolResult:
        del name, arguments
        return ProviderToolResult(
            content=[{"type": "text", "text": "pong"}],
        )

    async def close(self) -> None:
        self.closed = True


class FactoryRecorder:
    """Injectable transport factory recording every launch request."""

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


def _hub(
    config_path: Path,
    workspace: Path,
    factory: FactoryRecorder,
    *,
    resolver: Any = None,
    attempts: int = 3,
) -> McpHub:
    return McpHub(
        config_path,
        workspace,
        transport_factory=factory,
        source_resolver=resolver,
        spawn_attempts=attempts,
    )


def test_startup_spawns_enabled_and_skips_disabled(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    mcp_entry_add(config_path, "two", {"command": ["/bin/two"]}, None)
    mcp_entry_set_enabled(config_path, "two", False)
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)

    statuses = _run(hub.reconcile_all())

    assert statuses["one"].state is AliasState.RUNNING
    assert statuses["two"].state is AliasState.DISABLED
    assert [launch.alias for launch in factory.launches] == ["one"]
    launch = factory.launches[0]
    assert launch.transport == "stdio"
    assert launch.argv == ["/bin/one"]


def test_url_alias_uses_streamable_http_without_process(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "httpd", {"url": "http://127.0.0.1:9000/mcp"}, None)
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)

    statuses = _run(hub.reconcile_all())

    assert statuses["httpd"].state is AliasState.RUNNING
    assert factory.launches[0].transport == "streamable_http"
    assert factory.launches[0].url == "http://127.0.0.1:9000/mcp"
    assert factory.launches[0].argv is None


def test_spawn_failure_is_retried_then_unavailable_isolated(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "broken", {"command": ["/bin/broken"]}, None)
    mcp_entry_add(config_path, "healthy", {"command": ["/bin/healthy"]}, None)
    factory = FactoryRecorder(fail_aliases={"broken"})
    hub = _hub(config_path, workspace, factory, attempts=3)

    statuses = _run(hub.reconcile_all())

    assert statuses["broken"].state is AliasState.UNAVAILABLE
    assert statuses["healthy"].state is AliasState.RUNNING
    assert len(factory.for_alias("broken")) == 3
    error = statuses["broken"].last_error
    assert error is not None and error["code"] == "spawn_failed"
    # Invariant 3: the YAML never mutates itself after a runtime failure.
    entries = config.mcp_entries(config_path)
    assert entries["broken"] == {"command": ["/bin/broken"], "enabled": True}


def test_source_resolution_happens_at_spawn_time(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "src", {"source": "io.example/author/server"}, None)
    resolved: list[tuple[str, str | None]] = []

    async def resolver(source: str, version: str | None) -> Any:
        resolved.append((source, version))
        return {
            "name": source,
            "description": "resolved",
            "packages": [
                {"registry_type": "npm", "identifier": "server-pkg", "version": "1.2.3"}
            ],
        }

    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory, resolver=resolver)

    statuses = _run(hub.reconcile_all())

    assert statuses["src"].state is AliasState.RUNNING
    assert resolved == [("io.example/author/server", None)]
    launch = factory.launches[0]
    assert launch.argv == ["npx", "-y", "server-pkg@1.2.3"]
    # The declarative launcher cache lives under the relay home next to the
    # per-alias .env files (never inside the workspace); the launcher runs
    # with the cache as cwd and cache variables pointed there. Nothing is
    # persisted back into the YAML.
    cache_dir = config_path.parent / "mcp" / "src"
    assert launch.cwd == cache_dir
    assert launch.env["NPM_CONFIG_CACHE"] == str(cache_dir)
    assert cache_dir.is_dir()
    if os.name != "nt":
        assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700
    entries = config.mcp_entries(config_path)
    assert entries["src"] == {"source": "io.example/author/server", "enabled": True}


def test_source_with_pin_resolves_the_pinned_version(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(
        config_path,
        "src",
        {"source": "io.example/author/server", "version": "9.9.9"},
        None,
    )

    async def resolver(source: str, version: str | None) -> Any:
        return {
            "name": source,
            "description": "resolved",
            "packages": [
                {
                    "registry_type": "npm",
                    "identifier": "server-pkg",
                    "version": version,
                }
            ],
        }

    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory, resolver=resolver)
    _run(hub.reconcile_all())
    assert factory.launches[0].argv == ["npx", "-y", "server-pkg@9.9.9"]


# --------------------------------------------------------------------------
# Launcher cache location (relay home)
# --------------------------------------------------------------------------


def test_cache_dir_resolves_under_the_relay_home_not_the_workspace(
    tmp_path: Path,
) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hub = _hub(config_path, workspace, FactoryRecorder())

    cache_dir = hub._cache_dir("src")

    # Same relay home parent that holds the per-alias .env files
    # (<config dir>/mcp/<alias>, by default ~/.mcp-relay/mcp/<alias>).
    assert cache_dir == tmp_path / "mcp" / "src"
    assert cache_dir == config.alias_dotenv_path(config_path, "src").parent / "src"
    assert workspace not in cache_dir.parents


def test_remove_alias_cache_removes_under_the_relay_home_only(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    cache_dir = tmp_path / "mcp" / "src"
    cache_dir.mkdir(parents=True)
    (cache_dir / "package-blob").write_text("cached", encoding="utf-8")
    other = tmp_path / "mcp" / "other"
    other.mkdir(parents=True)

    remove_alias_cache(config_path, "src")

    assert not cache_dir.exists()
    assert other.is_dir()


def test_registry_unreachable_leaves_alias_unavailable(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "src", {"source": "io.example/author/server"}, None)

    async def resolver(source: str, version: str | None) -> Any:
        del source
        raise RegistryUnreachableError("offline")

    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)

    statuses = _run(hub.reconcile_all())

    assert statuses["src"].state is AliasState.UNAVAILABLE
    assert statuses["src"].last_error is not None
    assert statuses["src"].last_error["code"] == "registry_unreachable"
    assert factory.launches == []


def test_unsupported_registry_type_maps_to_transport_unsupported(
    tmp_path: Path,
) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "src", {"source": "io.example/author/server"}, None)

    async def resolver(source: str, version: str | None) -> Any:
        return {
            "name": source,
            "description": "resolved",
            "packages": [{"registry_type": "oci", "identifier": "img"}],
        }

    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory, resolver=resolver)
    statuses = _run(hub.reconcile_all())
    assert statuses["src"].state is AliasState.UNAVAILABLE
    assert statuses["src"].last_error is not None
    assert statuses["src"].last_error["code"] == "transport_unsupported"


def test_alias_env_reaches_the_transport_factory_not_the_yaml(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, {"API_KEY": "abc"})
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)

    _run(hub.reconcile_all())

    launch = factory.launches[0]
    assert launch.env.get("API_KEY") == "abc"


def test_reconcile_running_alias_with_same_config_is_a_no_op(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)

    _run(hub.reconcile_all())
    _run(hub.reconcile_alias("one"))

    assert len(factory.for_alias("one")) == 1


def test_disable_stops_and_enable_spawns_again(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)
    _run(hub.reconcile_all())

    mcp_entry_set_enabled(config_path, "one", False)
    status = _run(hub.reconcile_alias("one"))
    assert status.state is AliasState.DISABLED
    assert factory.for_alias("one")[0].closed

    mcp_entry_set_enabled(config_path, "one", True)
    status = _run(hub.reconcile_alias("one"))
    assert status.state is AliasState.RUNNING
    assert len(factory.for_alias("one")) == 2


def test_bounce_stops_then_respawns_on_changed_entry(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)
    _run(hub.reconcile_all())

    config.mcp_entry_replace(
        config_path, "one", {"command": ["/bin/one-v2"]}, None
    )
    status = _run(hub.reconcile_alias("one"))

    assert status.state is AliasState.RUNNING
    transports = factory.for_alias("one")
    assert len(transports) == 2
    assert transports[0].closed
    assert factory.launches[-1].argv == ["/bin/one-v2"]


def test_forget_stops_and_drops_the_alias(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)
    _run(hub.reconcile_all())

    _run(hub.forget("one"))

    assert hub.runtime_states() == {}
    assert factory.for_alias("one")[0].closed


def test_disk_differs_reports_manual_yaml_edits(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    mcp_entry_add(config_path, "two", {"command": ["/bin/two"]}, None)
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)
    _run(hub.reconcile_all())
    assert hub.disk_differs() == []

    config.mcp_entry_set_enabled(config_path, "two", False)

    assert hub.disk_differs() == ["two"]


def test_provider_clients_expose_running_aliases_only(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    mcp_entry_add(config_path, "off", {"command": ["/bin/off"]}, None)
    mcp_entry_set_enabled(config_path, "off", False)
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)
    _run(hub.reconcile_all())

    clients = hub.provider_clients()

    assert set(clients) == {"one"}
    descriptor = _run(clients["one"].list_tools())[0]
    assert descriptor.provider_name == "one"
    assert descriptor.tool_name == "ping"


def test_alias_env_file_secrets_are_read_at_spawn_time(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    write_alias_env(config_path, "one", {"TOKEN_ENV": "later-value"})
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)

    _run(hub.reconcile_all())

    assert factory.launches[0].env.get("TOKEN_ENV") == "later-value"


def test_desired_state_validation_failure_is_structured(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    import yaml

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    document["mcp_servers"] = {
        "bad": {"command": ["/bin/a"], "url": "https://x.test/mcp"}
    }
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    if os.name != "nt":
        config_path.chmod(0o600)
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)

    with pytest.raises(HubError) as excinfo:
        _run(hub.desired_states())
    assert excinfo.value.code == "config_invalid"
    assert excinfo.value.details  # structured path/message details, no secrets


# --------------------------------------------------------------------------
# Catalog publication (hub → ClientCatalog; hub stays the transport owner)
# --------------------------------------------------------------------------


def test_publish_catalog_reflects_running_and_disabled_aliases(
    tmp_path: Path,
) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    mcp_entry_add(config_path, "off", {"command": ["/bin/off"]}, None)
    mcp_entry_set_enabled(config_path, "off", False)
    hub = _hub(config_path, workspace, FactoryRecorder())
    _run(hub.reconcile_all())
    catalog = ClientCatalog()

    hub.publish_catalog(catalog)

    one, off = catalog.records["one"], catalog.records["off"]
    assert one.catalog_available is True
    assert [d.name for d in one.exposed()] == ["ping"]
    assert off.catalog_available is False
    assert off.runtime_state == "disabled"
    assert off.error == {"code": "alias_disabled", "message": "the alias is disabled"}
    assert off.exposed() == ()


def test_publish_catalog_marks_spawn_failure_unavailable_with_safe_error(
    tmp_path: Path,
) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "broken", {"command": ["/bin/broken"]}, None)
    hub = _hub(config_path, workspace, FactoryRecorder(fail_aliases={"broken"}), attempts=3)
    _run(hub.reconcile_all())
    catalog = ClientCatalog()

    hub.publish_catalog(catalog)

    broken = catalog.records["broken"]
    assert broken.runtime_state == "unavailable"
    assert broken.catalog_available is False
    assert broken.error is not None
    assert broken.error["code"] in {"spawn_failed", "startup_budget_exhausted"}


def test_bounce_replaces_the_route(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)
    _run(hub.reconcile_all())
    catalog = ClientCatalog()
    hub.publish_catalog(catalog)
    old_provider = catalog.route("one", "ping")

    config.mcp_entry_replace(config_path, "one", {"command": ["/bin/one-v2"]}, None)
    _run(hub.reconcile_alias("one"))
    hub.publish_catalog(catalog)

    assert catalog.route("one", "ping") is not old_provider
    assert factory.for_alias("one")[0].closed


def test_remove_alias_drops_it_from_the_catalog(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)
    _run(hub.reconcile_all())
    catalog = ClientCatalog()
    hub.publish_catalog(catalog)

    _run(hub.forget("one"))
    hub.publish_catalog(catalog)

    assert dict(catalog.records) == {}


def test_publish_catalog_snapshot_is_pure_client_side_discovery(
    tmp_path: Path,
) -> None:
    """Discovery reads inventories through the provider, without spawning."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)
    _run(hub.reconcile_all())
    catalog = ClientCatalog()
    hub.publish_catalog(catalog)
    baseline = factory.for_alias("one")[0].calls

    catalog.route("one", "ping")  # route lookup: no spawn, no read
    catalog.build(max_bytes=1_000_000)

    assert len(factory.launches) == 1  # no implicit spawn
    assert factory.for_alias("one")[0].calls == baseline


def test_alias_record_shapes_the_catalog_entry(tmp_path: Path) -> None:
    """AliasCatalog is built from hub state: no invented metadata."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)
    _run(hub.reconcile_all())

    record = hub.alias_record("one")

    assert isinstance(record, AliasCatalog)
    assert record.alias == "one"
    assert record.enabled is True
    assert record.runtime_state == "running"
    assert record.transport == "stdio"
    assert record.catalog_available is True
    assert record.error is None
    assert [descriptor.name for descriptor in record.descriptors] == ["ping"]


# --------------------------------------------------------------------------
# Per-alias global startup budget (120 s in production; short
# injected budgets here — never a real 120 s wait in tests).
# --------------------------------------------------------------------------


class DelayedTransport(FakeTransport):
    """Synthetic transport whose first inventory waits an injected delay."""

    def __init__(self, *, delay: float = 0.0, fail: bool = False) -> None:
        super().__init__(fail=fail)
        self.delay = delay

    async def list_tools(self, cursor: str | None = None) -> object:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return await super().list_tools(cursor)


class BudgetFactory:
    """Transport factory handing out one scripted transport per attempt."""

    def __init__(self, transports: list[FakeTransport]) -> None:
        self._pending: list[FakeTransport] = list(transports)
        self.handed: list[FakeTransport] = []
        self.launches: list[AliasLaunch] = []

    def __call__(self, launch: AliasLaunch) -> FakeTransport:
        self.launches.append(launch)
        transport = self._pending.pop(0)
        self.handed.append(transport)
        return transport


def _budget_hub(
    config_path: Path,
    workspace: Path,
    factory: BudgetFactory,
    *,
    budget: float,
    attempts: int = 3,
) -> McpHub:
    return McpHub(
        config_path,
        workspace,
        transport_factory=factory,
        spawn_attempts=attempts,
        startup_budget_seconds=budget,
    )


def test_slow_startup_succeeds_within_budget_on_retry(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "slow", {"command": ["/bin/slow"]}, None)
    factory = BudgetFactory([FakeTransport(fail=True), DelayedTransport(delay=0.3)])
    hub = _budget_hub(config_path, workspace, factory, budget=2.0)

    statuses = _run(hub.reconcile_all())

    assert statuses["slow"].state is AliasState.RUNNING
    assert len(factory.launches) == 2


def test_startup_budget_is_shared_across_attempts_and_never_rearmed(
    tmp_path: Path,
) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "slow", {"command": ["/bin/slow"]}, None)
    factory = BudgetFactory(
        [
            DelayedTransport(delay=0.4, fail=True),
            DelayedTransport(delay=0.4, fail=True),
            DelayedTransport(delay=0.5, fail=True),
        ]
    )
    hub = _budget_hub(config_path, workspace, factory, budget=1.0)

    statuses = _run(hub.reconcile_all())

    assert statuses["slow"].state is AliasState.UNAVAILABLE
    assert len(factory.launches) == 3
    error = statuses["slow"].last_error
    assert error is not None
    assert error["code"] == "startup_budget_exhausted"
    assert "budget" in error["message"]
    # Invariant: the YAML keeps the operator's intent, unchanged.
    entries = config.mcp_entries(config_path)
    assert entries["slow"] == {"command": ["/bin/slow"], "enabled": True}


def test_budget_exhausted_alias_does_not_respawn_without_explicit_operation(
    tmp_path: Path,
) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "slow", {"command": ["/bin/slow"]}, None)
    factory = BudgetFactory(
        [DelayedTransport(delay=0.3, fail=True) for _ in range(6)]
    )
    hub = _budget_hub(config_path, workspace, factory, budget=1.0)

    _run(hub.reconcile_all())
    first_count = len(factory.launches)
    assert first_count == 3
    assert hub.state_of("slow") is AliasState.UNAVAILABLE

    # Reconciling an unchanged configuration must never re-spawn.
    _run(hub.reconcile_alias("slow"))
    _run(hub.reconcile_all())
    assert len(factory.launches) == first_count
    assert hub.state_of("slow") is AliasState.UNAVAILABLE


def test_quick_failures_exhaust_attempts_with_spawn_failed_then_stay_down(
    tmp_path: Path,
) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "broken", {"command": ["/bin/broken"]}, None)
    factory = BudgetFactory([FakeTransport(fail=True) for _ in range(6)])
    hub = _budget_hub(config_path, workspace, factory, budget=1.0)

    statuses = _run(hub.reconcile_all())

    assert statuses["broken"].state is AliasState.UNAVAILABLE
    assert len(factory.launches) == 3
    assert statuses["broken"].last_error is not None
    assert statuses["broken"].last_error["code"] == "spawn_failed"
    _run(hub.reconcile_alias("broken"))
    assert len(factory.launches) == 3


def test_explicit_config_change_spawns_again_after_budget_exhaustion(
    tmp_path: Path,
) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "slow", {"command": ["/bin/slow-v1"]}, None)
    factory = BudgetFactory(
        [DelayedTransport(delay=0.4, fail=True) for _ in range(4)]
    )
    hub = _budget_hub(config_path, workspace, factory, budget=1.0)

    _run(hub.reconcile_all())
    assert hub.state_of("slow") is AliasState.UNAVAILABLE
    first_count = len(factory.launches)

    mcp_entry_replace(config_path, "slow", {"command": ["/bin/slow-v2"]}, None)
    factory._pending = [DelayedTransport(delay=0.0)]
    statuses = _run(hub.reconcile_all())

    assert len(factory.launches) > first_count
    assert statuses["slow"].state is AliasState.RUNNING


def test_cancelled_slow_startup_cleans_up_the_transport(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "slow", {"command": ["/bin/slow"]}, None)
    factory = BudgetFactory([DelayedTransport(delay=5.0)])
    hub = _budget_hub(config_path, workspace, factory, budget=10.0)

    async def scenario() -> None:
        task = asyncio.create_task(hub.reconcile_alias("slow"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(scenario())

    assert factory.handed[0].closed is True
    assert hub.state_of("slow") is AliasState.UNAVAILABLE
    error = hub.last_error("slow")
    assert error is not None and error["code"] == "spawn_cancelled"


def test_running_provider_keeps_the_ordinary_call_timeout(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "slow", {"command": ["/bin/slow"]}, None)
    factory = BudgetFactory([DelayedTransport(delay=0.0)])
    hub = _budget_hub(config_path, workspace, factory, budget=2.0)

    _run(hub.reconcile_all())

    provider = hub.provider_clients()["slow"]
    assert isinstance(provider, McpProviderToolClient)
    assert provider._timeout_seconds == DEFAULT_PROVIDER_TIMEOUT_SECONDS
    assert provider._timeout_seconds < 120.0


# --------------------------------------------------------------------------
# Decoupled startup. The control-channel connection and heartbeat
# start independently of the (possibly long) initial MCP reconciliation,
# STARTING is observable, and the catalog is re-published on every state
# change. Short injected delays here — never a real 120 s wait in tests.
# --------------------------------------------------------------------------


def test_other_alias_stays_invocable_during_slow_startup(tmp_path: Path) -> None:
    """While one alias is STARTING, running aliases remain invocable."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "aaa", {"command": ["/bin/fast"]}, None)
    mcp_entry_add(config_path, "zzz", {"command": ["/bin/slow"]}, None)

    transports = {"aaa": FakeTransport(), "zzz": DelayedTransport(delay=0.5)}

    def factory(launch: AliasLaunch) -> FakeTransport:
        return transports[launch.alias]

    hub = McpHub(config_path, workspace, transport_factory=factory)

    async def scenario() -> None:
        task = asyncio.create_task(hub.reconcile_all())
        for _ in range(200):
            if hub.state_of("zzz") is AliasState.STARTING:
                break
            await asyncio.sleep(0.01)
        assert hub.state_of("zzz") is AliasState.STARTING
        # The already-running alias is invocable during the other's startup.
        providers = hub.provider_clients()
        assert set(providers) == {"aaa"}
        result = await providers["aaa"].call_tool("ping", {})
        assert result is not None
        await asyncio.wait_for(task, timeout=5)
        assert hub.state_of("zzz") is AliasState.RUNNING

    _run(scenario())


def test_catalog_publication_observes_starting_then_terminal_states(
    tmp_path: Path,
) -> None:
    """STARTING is observable, and every change re-publishes the catalog."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "good", {"command": ["/bin/good"]}, None)
    mcp_entry_add(config_path, "bad", {"command": ["/bin/bad"]}, None)

    transports: dict[str, FakeTransport] = {
        "good": DelayedTransport(delay=0.2),
        "bad": DelayedTransport(delay=0.1, fail=True),
    }

    def factory(launch: AliasLaunch) -> FakeTransport:
        return transports[launch.alias]

    hub = McpHub(config_path, workspace, transport_factory=factory, spawn_attempts=1)
    catalog = ClientCatalog()

    states: dict[str, list[str]] = {"good": [], "bad": []}

    def observe() -> None:
        hub.publish_catalog(catalog)
        for alias in states:
            record = catalog._records.get(alias)
            if record is not None:
                state = record.runtime_state
                if not states[alias] or states[alias][-1] != state:
                    states[alias].append(state)

    hub.bind_on_change(observe)

    statuses = _run(hub.reconcile_all())

    assert statuses["good"].state is AliasState.RUNNING
    assert statuses["bad"].state is AliasState.UNAVAILABLE
    # STARTING was observable in the published catalog, then each alias
    # reached its terminal published state.
    assert states["good"][0] == "starting"
    assert states["good"][-1] == "running"
    assert states["bad"][0] == "starting"
    assert states["bad"][-1] == "unavailable"
    good = catalog.records["good"]
    assert good.runtime_state == "running"
    assert good.catalog_available is True
    bad = catalog.records["bad"]
    assert bad.runtime_state == "unavailable"
    assert bad.catalog_available is False


def test_starting_alias_record_reports_starting_not_spawn_failed(
    tmp_path: Path,
) -> None:
    """A STARTING alias is honestly reported as starting, never as failed."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "slow", {"command": ["/bin/slow"]}, None)
    factory = BudgetFactory([DelayedTransport(delay=0.5)])
    hub = _budget_hub(config_path, workspace, factory, budget=5.0)

    async def scenario() -> None:
        task = asyncio.create_task(hub.reconcile_alias("slow"))
        for _ in range(200):
            if hub.state_of("slow") is AliasState.STARTING:
                break
            await asyncio.sleep(0.01)
        record = hub.alias_record("slow")
        assert record is not None
        assert record.runtime_state == "starting"
        assert record.enabled is True
        assert record.catalog_available is False
        assert record.error is not None
        assert record.error["code"] == "alias_starting"
        await asyncio.wait_for(task, timeout=5)

    _run(scenario())


def test_running_alias_with_invalidated_inventory_publishes_unavailable(
    tmp_path: Path,
) -> None:
    """Between an upstream change and a good re-read, the alias publishes
    nothing rather than a stale list; other aliases stay available."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    mcp_entry_add(config_path, "two", {"command": ["/bin/two"]}, None)
    hub = _hub(config_path, workspace, FactoryRecorder())
    _run(hub.reconcile_all())
    catalog = ClientCatalog()

    provider = hub.provider_clients()["one"]
    provider.invalidate_inventory()
    hub.publish_catalog(catalog)

    record = catalog.records["one"]
    assert record.runtime_state == "running"
    assert record.catalog_available is False
    assert record.error is not None and record.error["code"] == "inventory_stale"
    assert catalog.records["two"].catalog_available is True

    _run(provider.list_tools())
    hub.publish_catalog(catalog)
    assert catalog.records["one"].catalog_available is True
    assert catalog.records["one"].error is None


def test_upstream_tools_changed_triggers_one_refresh_and_change_notices(
    tmp_path: Path,
) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    factory = FactoryRecorder()
    hub = _hub(config_path, workspace, factory)
    _run(hub.reconcile_all())
    changes: list[str] = []
    hub.bind_on_change(lambda: changes.append(hub.alias_record("one").error and "stale" or "fresh"))
    transport = factory.for_alias("one")[0]
    reads = transport.calls

    async def notify() -> None:
        await transport.on_tools_changed()
        await transport.on_tools_changed()  # coalesced with the first
        for _ in range(100):
            if changes and changes[-1] == "fresh":
                return
            await asyncio.sleep(0.01)

    _run(notify())
    assert changes[0] == "stale" and changes[-1] == "fresh"
    assert transport.calls == reads + 1


def test_a_dead_provider_makes_its_alias_unavailable(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_entry_add(config_path, "one", {"command": ["/bin/one"]}, None)
    hub = _hub(config_path, workspace, FactoryRecorder())
    _run(hub.reconcile_all())
    changes: list[str] = []
    hub.bind_on_change(lambda: changes.append(hub.state_of("one").value))

    async def die() -> None:
        hub.provider_clients()["one"]._mark_unavailable()
        for _ in range(100):
            if changes:
                return
            await asyncio.sleep(0.01)

    _run(die())
    assert hub.state_of("one") is AliasState.UNAVAILABLE
    assert hub.alias_record("one").error["code"] == "alias_unavailable"
    _run(hub.aclose())
