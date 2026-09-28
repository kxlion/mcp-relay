# Discover and use your MCP tools

[README](../README.md) · [CLI and configuration](cli.md) · [Protocol](protocol.md)

Your cloud AI connects to one MCP Relay endpoint. From there it sees the tools
of the MCP servers you configured on your computer, plus a few Relay tools.
Each configured server has an **alias**: a short name such as `localtools`.

## Your tools, published natively

Every tool of a running local server appears in `tools/list` as an ordinary MCP
tool named `<alias>__<tool>`, for example `localtools__read_file`. The
description, input schema, output schema and annotations are the server's own.
Call it like any other MCP tool:

```json
{"name": "localtools__read_file", "arguments": {"path": "notes.txt"}}
```

The result is the tool's native MCP result, including content,
`structuredContent`, `isError` and bounded top-level extensions. Relay does not
flatten it into text or add a wrapper.

Public names use only `A-Z a-z 0-9 _ -` and at most 64 characters. When
`<alias>__<tool>` does not fit that rule, Relay shortens it and appends an
8-character hash, so the name stays stable and unique; the original tool name
is still the one sent to your server.

The list follows your Client. Relay sends `notifications/tools/list_changed` to
open MCP sessions when:

- the Client connects, disconnects or reconnects;
- a server starts, stops, is added, modified, enabled, disabled or deleted;
- a local server announces its own tool-list change and Relay has re-read it.

While the Client is offline, only the Relay tools are listed.

**A call is sent once.** Relay does not automatically replay it after a
timeout, cancellation or lost response. A tool may change files or perform
other side effects; Relay does not make it read-only or idempotent. Check the
execution state before deciding whether to call it again.

## Choose which tools are published

By default every tool of a server is published. Add `tools:` to an entry to
publish only the listed tools, and optionally replace their description:

```yaml
mcp_servers:
  browser:
    command: [npx, "@example/browser-mcp"]
    tools:
      navigate:
      screenshot:
        description: Capture the current page as a PNG.
```

A short, well-described list helps less capable agents pick the right tool.
`tools:` accepts 1 to 128 tool names; a description override is at most 2048
characters. Listed names that the server does not provide are ignored. A tool
left out of the list cannot be called through Relay.

## Relay tools

| Tool | Purpose | Listed when |
|---|---|---|
| `relay_status` | Server, Client and local server state | Always |
| `relay_registry_search` | Search registry metadata for MCP servers | Always |
| `relay_mcp_add` | Declare and start a server alias | Client has `admin: true` |
| `relay_mcp_modify` | Replace a server entry | Client has `admin: true` |
| `relay_mcp_delete` | Remove a server entry | Client has `admin: true` |
| `relay_mcp_enable` | Enable a configured server | Client has `admin: true` |
| `relay_mcp_disable` | Disable a server while retaining its entry | Client has `admin: true` |

None of these tools stops or restarts the Relay Client itself.

## Check state: `relay_status`

Input: `{}`. The response has four parts:

| Field | Meaning |
|---|---|
| `server` | Server `version`, `relay_contract` and `published_tools` count |
| `client` | Connection, `admin`, `invocation_state`, progress, heartbeat age, version, connection time, last disconnect, uptime |
| `mcp_servers` | One entry per alias: `alias`, `enabled`, `runtime_state`, `transport`, `published_tools`, `error` |
| `disk_differs` | Aliases whose on-disk settings differ from runtime |

`client.report` says where the Client part comes from:

| Value | Meaning |
|---|---|
| `live` | The Client answered a short probe (at most 2 seconds) |
| `cached` | The Client was busy or unreachable; `report_age_seconds` gives the age of the last good answer |
| `unavailable` | No answer has been received yet; `mcp_servers` and `disk_differs` are `null` |

`runtime_state` is `starting`, `running`, `unavailable` or `disabled`. A server
that publishes no tools reports why in `error`, with a code such as
`alias_disabled`, `alias_starting`, `inventory_stale`, `spawn_failed`,
`alias_unavailable`, `name_collision` or `catalog_too_large`. Timestamps use
RFC3339 UTC. Status never shows credential values.

## Server entries

Declare entries in the Client's `config.yaml` under `mcp_servers`, or use the
administration tools below. Each entry requires exactly one launch method:

| Field | Use it for | Transport |
|---|---|---|
| `command` | An executable with an argument list, launched without a shell | stdio |
| `url` | An existing `http://` or `https://` MCP endpoint | Streamable HTTP |
| `source` | A registry server ID resolved to a declarative launcher | stdio |

For example, an already-running local HTTP server:

```yaml
mcp_servers:
  localtools:
    url: http://127.0.0.1:9000/mcp
    enabled: true
```

Replace the URL with your server's endpoint. For a local process, use a
`command` array with an absolute executable path or a PATH-resolvable name.
Install the server and any required runtime yourself. A declared `uvx` or `npx`
launcher may fetch packages when it starts.

`source` must identify a server returned by registry discovery, not merely an
npm or Python package name. Optional `version` pins apply only to `source`.
Resolved launchers cache packages under `~/.mcp-relay/mcp/<alias>`.

### Entry rules and secrets

- Aliases use 1–16 lowercase letters, are unique, and are limited to 32 entries.
  `client`, `mcp` and `server` are reserved.
- `command` accepts 1–8 items, each at most 512 characters; URLs are at most
  2048 characters and cannot contain user information.
- `source` is at most 255 characters; `version` is at most 64.
- YAML may include `enabled` (default `true`) and `tools`. Transport is derived
  from the chosen launch method, not written as a separate field.
- Credentials belong in the private `~/.mcp-relay/mcp/<alias>.env`, not YAML.
  Administration inputs may supply `env`; the Client writes those values to
  the alias file. The file is limited to 4 KiB and 32 keys, with private access.

Relay does not bundle a particular MCP server or guarantee its behavior.
Third-party payloads pass through without secret scanning. Configure each
server's permissions and data access accordingly.

## Manage servers

Administration is allowed only when the Client YAML contains `admin: true`.
Enable it locally with:

```sh
mcp-relay config set admin true
```

Restart the Client. To lock it, run `mcp-relay config unset admin` and restart
again. Without the switch the five administration tools are not listed, and a
call that reaches the Client anyway returns `permission_denied` before any
mutation. Calling your servers' tools does not require this switch.

### Add or replace

Example input for `relay_mcp_add`, using an existing HTTP MCP server:

```json
{
  "alias": "localtools",
  "entry": {"url": "http://127.0.0.1:9000/mcp"}
}
```

`relay_mcp_modify` takes the same shape. **Modify replaces the complete entry**,
so include the settings you intend to keep, `tools` included. Optional `env`
inside `entry` supplies the server's environment credentials; returned entries
expose only their names through `env_keys`.

A successful response reports `alias`, the reached runtime `status` and the
redacted `entry`. Adding an existing alias returns `alias_conflict`; modifying
an unknown alias returns `alias_unknown`.

### Enable, disable or delete

Each accepts `{"alias":"localtools"}`:

| Tool | Effect |
|---|---|
| `relay_mcp_enable` | Persist the enabled state and reconcile the server |
| `relay_mcp_disable` | Persist the disabled state and stop its connection/process |
| `relay_mcp_delete` | Remove the entry, stop the server and clean its launcher cache best-effort |

Enable/disable return `alias`, `enabled` and `runtime_state`. Delete returns
`{"alias":"localtools","status":"deleted"}`. Enable/disable are idempotent;
deleting an unknown alias returns `alias_unknown`.

### What happens if startup fails?

Changes follow **validate → write configuration → apply runtime change**. A
failed YAML write leaves the entry unchanged. Once saved, an entry remains
configured even if its server cannot start; Relay does not silently disable or
delete it. Inspect `relay_status` for the resulting state.

Startup is isolated per alias, with up to three attempts within a shared
120-second budget. A caller's deadline or cancellation can end an administration
operation sooner. A saved configuration does not prove that startup succeeded;
check runtime state before retrying.

## Search the registry

`relay_registry_search` queries metadata on the Relay Server without changing
your Client configuration. Example input:

```json
{"query":"filesystem","limit":10}
```

| Argument | Meaning |
|---|---|
| `query` | Server-name search, 1–200 characters |
| `limit` | Page size, 1–50; default 10 |
| `cursor` | Optional continuation cursor |
| `version` | Optional `latest` or exact version |
| `updated_since` | Optional RFC3339 timestamp; Relay also requests deleted entries |
| `include_deleted` | Optional boolean, default `false` |

Results include `name`, title, description, version, repository URL and package
metadata, with an optional `next_cursor`. Use the returned registry `name` as
`source` if you choose to add that server. Search does not install or enable it.
Registry presence is not a security review.

Responses are bounded to the requested count and a 5 MiB upstream page budget.
Entries with more than eight packages are skipped. Oversized pages or registry
failures return `registry_unreachable`; a truncated over-count page drops its
cursor to avoid silently skipping entries.

## Errors and recovery

Relay failures produce an MCP error result (`isError: true`) with a JSON text
payload containing `code`, a bounded `message` and `execution_state`:

| Execution state | Meaning |
|---|---|
| `not_started` | The target operation was not sent |
| `unknown` | The operation may have happened; its outcome is uncertain |

Native third-party `isError` results remain intact rather than being rewritten
as Relay errors.

| Code | Next step |
|---|---|
| `invalid_arguments` | Check the request fields and bounds |
| `permission_denied` | Review the local administration setting |
| `client_unavailable` | Check Client connectivity and credentials |
| `client_busy` | Wait for the current invocation to complete |
| `alias_unknown` / `tool_unknown` | The tool list changed; list tools again |
| `alias_unavailable` | Inspect the alias in `relay_status` |
| `result_too_large` | Request a smaller result or inspect documented size limits |
| `timeout` / `execution_failed` | Check execution state before any retry |
| `internal_error` | Inspect local diagnostics; raw exceptions are not returned |
| `invalid_alias` / `alias_conflict` / `invalid_entry` | Correct the server declaration |
| `transport_unsupported` / `spawn_failed` | Check the selected server and its runtime |
| `registry_unreachable` | Retry registry discovery later |
| `config_invalid` | Correct local configuration before retrying |

Only one Client invocation may be in flight. Ordinary tool errors do not require
a reconnect: the next call can use the existing session. A lost response does
not establish that an action failed.

For frame formats, bounds and cancellation, see the [protocol](protocol.md).
