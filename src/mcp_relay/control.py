"""Client status and the admin verbs that manage local MCP server aliases.

Every mutation follows validate → write YAML atomically → apply. A runtime
failure after a successful write never edits the YAML. The verbs act on child
MCP servers only, never on the Relay Client itself. Refusals raise
``CommandError`` so the Server renders them as ``isError`` results.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from .config import (
    MCP_ALIAS_PATTERN,
    MCP_ENV_MAX_KEYS,
    RESERVED_MCP_ALIASES,
    ConfigError,
    McpServerEntry,
    mcp_entries,
    mcp_entry_add,
    mcp_entry_remove,
    mcp_entry_replace,
    mcp_entry_set_enabled,
    read_alias_env,
)
from .diagnostics import error as _error_log
from .diagnostics import info as _info_log
from .mcp_catalog import ClientCatalog
from .mcp_command import CommandError
from .mcp_hub import AliasState, HubError, McpHub, remove_alias_cache
from .protocol import (
    ADMIN_OPERATIONS,
    OP_CLIENT_STATUS,
    OP_MCP_ADD,
    OP_MCP_DELETE,
    OP_MCP_DISABLE,
    OP_MCP_ENABLE,
    OP_MCP_MODIFY,
)


def _valid_alias(alias: object) -> bool:
    return (
        isinstance(alias, str)
        and bool(MCP_ALIAS_PATTERN.fullmatch(alias))
        and alias not in RESERVED_MCP_ALIASES
    )


class Control:
    """Answer ``client.status`` and the admin verbs for one Relay Client."""

    def __init__(
        self,
        *,
        hub: McpHub | None,
        catalog: ClientCatalog,
        client_version: str,
        admin_enabled: bool = False,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.hub = hub
        self._catalog = catalog
        self._client_version = client_version
        # Read once at startup; the verbs can neither change nor bypass it.
        self.admin_enabled = admin_enabled
        self._monotonic = monotonic or time.monotonic
        self._started_at = self._monotonic()

    async def invoke(
        self, operation: str, arguments: dict[str, Any], *, request_id: str
    ) -> dict[str, Any]:
        if operation == OP_CLIENT_STATUS:
            if arguments:
                raise CommandError("invalid_arguments", "status takes no arguments")
            return self.status()
        handler = {
            OP_MCP_ADD: self._add,
            OP_MCP_MODIFY: self._modify,
            OP_MCP_DELETE: self._delete,
            OP_MCP_ENABLE: self._enable,
            OP_MCP_DISABLE: self._disable,
        }.get(operation)
        if handler is None:
            raise CommandError("invalid_arguments", "unknown relay operation")
        try:
            if not self.admin_enabled:
                raise CommandError(
                    "permission_denied", "administration is disabled on this client"
                )
            if self.hub is None:
                raise CommandError(
                    "config_invalid", "administration requires a YAML configuration"
                )
            result = await handler(self.hub, arguments)
        except HubError as error:
            self._log_admin(operation, request_id, arguments, code=error.code)
            raise CommandError(error.code, error.message) from None
        except CommandError as error:
            self._log_admin(operation, request_id, arguments, code=error.code)
            raise
        self._log_admin(operation, request_id, arguments)
        return result

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        records = self._catalog.records
        publication = self._catalog.publication_errors
        servers = []
        for alias in sorted(records):
            record = records[alias]
            servers.append(
                {
                    "alias": alias,
                    "enabled": record.enabled,
                    "runtime_state": record.runtime_state,
                    "transport": record.transport,
                    "published_tools": (
                        0 if alias in publication else len(record.exposed())
                    ),
                    "error": publication.get(alias) or record.error,
                }
            )
        return {
            "version": self._client_version,
            "uptime_seconds": max(0, int(self._monotonic() - self._started_at)),
            "admin": self.admin_enabled,
            "disk_differs": [] if self.hub is None else self.hub.disk_differs(),
            "mcp_servers": servers,
        }

    # ------------------------------------------------------------------
    # Admin verbs
    # ------------------------------------------------------------------

    async def _add(self, hub: McpHub, arguments: dict[str, Any]) -> dict[str, Any]:
        alias, entry, env = self._parse_mutation(arguments)
        if alias in _entries(hub):
            raise CommandError("alias_conflict", "an MCP server alias already exists")
        _write(lambda: mcp_entry_add(hub.config_path, alias, entry, env))
        return await self._applied(hub, alias)

    async def _modify(self, hub: McpHub, arguments: dict[str, Any]) -> dict[str, Any]:
        alias, entry, env = self._parse_mutation(arguments)
        if alias not in _entries(hub):
            raise CommandError("alias_unknown", "no such MCP server alias")
        _write(lambda: mcp_entry_replace(hub.config_path, alias, entry, env))
        return await self._applied(hub, alias)

    async def _delete(self, hub: McpHub, arguments: dict[str, Any]) -> dict[str, Any]:
        alias = self._existing_alias(hub, arguments)
        _write(lambda: mcp_entry_remove(hub.config_path, alias))
        await hub.forget(alias)
        remove_alias_cache(hub.config_path, alias)
        return {"alias": alias, "status": "deleted"}

    async def _enable(self, hub: McpHub, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._set_enabled(hub, arguments, True)

    async def _disable(self, hub: McpHub, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._set_enabled(hub, arguments, False)

    async def _set_enabled(
        self, hub: McpHub, arguments: dict[str, Any], enabled: bool
    ) -> dict[str, Any]:
        # Startup runs inline: if the caller's deadline fires first, the call
        # is cancelled, the YAML commit stays, and the alias reports
        # ``spawn_cancelled`` until a later reconcile.
        alias = self._existing_alias(hub, arguments)
        _write(lambda: mcp_entry_set_enabled(hub.config_path, alias, enabled))
        status = await hub.reconcile_alias(alias)
        return {
            "alias": alias,
            "enabled": enabled,
            "runtime_state": status.state.value,
        }

    async def _applied(self, hub: McpHub, alias: str) -> dict[str, Any]:
        status = await hub.reconcile_alias(alias)
        if status.state is AliasState.UNAVAILABLE:
            error = status.last_error or {}
            raise CommandError(
                error.get("code", "spawn_failed"),
                error.get("message", "the local MCP server could not be started"),
            )
        entry = McpServerEntry.model_validate(_entries(hub)[alias]).yaml_value()
        try:
            env_keys = sorted(read_alias_env(hub.config_path, alias))
        except ConfigError:
            env_keys = []
        if env_keys:
            entry["env_keys"] = env_keys
        return {"alias": alias, "status": status.state.value, "entry": entry}

    @staticmethod
    def _existing_alias(hub: McpHub, arguments: dict[str, Any]) -> str:
        if set(arguments) != {"alias"} or not _valid_alias(arguments["alias"]):
            raise CommandError("invalid_alias", "alias must be 1-16 lowercase letters")
        alias = arguments["alias"]
        if alias not in _entries(hub):
            raise CommandError("alias_unknown", "no such MCP server alias")
        return alias

    @staticmethod
    def _parse_mutation(
        arguments: dict[str, Any],
    ) -> tuple[str, dict[str, Any], dict[str, str] | None]:
        alias = arguments.get("alias")
        if not _valid_alias(alias):
            raise CommandError("invalid_alias", "alias must be 1-16 lowercase letters")
        entry = arguments.get("entry")
        if set(arguments) - {"alias", "entry"} or not isinstance(entry, dict):
            raise CommandError("invalid_entry", "MCP server entry is invalid")
        entry = dict(entry)
        env = entry.pop("env", None)
        if env is not None and (
            not isinstance(env, dict)
            or len(env) > MCP_ENV_MAX_KEYS
            or not all(
                isinstance(key, str) and isinstance(value, str) and value
                for key, value in env.items()
            )
        ):
            raise CommandError("invalid_entry", "MCP server entry env is invalid")
        try:
            McpServerEntry.model_validate(entry)
        except ValidationError:
            raise CommandError("invalid_entry", "MCP server entry is invalid") from None
        assert isinstance(alias, str)
        return alias, entry, env

    @staticmethod
    def _log_admin(
        operation: str,
        request_id: str,
        arguments: dict[str, Any],
        *,
        code: str | None = None,
    ) -> None:
        """One sanitized line per admin outcome: never entries, argv or env."""
        if operation not in ADMIN_OPERATIONS:
            return
        parts = ["mcp.admin:", f"operation={operation}", f"request_id={request_id}"]
        alias = arguments.get("alias")
        if _valid_alias(alias):
            parts.append(f"alias={alias}")
        if code is None:
            _info_log(" ".join([*parts, "result=ok"]))
        else:
            _error_log(" ".join([*parts, f"code={code}"]))


def _entries(hub: McpHub) -> dict[str, Any]:
    try:
        return mcp_entries(hub.config_path)
    except ConfigError:
        raise CommandError(
            "config_invalid", "client configuration is invalid"
        ) from None


def _write(action: Callable[[], None]) -> None:
    """Run one YAML commit, mapping its safe error onto a closed code."""
    try:
        action()
    except ConfigError as exc:
        message = str(exc)
        if "already exists" in message:
            raise CommandError("alias_conflict", "an MCP server alias already exists")
        if "unknown" in message:
            raise CommandError("alias_unknown", "no such MCP server alias")
        if "invalid" in message or "must be" in message or "requires" in message:
            raise CommandError("invalid_entry", message)
        raise CommandError("config_invalid", "client configuration is invalid")
