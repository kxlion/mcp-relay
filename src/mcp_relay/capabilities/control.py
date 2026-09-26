"""Agent-facing control capability running inside the Relay Client.

These tools let a remote MCP agent manage the local MCP servers of this
Relay Client — without a terminal. The YAML write is the commit point of
every mutation (validate → write atomically → apply); a runtime failure
after a successful write never edits the YAML. The capability never stops,
restarts, or upgrades the Relay Client itself: it acts on child MCP servers
only. Mutations that change the public inventory trigger the client's
capability re-announcement through ``on_inventory_change``.

Step 7B: administration-triggered startups (mcp.add/modify/enable) run
inline within the calling verb. The ordinary invocation deadline is shorter
than the hub's 120 s startup budget: when it fires first, the call is
cancelled with the existing honest semantics (YAML commit kept, in-flight
transport closed, ``spawn_cancelled`` uncertainty reported through the
catalog, no fabricated success, no job system). Initial startup at client
boot is different: it runs decoupled in a background task owned by the
RelayClient, so a long alias startup never delays the control connection.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import (
    MAX_MCP_COMMAND_ITEMS,
    MCP_ENV_MAX_KEYS,
    ConfigError,
    McpServerEntry,
    mcp_entry_add,
    mcp_entry_remove,
    mcp_entry_replace,
    mcp_entry_set_enabled,
    read_alias_env,
)
from ..diagnostics import error as _error_log
from ..diagnostics import info as _info_log
from ..json_bounds import JsonObject
from ..mcp_catalog import CatalogError, resolve_selector
from ..mcp_command import CommandError
from ..mcp_hub import AliasState, HubError, McpHub, remove_alias_cache
from ..protocol import InvokeMessage, ToolName
from ..provider_tools import ProviderToolDescriptor

#: Wire names on the client boundary; the facade derives the public names
#: ``relay_client_*``, ``relay_mcp_*`` from the ``client.``/``mcp.`` providers.
CONTROL_TOOL_WIRE_NAMES = frozenset(
    {
        "client.status",
        "mcp.list",
        "mcp.add",
        "mcp.modify",
        "mcp.delete",
        "mcp.enable",
        "mcp.disable",
    }
)

_MAX_ALIAS_LENGTH = 16
_PROTOCOL_VERSION = 1

#: Verbs gated by ``client.admin``. Identification is an
#: explicit set (never a name prefix): discovery (``mcp.list``) and the two
#: status verbs are always available regardless of the setting.
_ADMIN_CONTROL_TOOLS: frozenset[str] = frozenset(
    {
        "mcp.add",
        "mcp.modify",
        "mcp.delete",
        "mcp.enable",
        "mcp.disable",
    }
)

# Bounded input schemas (closed objects; argument semantics stay here).
_EMPTY_OBJECT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
_ALIAS_SCHEMA: JsonObject = {
    # The closed alias rules (1-16 lowercase letters) stay in the capability
    # models: publishing a tighter string bound would make the argument
    # boundary reject with a generic error what the closed model reports as
    # the structured ``invalid_alias`` code. The shared JSON argument budget
    # still bounds the raw input.
    "type": "string",
    "minLength": 1,
}
_ENTRY_SCHEMA: JsonObject = {
    # Descriptor schemas are no longer rewritten by the relay: provider
    # schemas pass through as-is under transport bounds only, and the driver
    # is the sole validator of their semantics. The entry object is described
    # generically here (schema pass-through means even keys like ``command``
    # would round-trip); the exact closed field set (source | command |
    # url, optional version/env) is enforced by the closed Pydantic model in
    # the capability and mirrored in the YAML contract. String-length bounds
    # also stay with the closed model (url ≤ 2048, command item ≤ 512, source
    # ≤ 255, version ≤ 64, .env ≤ 4 KiB). Structural bounds remain
    # here and the whole argument object stays under the shared JSON budget.
    "type": "object",
    "minProperties": 1,
    "maxProperties": 8,
    "additionalProperties": {
        "anyOf": [
            {"type": "string", "minLength": 1},
            {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_MCP_COMMAND_ITEMS,
                "items": {"type": "string", "minLength": 1},
            },
            {
                "type": "object",
                "minProperties": 0,
                "maxProperties": MCP_ENV_MAX_KEYS,
                "additionalProperties": {"type": "string", "minLength": 1},
            },
            {"type": "boolean"},
        ]
    },
}


_LIST_SCHEMA: JsonObject = {
    # Closed selector: level resolution is servers → tools → tool. A bare
    # string bound on alias/tool stays with the closed models (invalid code,
    # not a generic boundary rejection); the JSON budget bounds the raw input.
    "type": "object",
    "properties": {
        "alias": {"type": "string", "minLength": 1},
        "tool": {"type": "string", "minLength": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "cursor": {"type": "string", "minLength": 1, "maxLength": 1024},
    },
    "additionalProperties": False,
}


def _closed_object(
    properties: dict[str, JsonObject], required: list[str]
) -> JsonObject:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_STATUS_DESCRIPTIONS: dict[str, str] = {
    "client.status": (
        "Report this Relay Client's runtime status (version, protocol, uptime, "
        "workspace) and which configured aliases differ from disk."
    ),
    "mcp.list": (
        "List the local MCP server hub: one entry per alias with its runtime "
        "state, transport, redacted entry, and last error."
    ),
    "mcp.add": (
        "Declare and start a new local MCP server alias. The YAML write is the "
        "commit; exactly one of source, command, or url is required."
    ),
    "mcp.modify": (
        "Full-replace an existing alias entry (same schema as add), then bounce "
        "that alias."
    ),
    "mcp.delete": (
        "Stop a local MCP server and remove its alias. Existence is strict."
    ),
    "mcp.enable": "Enable an alias (idempotent) and reconcile it to running.",
    "mcp.disable": "Disable an alias (idempotent); the entry is kept, the process stops.",
}


def _descriptor(provider_name: str, tool_name: str, schema: JsonObject) -> (
    ProviderToolDescriptor
):
    wire_name = f"{provider_name}.{tool_name}"
    return ProviderToolDescriptor(
        provider_name=provider_name,
        tool_name=tool_name,
        description=_STATUS_DESCRIPTIONS[wire_name],
        input_schema=schema,
    )


class _ControlModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ListInput(_ControlModel):
    """Closed ``mcp.list`` selector: servers → tools → tool levels."""

    alias: str | None = Field(default=None, min_length=1, max_length=_MAX_ALIAS_LENGTH)
    tool: str | None = Field(default=None, min_length=1, max_length=128)
    limit: int | None = Field(default=None, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)


class _AliasInput(_ControlModel):
    alias: str = Field(min_length=1, max_length=_MAX_ALIAS_LENGTH)


class _ControlCapabilityErrors:
    """Structured error payloads shared by every control tool."""

    @staticmethod
    def invalid_alias() -> dict[str, str]:
        return {
            "code": "invalid_alias",
            "message": "alias must be 1-16 lowercase letters",
            "suggested_action": "choose_other_alias",
        }

    @staticmethod
    def alias_conflict() -> dict[str, str]:
        return {
            "code": "alias_conflict",
            "message": "an MCP server alias already exists",
            "suggested_action": "choose_other_alias",
        }

    @staticmethod
    def alias_unknown() -> dict[str, str]:
        return {
            "code": "alias_unknown",
            "message": "no such MCP server alias",
        }

    @staticmethod
    def invalid_entry(message: str = "MCP server entry is invalid") -> dict[str, str]:
        return {"code": "invalid_entry", "message": message}

    @staticmethod
    def config_invalid() -> dict[str, str]:
        return {
            "code": "config_invalid",
            "message": "client configuration is invalid",
            "suggested_action": "fix_config",
        }


def _valid_alias(alias: str) -> bool:

    from ..config import MCP_ALIAS_PATTERN

    return bool(MCP_ALIAS_PATTERN.fullmatch(alias))


class ControlCapability:
    """LocalCapability exposing the control surface over the relayed channel."""

    def __init__(
        self,
        *,
        hub: McpHub,
        workspace: Path,
        client_version: str,
        protocol_version: int = _PROTOCOL_VERSION,
        on_inventory_change: Callable[[], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
        admin_enabled: bool = False,
    ) -> None:
        self.hub = hub
        self._workspace = workspace
        self._client_version = client_version
        self._protocol_version = protocol_version
        self._on_inventory_change = on_inventory_change
        self._monotonic = monotonic or time.monotonic
        self._started_at = self._monotonic()
        self._unavailable = asyncio.Event()
        self._closed = False
        # The single administration switch, read once at startup. It gates
        # only the admin verbs (mcp.add/modify/delete/enable/disable);
        # list/command and status stay available. The verbs can neither
        # change it nor grant themselves admin. It is fail-closed: only an
        # explicit ``client.admin: true`` in the YAML unlocks them.
        self._admin_enabled = admin_enabled
        # Catalog refresh hook (wired by the Relay Client): invoked after any
        # effective mutation.
        self._on_catalog_refresh: Callable[[], None] | None = None
        # The client catalog (optional): revision and hub counters in status.
        self._catalog: Any = None
        # Plain attribute (not a property) so the instance satisfies the
        # LocalCapability protocol's invariant ``tools`` member.
        self.tools: frozenset[ToolName] = CONTROL_TOOL_WIRE_NAMES

    def bind_inventory_change(
        self, callback: Callable[[], Awaitable[None]] | None
    ) -> None:
        """Attach the re-announcement callback (wired by the Relay Client)."""
        self._on_inventory_change = callback

    def bind_catalog_refresh(
        self, callback: Callable[[], None] | None
    ) -> None:
        """Attach the catalog refresh callback (wired by the Relay Client)."""
        self._on_catalog_refresh = callback

    def _catalog_refresh(self) -> None:
        if self._on_catalog_refresh is None:
            return
        try:
            self._on_catalog_refresh()
        except Exception:
            # A failed catalog refresh must not fail the committed mutation;
            # the next discovery republishes anyway.
            return

    def _permission_denied(self) -> dict[str, str]:
        return {
            "code": "permission_denied",
            "message": "administration is disabled on this client",
        }

    async def start(self) -> None:
        return None

    async def wait_unavailable(self) -> None:
        await self._unavailable.wait()

    async def aclose(self) -> None:
        self._closed = True
        self._unavailable.set()

    async def list_tools(self) -> list[ProviderToolDescriptor]:
        return [
            _descriptor("client", "status", _EMPTY_OBJECT_SCHEMA),
            _descriptor(
                "mcp",
                "list",
                _LIST_SCHEMA,
            ),
            _descriptor(
                "mcp",
                "add",
                _closed_object(
                    {"alias": _ALIAS_SCHEMA, "entry": _ENTRY_SCHEMA},
                    ["alias", "entry"],
                ),
            ),
            _descriptor(
                "mcp",
                "modify",
                _closed_object(
                    {"alias": _ALIAS_SCHEMA, "entry": _ENTRY_SCHEMA},
                    ["alias", "entry"],
                ),
            ),
            _descriptor(
                "mcp", "delete", _closed_object({"alias": _ALIAS_SCHEMA}, ["alias"])
            ),
            _descriptor(
                "mcp", "enable", _closed_object({"alias": _ALIAS_SCHEMA}, ["alias"])
            ),
            _descriptor(
                "mcp", "disable", _closed_object({"alias": _ALIAS_SCHEMA}, ["alias"])
            ),
        ]

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def invoke(self, message: InvokeMessage) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("control capability is closed")
        dispatch = {
            "client.status": self._status,
            "mcp.list": self._list,
            "mcp.add": self._add,
            "mcp.modify": self._modify,
            "mcp.delete": self._delete,
            "mcp.enable": self._enable,
            "mcp.disable": self._disable,
        }
        handler = dispatch.get(message.tool_name)
        if handler is None:
            return _ControlCapabilityErrors.invalid_entry(
                "unknown control tool"
            )
        if (
            message.tool_name in _ADMIN_CONTROL_TOOLS
            and not self._admin_enabled
        ):
            # The single admin switch gates exactly these verbs, checked
            # before any mutation. list/command/status stay available.
            self._log_admin_event(
                message, code="permission_denied", level="error"
            )
            return self._permission_denied()
        try:
            result = await handler(message.arguments)
        except HubError as error:
            # List/admin failures are raised, never returned: the Client
            # transports them as ClientError frames so the facade renders
            # an isError=true MCP result with the closed error object.
            self._log_admin_event(message, code=error.code, level="error")
            raise CommandError(
                error.code,
                error.message,
                execution_state="not_started",
            ) from None
        except ConfigError:
            self._log_admin_event(message, code="invalid_arguments", level="error")
            raise CommandError(
                "invalid_arguments",
                "client configuration is invalid",
                execution_state="not_started",
            ) from None
        except CommandError as error:
            # Discovery failures keep their closed catalog codes
            # (invalid_arguments, alias_unknown, tool_unknown,
            # alias_unavailable, invalid_cursor, catalog_stale,
            # result_too_large) and their not_started execution state.
            self._log_admin_event(message, code=error.code, level="error")
            raise
        self._log_admin_event(message, result=result)
        return result

    def _log_admin_event(
        self,
        message: InvokeMessage,
        *,
        result: dict[str, Any] | None = None,
        code: str | None = None,
        level: Literal["info", "error"] = "info",
    ) -> None:
        """One sanitized line per admin operation outcome.

        Only validated identifiers are ever emitted: the operation wire
        name, the request id, and the alias solely when it already passes
        the closed alias pattern. Never the entry, command, args, env, or
        any configuration URL. A returned structured refusal keeps its
        closed ``code``; a successful mutation reports ``result=ok``.
        """
        if message.tool_name not in _ADMIN_CONTROL_TOOLS:
            # Read-only discovery/status verbs are never admin events —
            # including when they fail with a shared CommandError path.
            return
        parts = [
            "mcp.admin:",
            f"operation={message.tool_name}",
            f"request_id={message.request_id}",
        ]
        arguments = message.arguments
        alias = arguments.get("alias") if isinstance(arguments, dict) else None
        if isinstance(alias, str) and _valid_alias(alias):
            parts.append(f"alias={alias}")
        if code is None and isinstance(result, dict):
            returned = result.get("code")
            if isinstance(returned, str):
                code = returned
        if code is not None:
            parts.append(f"code={code}")
            level = "error"
        else:
            parts.append("result=ok")
        line = " ".join(parts)
        if level == "error":
            _error_log(line)
        else:
            _info_log(line)

    # ------------------------------------------------------------------
    # Read-only tools
    # ------------------------------------------------------------------

    async def _status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _ControlModel.model_validate(arguments)
        return {
            "client": {
                "version": self._client_version,
                "protocol": self._protocol_version,
                "uptime_s": max(0, int(self._monotonic() - self._started_at)),
                "workspace": str(self._workspace),
            },
            "disk_differs": self.hub.disk_differs(),
            # Observable without exposing any schema content: the single
            # administration switch and the catalog revision (opaque).
            "admin": self._admin_enabled,
            "catalog_revision": (
                self._catalog.revision if self._catalog is not None else None
            ),
            # Third-party hub counters, computed from the catalog snapshot
            # without triggering any refresh.
            "hub": self._hub_counters(),
        }

    def bind_catalog(self, catalog: Any) -> None:
        """Attach the client catalog for revision and counter reporting."""
        self._catalog = catalog

    def _hub_counters(self) -> dict[str, int]:
        # Third-party counters only: the seven internal control tools are
        # never a server, and the Server's own status stays local to it.
        # ``configured_aliases`` counts the YAML-configured third-party
        # servers (the desired state), independent of any stale runtime
        # record; ``running_aliases`` and ``available_tools`` follow the
        # retained contract: only aliases whose catalog record is actually
        # executable (fresh inventory) count, never stale RUNNING records
        # with an invalidated inventory. No refresh and no remote call here.
        try:
            configured = len(self.hub.desired_states())
        except HubError:
            # Status stays available, but the counter falls back to the
            # known snapshot, which may differ from disk; do not present it
            # as a fresh successful read of the configuration.
            records_anyway = (
                () if self._catalog is None else self._catalog.snapshot._records
            )
            configured = len(records_anyway)
        if self._catalog is None:
            return {
                "configured_aliases": configured,
                "running_aliases": 0,
                "available_tools": 0,
            }
        records = self._catalog.snapshot._records
        available = [
            record for record in records.values() if record.catalog_available
        ]
        return {
            "configured_aliases": configured,
            "running_aliases": len(available),
            "available_tools": sum(
                len(record.descriptors) for record in available
            ),
        }

    async def _list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed = _ListInput.model_validate(arguments)
        except ValidationError as error:
            raise CommandError(
                "invalid_arguments",
                "mcp.list arguments are invalid",
                execution_state="not_started",
            ) from error
        return await self._paginated_list(
            alias=parsed.alias,
            tool=parsed.tool,
            limit=parsed.limit,
            cursor=parsed.cursor,
        )

    async def _paginated_list(
        self,
        *,
        alias: str | None,
        tool: str | None,
        limit: int | None,
        cursor: str | None,
    ) -> dict[str, Any]:
        """The fixed-facade discovery contract, served from the client catalog.

        Validation and selector checks follow ``validate_selector``;
        first pages re-read the target inventories (bounded, coalesced by the
        catalog refresh), cursor pages continue the existing snapshot;
        failures raise ``CommandError`` with the closed discovery codes.
        """
        if self._catalog is None:
            raise CommandError(
                "invalid_arguments",
                "no client catalog is available",
                execution_state="not_started",
            )
        try:
            selector = resolve_selector(
                alias=alias, tool=tool, limit=limit, cursor=cursor
            )
            offset = 0
            if cursor is not None:
                offset = self._catalog.cursor_codec.decode(
                    cursor,
                    current_revision=self._catalog.revision,
                    level=selector.level,
                    alias=selector.alias or "",
                )
            elif selector.level == "servers":
                self._catalog_refresh()
            else:
                await self._refresh_alias_inventory(alias)
        except CatalogError as error:
            raise CommandError(
                error.code, error.message, execution_state="not_started"
            ) from None
        snapshot = self._catalog.snapshot
        revision = self._catalog.revision
        codec = self._catalog.cursor_codec

        def _cursor(level: str, page_alias: str, page: Any) -> str | None:
            if page.next_offset is None:
                return None
            return codec.encode(
                revision=revision, level=level, alias=page_alias,
                offset=page.next_offset,
            )

        try:
            if selector.level == "servers":
                page = self._catalog.paginate(
                    snapshot.servers_view(),
                    offset=offset,
                    limit=selector.limit,
                    render=lambda item: item,
                    level="servers",
                )
                return {
                    "level": "servers",
                    "items": page.items,
                    "catalog_revision": revision,
                    "next_cursor": _cursor("servers", "", page),
                }
            if selector.level == "tool":
                return {
                    "level": "tool",
                    "alias": alias,
                    "tool": snapshot.tool_detail(alias, tool),
                    "catalog_revision": revision,
                }
            page = self._catalog.paginate(
                snapshot.tools_view(alias),
                offset=offset,
                limit=selector.limit,
                render=lambda item: dict(item),
                level="tools",
                alias=alias,
            )
            return {
                "level": "tools",
                "alias": alias,
                "items": page.items,
                "catalog_revision": revision,
                "next_cursor": _cursor("tools", alias or "", page),
            }
        except CatalogError as error:
            raise CommandError(
                error.code, error.message, execution_state="not_started"
            ) from None

    async def _refresh_alias_inventory(self, alias: str | None) -> None:
        """Force one bounded inventory re-read of one alias before discovery."""
        if alias is None:
            return
        run = self.hub._runtimes.get(alias)
        provider = None if run is None else run.provider
        if provider is None:
            # Not running (disabled/unavailable): the hub state is already in
            # the catalog; nothing executable to re-read.
            return
        try:
            # Discovery forces the re-read: invalidate first so the provider
            # cannot serve its cache, then perform one bounded read.
            provider.invalidate_inventory()
            await asyncio.wait_for(
                provider.list_tools(), timeout=self._provider_reread_timeout()
            )
        except Exception:
            # A failed reread keeps the alias listed with its current catalog
            # state; the failure marks the provider inventory invalid.
            pass
        record = self.hub.alias_record(alias)
        if record is not None and self._catalog is not None:
            self._catalog.update_alias(record)

    def _provider_reread_timeout(self) -> float:
        # Bounded by the provider's own deadline and the global invocation
        # deadline; this wait_for is the outer bound on the refresh path.
        return 5.0

    def _redacted_entry(self, alias: str, entry: McpServerEntry) -> dict[str, Any]:
        redacted: dict[str, Any] = entry.yaml_value()
        try:
            keys = sorted(read_alias_env(self.hub.config_path, alias))
        except ConfigError:
            keys = []
        if keys:
            redacted["env_keys"] = keys
        return redacted

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def _add(self, arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = self._parse_mutation(arguments)
        if isinstance(parsed, dict):
            return parsed
        alias, entry, env = parsed
        if _alias_exists(self.hub, alias):
            return _ControlCapabilityErrors.alias_conflict()
        try:
            mcp_entry_add(self.hub.config_path, alias, entry, env)
        except ConfigError as exc:
            return self._config_write_error(exc)
        status = await self.hub.reconcile_alias(alias)
        self._catalog_refresh()
        await self._notify()
        if status.state is AliasState.UNAVAILABLE:
            return self._spawn_failed(status)
        return {
            "alias": alias,
            "status": status.state.value,
            "entry": self._redacted_entry(alias, self._entry_model(alias)),
        }

    async def _modify(self, arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = self._parse_mutation(arguments)
        if isinstance(parsed, dict):
            return parsed
        alias, entry, env = parsed
        if not _alias_exists(self.hub, alias):
            return _ControlCapabilityErrors.alias_unknown()
        try:
            mcp_entry_replace(self.hub.config_path, alias, entry, env)
        except ConfigError as exc:
            return self._config_write_error(exc)
        status = await self.hub.reconcile_alias(alias)
        self._catalog_refresh()
        await self._notify()
        if status.state is AliasState.UNAVAILABLE:
            return self._spawn_failed(status)
        return {
            "alias": alias,
            "status": status.state.value,
            "entry": self._redacted_entry(alias, self._entry_model(alias)),
        }

    @staticmethod
    def _spawn_failed(status: Any) -> dict[str, Any]:
        error = status.last_error or {
            "code": "spawn_failed",
            "message": "the local MCP server could not be started",
        }
        return dict(error)

    async def _delete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed = _AliasInput.model_validate(arguments)
        except ValidationError:
            return _ControlCapabilityErrors.invalid_alias()
        alias = parsed.alias
        if not _valid_alias(alias):
            return _ControlCapabilityErrors.invalid_alias()
        if not _alias_exists(self.hub, alias):
            return _ControlCapabilityErrors.alias_unknown()
        try:
            # The YAML write is the commit (invariant 2): a failed removal
            # leaves the alias declared and running — nothing to roll back.
            mcp_entry_remove(self.hub.config_path, alias)
        except ConfigError as exc:
            return self._config_write_error(exc)
        await self.hub.forget(alias)
        # Best-effort cleanup of the alias launcher cache (uvx/npx downloads
        # under <config dir>/mcp/<alias>, by default
        # ~/.mcp-relay/mcp/<alias>): the alias no longer exists in YAML or
        # runtime, so its cache is garbage. remove_alias_cache never raises
        # and never touches other aliases' caches.
        remove_alias_cache(self.hub.config_path, alias)
        self._catalog_refresh()
        await self._notify()
        return {"alias": alias, "status": "deleted"}

    async def _enable(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._set_enabled(arguments, True)

    async def _disable(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._set_enabled(arguments, False)

    async def _set_enabled(
        self, arguments: dict[str, Any], enabled: bool
    ) -> dict[str, Any]:
        # Step 7B distinction — administration-triggered startup vs the
        # caller's deadline: this verb reconciles inline, so an alias whose
        # startup outlives the ordinary invocation deadline (shorter than
        # the 120 s startup budget) is cancelled with the caller's call.
        # There is deliberately no job system and no deferred completion:
        # the cancellation keeps the existing honest semantics — the YAML
        # commit stays (the enable is durable), the in-flight transport is
        # closed, the alias reports ``spawn_cancelled``/unavailable
        # uncertainty through the catalog (never a fabricated running
        # state), and a later explicit reconcile may retry.
        try:
            parsed = _AliasInput.model_validate(arguments)
        except ValidationError:
            return _ControlCapabilityErrors.invalid_alias()
        alias = parsed.alias
        if not _valid_alias(alias):
            return _ControlCapabilityErrors.invalid_alias()
        if not _alias_exists(self.hub, alias):
            return _ControlCapabilityErrors.alias_unknown()
        try:
            mcp_entry_set_enabled(self.hub.config_path, alias, enabled)
        except ConfigError as exc:
            return self._config_write_error(exc)
        status = await self.hub.reconcile_alias(alias)
        self._catalog_refresh()
        await self._notify()
        return {
            "alias": alias,
            "enabled": enabled,
            "runtime_state": status.state.value,
        }

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _parse_mutation(
        self, arguments: dict[str, Any]
    ) -> tuple[str, dict[str, Any], dict[str, str] | None] | dict[str, Any]:
        if not isinstance(arguments, dict):
            return _ControlCapabilityErrors.invalid_entry()
        alias = arguments.get("alias")
        if not isinstance(alias, str) or not _valid_alias(alias):
            return _ControlCapabilityErrors.invalid_alias()
        entry = arguments.get("entry")
        if not isinstance(entry, dict):
            return _ControlCapabilityErrors.invalid_entry()
        entry = dict(entry)
        env = entry.pop("env", None)
        if env is not None and (
            not isinstance(env, dict)
            or len(env) > MCP_ENV_MAX_KEYS
            or not all(
                isinstance(key, str)
                and isinstance(value, str)
                and value
                for key, value in env.items()
            )
        ):
            return _ControlCapabilityErrors.invalid_entry()
        try:
            McpServerEntry.model_validate(entry)
        except ValidationError:
            return _ControlCapabilityErrors.invalid_entry()
        return alias, entry, env

    def _entry_model(self, alias: str) -> McpServerEntry:
        desired = self.hub.desired_states()
        return desired[alias]

    @staticmethod
    def _config_write_error(exc: ConfigError) -> dict[str, str]:
        message = str(exc)
        if "already exists" in message:
            return _ControlCapabilityErrors.alias_conflict()
        if "unknown" in message:
            return _ControlCapabilityErrors.alias_unknown()
        if "invalid" in message or "must be" in message or "requires" in message:
            return _ControlCapabilityErrors.invalid_entry(message)
        return _ControlCapabilityErrors.config_invalid()

    async def _notify(self) -> None:
        if self._on_inventory_change is None:
            return
        try:
            await self._on_inventory_change()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed re-announcement must not fail the committed mutation.
            return


def _alias_exists(hub: McpHub, alias: str) -> bool:
    from ..config import mcp_entries

    try:
        return alias in mcp_entries(hub.config_path)
    except ConfigError:
        return False
