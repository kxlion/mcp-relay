# Discover and use your MCP tools

[README](../README.md) · [CLI and configuration](cli.md) · [Protocol](protocol.md)

Your cloud AI connects to one MCP Relay endpoint. From there it can inspect your
local Client, discover the MCP servers you configured, and call their tools.
Each configured server has an **alias**: a short name such as `localtools`.

MCP Relay exposes 10 fixed tools. They remain listed when the Client is offline
or administration is disabled. Tools from your configured servers are reached
through discovery and execution, rather than published individually.

## Start with discovery

Ask your AI to follow this sequence:

1. Call `relay_client_status` with `{}` to check the connection to your computer.
2. Call `relay_mcp_list` with `{}` to list configured servers.
3. Call `relay_mcp_list` with `{"alias":"localtools"}` to list one server's tools.
4. Request a tool's descriptor, including its input schema, with
   `{"alias":"localtools","tool":"example_tool"}`.
5. Call `relay_mcp_command` with the exact alias, tool name, arguments and
   `catalog_revision` returned by discovery.

The alias and tool names below are illustrative. Replace them with names from
your catalog, and copy the actual revision rather than inventing one:

```json
{
  "alias": "localtools",
  "tool": "example_tool",
  "arguments": {},
  "catalog_revision": "<revision returned by discovery>"
}
```

The result is the tool's native MCP result, including content,
`structuredContent`, `isError` and bounded top-level extensions. Relay does not
flatten it into text or add an application-level `result` wrapper.

## Available Relay tools

| Tool | Purpose | Requires local `admin: true` |
|---|---|---|
| `relay_server_status` | Read the Server's connection state | No |
| `relay_registry_search` | Search registry metadata for MCP servers | No |
| `relay_client_status` | Read the connected Client's live state | No |
| `relay_mcp_list` | Discover configured servers, tools and descriptors | No |
| `relay_mcp_command` | Execute one discovered tool | No |
| `relay_mcp_add` | Declare and start a server alias | Yes |
| `relay_mcp_modify` | Replace a server entry | Yes |
| `relay_mcp_delete` | Remove a server entry | Yes |
| `relay_mcp_enable` | Enable a configured server | Yes |
| `relay_mcp_disable` | Disable a server while retaining its entry | Yes |

Server status and registry search run on the Relay Server. The other eight
operations travel to the local Client and return `client_unavailable` if it
cannot be reached. None of these tools stops or restarts the Relay Client itself.

## Check connection and runtime state

### `relay_server_status`

Input: `{}`. Reports the Server's view without contacting the Client:

- Client identity and `connected` state;
- `invocation_state`, progress and heartbeat age;
- Client package version, connection time and last disconnect;
- counters for public Relay tools and announced Client operations.

The response uses these fields directly, not a nested `server`/`client` envelope.
When disconnected, it suggests `start_client`. Public tool count is 10;
announced Client operations are 8 when available. These are not counts of
third-party tools. Connection timestamps use RFC3339 UTC.

### `relay_client_status`

Input: `{}`. Makes a round trip to your computer and reports:

| Field | Meaning |
|---|---|
| `client` | Client package/protocol versions, uptime and workspace |
| `admin` | Whether remote server administration is enabled |
| `catalog_revision` | Current opaque catalog revision |
| `disk_differs` | Aliases whose on-disk settings differ from runtime |
| `hub.configured_aliases` | Configured server count |
| `hub.running_aliases` | Running server count |
| `hub.available_tools` | Tools in executable inventories |

These counters come from the current snapshot; status does not refresh tool
inventories. Apply manual YAML changes by restarting the Client.

## Discover servers and tools

`relay_mcp_list` selects its view from the supplied arguments:

| Arguments | View | Result fields |
|---|---|---|
| `{}` | Servers | `level`, `items`, `catalog_revision`, `next_cursor` |
| `{"alias":"localtools"}` | One server's tools | `level`, `alias`, `items`, `catalog_revision`, `next_cursor` |
| `{"alias":"localtools","tool":"example_tool"}` | One tool's full descriptor | `level`, `alias`, `tool`, `catalog_revision` |

The server view reports `runtime_state` (`starting`, `running`, `unavailable`
or `disabled`), catalog availability and safe errors. Entries expose credential
names through `env_keys`, never their values.

For server and tool lists, `limit` defaults to 20 and accepts integers from
1 to 100. Supply `cursor` from `next_cursor` to continue; `null` marks the last
page. For a full descriptor, omit `limit` and `cursor`.

Cursors are opaque, signed and valid only for their Client runtime and catalog
snapshot. Do not edit them. A changed snapshot returns `catalog_stale`; an
invalid cursor returns `invalid_cursor`. Pages may contain fewer entries than
requested to fit the result budget. A single item that cannot fit returns
`result_too_large`.

First-page discovery refreshes inventories within bounded deadlines. Cursor
pages continue the snapshot. When a local MCP server announces a tool-list
change, the Client marks its inventory non-executable until refreshed. A failed
refresh leaves that server visible with `catalog_available: false`; other
servers remain usable.

## Execute a discovered tool

`relay_mcp_command` requires all four fields: `alias`, `tool`, `arguments` and
`catalog_revision`. Arguments must be a bounded JSON object; the target MCP
server validates them against its tool schema.

Before dispatch, Relay checks the revision, alias, inventory, exact tool name
and route generation. A rejected check never sends the operation to the target.
A stale revision requires fresh discovery.

**A call is sent once.** Relay does not automatically replay it after a timeout,
cancellation or lost response. A tool may change files or perform other side
effects; Relay does not make it read-only or idempotent. Check the execution
state before deciding whether to call it again.

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
- YAML may include `enabled` (default `true`). Transport is derived from the
  chosen launch method, not written as a separate field.
- Credentials belong in the private `~/.mcp-relay/mcp/<alias>.env`, not YAML.
  Administration inputs may supply `env`; the Client writes those values to
  the alias file. The file is limited to 4 KiB and 32 keys, with private access.

Relay does not bundle a particular MCP server or guarantee its behavior.
Third-party payloads pass through without secret scanning. Configure each
server's permissions and data access accordingly.

## Manage servers

Administration is allowed only when the Client YAML explicitly contains
`admin: true`. Enable it locally with:

```sh
mcp-relay config set admin true
```

Restart the Client. To lock it, run `mcp-relay config unset admin` and restart
again. A refused operation returns `permission_denied` before a mutation.
Discovery and execution do not require this switch.

### Add or replace

Example input for `relay_mcp_add`, using an existing HTTP MCP server:

```json
{
  "alias": "localtools",
  "entry": {"url": "http://127.0.0.1:9000/mcp"}
}
```

`relay_mcp_modify` takes the same shape. **Modify replaces the complete entry**,
so read the existing entry first and include the settings you intend to keep.
Optional `env` inside `entry` supplies the server's environment credentials;
returned entries expose only their names.

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
delete it. Inspect `relay_mcp_list` for the resulting state.

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

Relay dispatch and command failures produce an MCP error result (`isError: true`)
with a JSON text payload containing `code`, a bounded `message` and
`execution_state`:

| Execution state | Meaning |
|---|---|
| `not_started` | The target operation was not sent |
| `unknown` | The operation may have happened; its outcome is uncertain |

Control and registry failures can also return `code`, `message` and an optional
`suggested_action`. Native third-party `isError` results remain intact rather
than being rewritten as Relay errors.

| Code | Next step |
|---|---|
| `invalid_arguments` | Check the Relay request fields and bounds |
| `permission_denied` | Review the local administration setting |
| `client_unavailable` | Check Client connectivity and credentials |
| `client_busy` | Wait for the current invocation to complete |
| `catalog_stale` | Discover again and use the new revision/cursor |
| `invalid_cursor` | Restart discovery from the first page |
| `alias_unknown` / `tool_unknown` | Discover exact current names |
| `alias_unavailable` | Inspect the alias's state and discovery error |
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

The public tool list stays fixed, so Relay does not publish tool-list-change
notifications for your server catalog. Use discovery to obtain current state.
For frame formats, bounds and cancellation, see the [protocol](protocol.md).
