"""Per-alias reconcile engine for the Relay Client's local MCP server hub.

Desired state lives in the YAML (``client.mcp_servers``); runtime state lives
here, in memory, one isolated failure domain per alias. The engine diff diff
desired versus runtime and spawns, stops, or bounces only the affected alias:
three spawn attempts, then the alias reports ``unavailable`` while the YAML
keeps the operator's intent (no automatic configuration mutation). Alias
credential material lives in the alias private ``.env`` and is read at spawn
time only; it never enters the YAML and never enters tool results.
"""

from __future__ import annotations

import asyncio
import enum
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    ConfigError,
    McpServerEntry,
    alias_cache_dir,
    mcp_entries,
    read_alias_env,
)
from .diagnostics import debug as _debug_log
from .mcp_catalog import AliasCatalog, ClientCatalog
from .mcp_registry import RegistryUnreachableError
from .providers.base import (
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ProviderToolClient,
    bounded_error_detail,
    exception_type_chain,
)
from .providers.mcp_client import McpProviderToolClient


class AliasState(str, enum.Enum):
    """Runtime state of one alias; ``disabled`` aliases are skipped at startup."""

    RUNNING = "running"
    STARTING = "starting"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


HUB_SPAWN_ATTEMPTS = 3
#: Step 7A: global startup budget per alias, covering source resolution,
#: transport open + initialize, and the first inventory. A code constant by
#: design — there is deliberately no YAML or environment knob for it. The
#: budget is shared across spawn attempts and never rearmed per attempt.
HUB_STARTUP_BUDGET_SECONDS = 120.0


class HubError(Exception):
    """A structured, user-safe hub failure: {code, message, suggested_action?}."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        suggested_action: str | None = None,
        details: tuple[dict[str, str], ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggested_action = suggested_action
        self.details = details

    def to_payload(self) -> dict[str, str]:
        payload = {"code": self.code, "message": self.message}
        if self.suggested_action is not None:
            payload["suggested_action"] = self.suggested_action
        return payload


@dataclass(frozen=True)
class AliasLaunch:
    """The fully resolved spawn request handed to the transport factory."""

    alias: str
    transport: str
    argv: list[str] | None
    url: str | None
    env: dict[str, str]
    cwd: Path | None


@dataclass
class _AliasRuntime:
    state: AliasState = AliasState.DISABLED
    transport: Any = None
    provider: McpProviderToolClient | None = None
    last_error: dict[str, str] | None = None
    applied: dict[str, Any] | None = None


TransportFactory = Callable[[AliasLaunch], Any]
#: Resolve one registry source to its record at spawn time; ``None`` when the
#: registry knows no such source. Reachability failures raise
#: ``RegistryUnreachableError``.
SourceResolver = Callable[[str, str | None], Awaitable[Any]]

_LAUNCHER_CACHE_VARIABLES = {
    "npx": "NPM_CONFIG_CACHE",
    "uvx": "UV_CACHE_DIR",
}


def _alias_transport(run: "_AliasRuntime") -> str:
    """Best-effort transport label for catalog display metadata."""
    applied_transport = (run.applied or {}).get("transport") if run.applied else None
    if isinstance(applied_transport, str) and applied_transport:
        return applied_transport
    return "stdio"


def _safe_exception_chain(error: BaseException) -> str:
    return " <- ".join(exception_type_chain(error))


def default_source_resolver(
    *,
    base_url: str,
    timeout_seconds: float,
    transport: Any = None,
) -> SourceResolver:
    """Build the production resolver on the bounded official-registry client."""

    from .mcp_registry import lookup_registry_server

    async def resolve(source: str, version: str | None) -> Any:
        return await lookup_registry_server(
            source,
            version=version,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )

    return resolve


def launcher_argv(summary: Any, version: str | None) -> list[str]:
    """Derive the declarative launcher argv from one registry record."""
    from .mcp_registry import RegistryServerSummary, declarative_launcher

    if not isinstance(summary, RegistryServerSummary):
        summary = RegistryServerSummary.model_validate(summary)
    for package in summary.packages:
        argv = declarative_launcher(package)
        if argv is not None:
            if version is not None and len(argv) > 1 and argv[-1].endswith(
                f"@{package.version}"
            ):
                argv[-1] = f"{package.identifier}@{version}"
            return argv
    raise HubError(
        "transport_unsupported",
        "registry record declares no supported declarative launcher",
        suggested_action="fix_config",
    )


class McpHub:
    """Own alias runtime state; reconciles one alias at a time, in isolation."""

    def __init__(
        self,
        config_path: str | Path | None,
        workspace: Path,
        *,
        transport_factory: TransportFactory,
        source_resolver: SourceResolver | None = None,
        spawn_attempts: int = HUB_SPAWN_ATTEMPTS,
        startup_budget_seconds: float = HUB_STARTUP_BUDGET_SECONDS,
        provider_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.config_path: str | Path | None = config_path
        self._workspace = workspace
        self._transport_factory = transport_factory
        self._source_resolver = source_resolver
        self._spawn_attempts = max(1, spawn_attempts)
        self._startup_budget_seconds = startup_budget_seconds
        self._provider_timeout_seconds = provider_timeout_seconds
        self._monotonic = monotonic or time.monotonic
        self._runtimes: dict[str, _AliasRuntime] = {}
        # Step 7B: change observer (wired by the client to catalog
        # publication). Invoked synchronously after every runtime state
        # change so STARTING and terminal states become observable while a
        # startup is still in flight; a failing observer never fails the
        # reconcile path.
        self._on_change: Callable[[], None] | None = None

    def bind_on_change(self, callback: Callable[[], None] | None) -> None:
        """Attach the change observer (wired by the Relay Client)."""
        self._on_change = callback

    def _note_change(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:
            _debug_log("hub on_change observer failed")

    # ------------------------------------------------------------------
    # Desired state (YAML) — validated, fail-closed, secrets excluded
    # ------------------------------------------------------------------

    def desired_states(self) -> dict[str, McpServerEntry]:
        try:
            raw = mcp_entries(self.config_path)
        except ConfigError as exc:
            raise HubError(
                "config_invalid",
                "client configuration is invalid",
                suggested_action="fix_config",
            ) from exc
        validated: dict[str, McpServerEntry] = {}
        details: list[dict[str, str]] = []
        for alias, entry in raw.items():
            if not isinstance(entry, Mapping):
                details.append(
                    {"alias": str(alias), "message": "mcp_servers entry is invalid"}
                )
                continue
            try:
                validated[str(alias)] = McpServerEntry.model_validate(dict(entry))
            except Exception:
                details.append(
                    {"alias": str(alias), "message": "mcp_servers entry is invalid"}
                )
        if details:
            raise HubError(
                "config_invalid",
                "client configuration is invalid",
                suggested_action="fix_config",
                details=tuple(details),
            )
        return validated

    # ------------------------------------------------------------------
    # Runtime state
    # ------------------------------------------------------------------

    def runtime_states(self) -> dict[str, AliasState]:
        return {alias: run.state for alias, run in self._runtimes.items()}

    def last_error(self, alias: str) -> dict[str, str] | None:
        run = self._runtimes.get(alias)
        return None if run is None else run.last_error

    def state_of(self, alias: str) -> AliasState:
        run = self._runtimes.get(alias)
        return AliasState.DISABLED if run is None else run.state

    def disk_differs(self) -> list[str]:
        """Aliases whose on-disk YAML entry differs from the applied runtime."""
        differing: list[str] = []
        try:
            desired = self.desired_states()
        except HubError:
            return sorted(self._runtimes)
        for alias in sorted(set(self._runtimes) | set(desired)):
            run = self._runtimes.get(alias)
            applied = None if run is None else run.applied
            entry = desired.get(alias)
            current = entry.yaml_value() if entry is not None else None
            if applied != current:
                differing.append(alias)
        return differing

    def provider_clients(self) -> dict[str, ProviderToolClient]:
        """Running aliases as bounded provider clients keyed by alias."""
        return {
            alias: run.provider
            for alias, run in sorted(self._runtimes.items())
            if run.provider is not None and run.state is AliasState.RUNNING
        }

    # ------------------------------------------------------------------
    # Catalog publication (the hub stays the sole transport owner)
    # ------------------------------------------------------------------

    def alias_record(self, alias: str) -> AliasCatalog | None:
        """Shape one alias's current state into a catalog record, or None.

        A running alias publishes its live inventory and route reference; a
        disabled or unavailable alias publishes ``catalog_available: false``
        with the hub's safe error. This method performs no discovery and no
        spawn: it reflects state the hub already owns.
        """
        run = self._runtimes.get(alias)
        if run is None:
            return None
        # A RUNNING alias is catalog-available only with a fresh inventory:
        # an invalidated (or never-read) cache would otherwise publish a
        # misleading empty tool list. The process state stays RUNNING; the
        # catalog record honestly refuses discovery until a bounded reread.
        provider = run.provider
        cached = None if provider is None else provider.cached_inventory()
        inventory_ready = (
            provider is not None
            and bool(provider.inventory_valid)
            and cached is not None
        )
        available = run.state is AliasState.RUNNING and inventory_ready
        descriptors: tuple[Any, ...] = ()
        if available:
            assert provider is not None
            descriptors = tuple(cached or ())
        if available:
            discovery_error: dict[str, str] | None = None
        elif run.state is AliasState.DISABLED:
            discovery_error = {
                "code": "alias_disabled",
                "message": "the alias is disabled",
            }
        elif run.state is AliasState.STARTING:
            # Step 7B: an in-flight startup is honestly reported as
            # starting, never as an already-failed spawn.
            discovery_error = {
                "code": "alias_starting",
                "message": "the local MCP server is starting",
            }
        elif run.state is AliasState.RUNNING:
            discovery_error = {
                "code": "inventory_stale",
                "message": "the tool inventory is not currently executable",
            }
        else:
            discovery_error = run.last_error or {
                "code": "spawn_failed",
                "message": "the local MCP server could not be started",
            }
        return AliasCatalog(
            alias=alias,
            # A STARTING alias was committed (enabled in YAML or by an admin
            # verb) even though the spawn has not landed yet.
            enabled=(
                run.applied is not None
                or run.state in (AliasState.RUNNING, AliasState.STARTING)
            ),
            runtime_state=run.state.value,
            transport=_alias_transport(run),
            entry=dict(run.applied or {}),
            last_error=run.last_error,
            catalog_available=available,
            discovery_error=discovery_error,
            descriptors=descriptors,
            provider=run.provider if available else None,
            env_keys=tuple(sorted(read_alias_env(self.config_path, alias))),
        )

    def publish_catalog(self, catalog: ClientCatalog) -> None:
        """Push the current alias set into the Client catalog.

        Re-publication is idempotent: the catalog keeps its revision unless
        an executable snapshot actually changed. Aliases removed from the
        runtime disappear from the catalog. No transport lifecycle happens
        here — spawns, stops and bounces remain reconcile-only.
        """
        current = self._runtimes.keys()
        for alias in sorted(current):
            record = self.alias_record(alias)
            if record is not None:
                catalog.update_alias(record)
        for alias in sorted(set(catalog.snapshot._records) - set(current)):
            catalog.remove_alias(alias)

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    async def reconcile_all(self) -> dict[str, "_Status"]:
        desired = self.desired_states()
        for alias in sorted(set(self._runtimes) - set(desired)):
            await self.forget(alias)
        statuses: dict[str, _Status] = {}
        for alias in sorted(desired):
            statuses[alias] = await self.reconcile_alias(alias)
        return statuses

    async def reconcile_alias(self, alias: str) -> _Status:
        desired = self.desired_states()
        entry = desired.get(alias)
        run = self._runtimes.get(alias)
        if entry is None:
            if run is not None:
                await self.forget(alias)
            return _Status(self, alias)
        if not entry.enabled:
            await self._stop(alias)
            run = self._runtimes.setdefault(alias, _AliasRuntime())
            run.state = AliasState.DISABLED
            run.last_error = None
            run.applied = None
            self._note_change()
            return _Status(self, alias)
        desired_value = entry.yaml_value()
        if (
            run is not None
            and run.state in (AliasState.RUNNING, AliasState.UNAVAILABLE)
            and run.applied == desired_value
        ):
            # A running alias is a no-op; an alias whose startup budget was
            # exhausted (or whose attempts failed with the applied config
            # unchanged) stays down: only an explicit operator action — a
            # changed entry (bounce) or a disable/enable cycle — respawns it.
            return _Status(self, alias)
        await self._stop(alias)
        status = await self._spawn(alias, entry)
        return status

    async def forget(self, alias: str) -> None:
        """Stop one alias and drop its runtime state entirely."""
        await self._stop(alias)
        self._runtimes.pop(alias, None)
        self._note_change()

    # ------------------------------------------------------------------
    # Spawn / stop internals
    # ------------------------------------------------------------------

    async def _stop(self, alias: str) -> None:
        run = self._runtimes.get(alias)
        if run is None:
            return
        transport, run.transport = run.transport, None
        run.provider = None
        if transport is not None:
            await self._close_quietly(transport, alias=alias)

    @staticmethod
    async def _close_quietly(transport: Any, *, alias: str) -> None:
        close = getattr(transport, "close", None)
        if not callable(close):
            return
        try:
            await asyncio.wait_for(close(), timeout=5.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            _debug_log(f"hub alias close failed: alias={alias}")

    async def _spawn(self, alias: str, entry: McpServerEntry) -> _Status:
        run = self._runtimes.setdefault(alias, _AliasRuntime())
        run.state = AliasState.STARTING
        run.last_error = None
        # Step 7B: STARTING is published immediately, so the catalog shows
        # the in-flight startup before the first spawn attempt lands.
        self._note_change()
        # Step 7A: one global startup budget per alias, shared by source
        # resolution and every spawn attempt; never rearmed per attempt.
        deadline = self._monotonic() + self._startup_budget_seconds
        try:
            launch = await self._bounded_build_launch(alias, entry, deadline)
        except asyncio.CancelledError:
            run.state = AliasState.UNAVAILABLE
            # A cancellation does not record the config as an applied attempt;
            # a later reconciliation may retry it.
            run.applied = None
            run.last_error = HubError(
                "spawn_cancelled", "local MCP server startup was cancelled"
            ).to_payload()
            self._note_change()
            raise
        except asyncio.TimeoutError:
            run.state = AliasState.UNAVAILABLE
            run.last_error = self._budget_exhausted_error()
            run.applied = entry.yaml_value()
            self._note_change()
            return _Status(self, alias)
        except HubError as error:
            run.state = AliasState.UNAVAILABLE
            run.last_error = error.to_payload()
            run.applied = None
            self._note_change()
            return _Status(self, alias)
        cache_dir = self._cache_dir(alias)
        if entry.source is not None:
            cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._harden_dir(cache_dir)
        last_error: HubError | None = None
        budget_exhausted = False
        for attempt in range(1, self._spawn_attempts + 1):
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                budget_exhausted = True
                break
            transport: Any = None
            try:
                transport = self._transport_factory(
                    AliasLaunch(
                        alias=alias,
                        transport=entry.transport,
                        argv=launch,
                        url=entry.url,
                        env=self._spawn_env(alias, launch),
                        cwd=cache_dir if entry.source is not None else None,
                    )
                )
                provider = McpProviderToolClient(
                    transport,
                    provider_name=alias,
                    timeout_seconds=self._provider_timeout_seconds,
                )
                provider.bind_transport_notifications()
                # Open + initialize + first inventory run against the shared
                # startup budget, not a per-attempt timeout; the ordinary
                # per-call provider timeout (30 s) stays unchanged.
                await provider.list_tools(timeout_seconds=remaining)
            except asyncio.CancelledError:
                run.state = AliasState.UNAVAILABLE
                # A cancellation does not record the config as an applied
                # attempt; a later reconciliation may retry it.
                run.applied = None
                run.last_error = HubError(
                    "spawn_cancelled", "local MCP server startup was cancelled"
                ).to_payload()
                if transport is not None:
                    await self._close_quietly(transport, alias=alias)
                self._note_change()
                raise
            except Exception as error:
                _debug_log(
                    "hub alias spawn failed: "
                    f"alias={alias} attempt={attempt}/{self._spawn_attempts} "
                    f"chain={_safe_exception_chain(error)} "
                    f"detail={bounded_error_detail(error)}"
                )
                last_error = HubError(
                    "spawn_failed",
                    "the local MCP server could not be started",
                )
                if transport is not None:
                    await self._close_quietly(transport, alias=alias)
                if self._monotonic() >= deadline:
                    budget_exhausted = True
                    break
                continue
            run.transport = transport
            run.provider = provider
            run.state = AliasState.RUNNING
            run.last_error = None
            run.applied = entry.yaml_value()
            self._note_change()
            return _Status(self, alias)
        run.state = AliasState.UNAVAILABLE
        run.transport = None
        run.provider = None
        if budget_exhausted or last_error is None:
            run.last_error = self._budget_exhausted_error()
        else:
            run.last_error = last_error.to_payload()
        # The applied configuration is recorded so reconcile knows the alias
        # is intentionally down: no automatic re-spawn without an explicit
        # reload (changed entry) or enable operation.
        run.applied = entry.yaml_value()
        self._note_change()
        return _Status(self, alias)

    @staticmethod
    def _budget_exhausted_error() -> dict[str, str]:
        return HubError(
            "startup_budget_exhausted",
            "the local MCP server startup budget was exhausted",
        ).to_payload()

    async def _bounded_build_launch(
        self, alias: str, entry: McpServerEntry, deadline: float
    ) -> list[str] | None:
        """Resolve the launch inside the shared startup budget."""
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return await asyncio.wait_for(
            self._build_launch(alias, entry), timeout=remaining
        )

    async def _build_launch(self, alias: str, entry: McpServerEntry) -> list[str] | None:
        if entry.url is not None:
            return None
        if entry.command is not None:
            return list(entry.command)
        assert entry.source is not None
        if self._source_resolver is None:
            raise HubError(
                "registry_unreachable",
                "no registry resolver is configured",
                suggested_action="retry_later",
            )
        try:
            summary = await self._source_resolver(entry.source, entry.version)
        except (HubError, RegistryUnreachableError, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise RegistryUnreachableError(type(exc).__name__) from None
        if summary is None:
            raise HubError(
                "spawn_failed",
                "the registry knows no such source",
            )
        return launcher_argv(summary, entry.version)

    def _cache_dir(self, alias: str) -> Path:
        return alias_cache_dir(self.config_path, alias)

    @staticmethod
    def _harden_dir(path: Path) -> None:
        try:
            path.chmod(0o700)
        except OSError:
            pass

    def _spawn_env(self, alias: str, argv: list[str] | None) -> dict[str, str]:
        try:
            env = read_alias_env(self.config_path, alias)
        except ConfigError:
            env = {}
        if argv:
            variable = _LAUNCHER_CACHE_VARIABLES.get(argv[0])
            if variable is not None:
                env = {
                    **env,
                    variable: str(self._cache_dir(alias)),
                }
        return env


class _Status:
    """Lightweight per-alias status view bound to its hub runtime entry."""

    def __init__(self, hub: McpHub, alias: str) -> None:
        self._hub = hub
        self._alias = alias

    @property
    def state(self) -> AliasState:
        return self._hub.state_of(self._alias)

    @property
    def last_error(self) -> dict[str, str] | None:
        return self._hub.last_error(self._alias)

    @property
    def alias(self) -> str:
        return self._alias


def remove_alias_cache(config_path: str | Path | None, alias: str) -> None:
    """Best-effort removal of one alias launcher cache directory.

    The cache lives under the relay home (``<config dir>/mcp/<alias>``,
    by default ``~/.mcp-relay/mcp/<alias>``), never inside the workspace.
    """
    try:
        cache_dir = alias_cache_dir(config_path, alias)
    except ConfigError:
        return
    shutil.rmtree(cache_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# Production transports (FastMCP 4 clients; injectable in tests)
# --------------------------------------------------------------------------


class FastMcpClientTransport:
    """One alias connection owned by a ``fastmcp.Client`` (FastMCP 4).

    The library owns the session and transport mechanics — connect,
    initialize, the stdio subprocess, the Streamable HTTP session — through
    its public surface only (``StdioTransport`` / ``StreamableHttpTransport``,
    the async context-manager protocol, ``close()``). The relay adds no owner
    task and no session plumbing: it opens the client lazily and bounded,
    forwards raw MCP results without reinterpretation, and surfaces wire
    ``tools/list_changed`` notifications through ``on_tools_changed``.

    Close is terminal and idempotent; cancelling a close waiter does not
    abandon the shutdown it started (the cleanup task is shielded), and a
    later close observes completion.
    """

    def __init__(self, launch: AliasLaunch, *, init_timeout_seconds: float) -> None:
        from fastmcp import Client
        from fastmcp.client.transports import (
            StdioTransport,
            StreamableHttpTransport,
        )

        if launch.transport == "streamable_http":
            if launch.url is None:
                raise ValueError("streamable_http transport requires a url")
            fastmcp_transport = StreamableHttpTransport(launch.url)
        else:
            if launch.argv is None or not launch.argv:
                raise ValueError("stdio transport requires a command argv")
            fastmcp_transport = StdioTransport(
                launch.argv[0],
                list(launch.argv[1:]),
                env=dict(launch.env) or None,
                cwd=str(launch.cwd) if launch.cwd is not None else None,
            )
        self._init_timeout_seconds = init_timeout_seconds
        #: Hook invoked when the server surfaces ``tools/list_changed``.
        #: The provider binds its ``invalidate_inventory`` here so an upstream
        #: change immediately marks the cached inventory non-executable.
        self.on_tools_changed: Callable[[], Awaitable[None]] | None = None
        self._open_lock = asyncio.Lock()
        self._opened = False
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._client = Client(
            fastmcp_transport,
            message_handler=self._handle_message,
            init_timeout=init_timeout_seconds,
            # The relay speaks the standard initialize handshake; skip the
            # auto-mode ``server/discover`` probe (raw and legacy servers
            # never answer it, costing the whole probe deadline).
            mode="legacy",
        )

    async def _handle_message(self, message: Any) -> None:
        """Surface the wire notifications the FastMCP session receives."""
        from mcp import types as mcp_types

        if isinstance(message, mcp_types.ToolListChangedNotification):
            hook = self.on_tools_changed
            if hook is not None:
                await hook()

    async def _ensure_client(self) -> Any:
        if self._closed:
            raise RuntimeError("MCP transport is closed")
        if not self._opened:
            async with self._open_lock:
                if self._closed:
                    raise RuntimeError("MCP transport is closed")
                if not self._opened:
                    await self._client.__aenter__()
                    self._opened = True
        return self._client

    async def list_tools(self, cursor: str | None = None) -> object:
        """One raw MCP ``tools/list`` page; cache bypass keeps rereads honest."""
        client = await self._ensure_client()
        return await client.list_tools_mcp(
            cursor=cursor or None, cache_mode="bypass"
        )

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> object:
        """The raw MCP ``CallToolResult``; error results are results, not raises."""
        client = await self._ensure_client()
        return await client.call_tool_mcp(name, dict(arguments))

    async def close(self) -> None:
        if self._shutdown_task is None:
            self._closed = True
            client = self._client
            self._shutdown_task = asyncio.create_task(self._shutdown(client))
        try:
            await asyncio.shield(self._shutdown_task)
        except asyncio.CancelledError:
            # The waiter may leave, but the shutdown task keeps running; a
            # later close awaits the same task to observe completion.
            raise

    async def _shutdown(self, client: Any) -> None:
        try:
            await client.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            _debug_log("fastmcp client close failed")


def production_transport_factory(
    launch: AliasLaunch, *, timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS
) -> Any:
    """Build the production transport for one resolved alias launch."""
    return FastMcpClientTransport(launch, init_timeout_seconds=timeout_seconds)
