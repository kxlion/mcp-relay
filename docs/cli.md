# Run and configure MCP Relay

[README](../README.md) · [Deployment](deployment.md) · [Tools](tools.md) · [Protocol](protocol.md)

Use this guide after [installation](../README.md#get-started) to configure the
cloud Server, connect your local Client and diagnose problems. Commands below
use the installed `mcp-relay` executable.

## Commands at a glance

| Command | Purpose |
|---|---|
| `mcp-relay onboard` | Guided, interactive setup |
| `mcp-relay server` | Run the cloud Relay Server |
| `mcp-relay client` | Run the local Relay Client |
| `mcp-relay config show` | Inspect effective settings with secrets redacted |
| `mcp-relay config get KEY` | Read a supported Client setting |
| `mcp-relay config set KEY VALUE` | Change a Client setting |
| `mcp-relay config unset KEY` | Reset a Client setting |
| `mcp-relay config validate` | Check configured roles offline |
| `mcp-relay --help` / `--version` | Show commands or the installed version |

Use the default configuration directory. The public CLI accepts no custom
configuration path. Run onboarding without flags; it requires an interactive
terminal.

## Install and upgrade

MCP Relay is published on [PyPI](https://pypi.org/project/mcp-relay/):

```sh
uv tool install mcp-relay
uv tool upgrade mcp-relay
```

uv downloads Python 3.14 when it is not already available. To pin a release,
for example in a script or CI job, install `mcp-relay==<version>`.

The one-line installers (`scripts/install.sh` for Linux and macOS, which needs
Bash and `curl`, and `scripts/install.ps1` for Windows PowerShell 5.1 or newer)
set up uv, install the same package for your user account and start guided
setup when a terminal is available. In the installer's environment:

| Variable | Effect |
|---|---|
| `MCP_RELAY_VERSION=<version>` | Install that release instead of the latest |
| `MCP_RELAY_SETUP=skip` | Skip guided setup |

To inspect an installer before running it:

```bash
curl -fsSL https://raw.githubusercontent.com/kxlion/mcp-relay/main/scripts/install.sh -o install-mcp-relay.sh
less install-mcp-relay.sh
bash install-mcp-relay.sh
```

```powershell
irm https://raw.githubusercontent.com/kxlion/mcp-relay/main/scripts/install.ps1 -OutFile .\install-mcp-relay.ps1
Get-Content .\install-mcp-relay.ps1
.\install-mcp-relay.ps1
```

## Prepare credentials and run setup

Supply credentials through the process environment or a private `.env` file
before starting the runtimes. The Client token must already be available before
Client onboarding.

| Variable | Required on |
|---|---|
| `RELAY_CLIENT_TOKEN` | Server and Client, with the same value |
| `RELAY_MCP_TOKEN` | Server and the cloud AI agent's MCP connection |

Use two different, randomly generated secrets. Each must be 32–256 printable
ASCII characters without spaces. Store `.env` with access restricted to your
account (`0600` on Linux and macOS). Do not put credentials in YAML, command
arguments or URLs, and transfer them between machines through a secure channel.
MCP Relay does not generate or persist these tokens for you.

```sh
mcp-relay onboard
```

Choose **Server-only** on the cloud host and **Client connected to a remote
Server** on your computer. Onboarding writes non-secret Server listener settings
to `.env` and Client settings to `config.yaml`. Existing settings are preserved
unless you explicitly change them.

For an unattended deployment, set `MCP_RELAY_SETUP=skip` when installing, then
provide the environment, private `.env` and Client YAML yourself.

## Where settings live

The default directory is `~/.mcp-relay` on Linux and macOS and
`%USERPROFILE%\.mcp-relay` on Windows.

| File | Contents |
|---|---|
| `config.yaml` | Client identity, relay URL, workspace, MCP servers and `admin` |
| `.env` | Operator-supplied Relay tokens and environment settings |
| `mcp/<alias>.env` | Private credentials for one configured MCP server |
| `server.log` / `client.log` | Runtime diagnostics |

The Server reads environment settings and `.env`; it does not read Client YAML.
Explicit process environment values take precedence over `.env` and mapped
Client YAML settings. `admin` is unlocked only by an explicit `true` in Client
YAML.

### Cloud Server listeners

The Server uses two separate listeners:

| Setting | Default | Endpoint |
|---|---|---|
| `RELAY_SERVER_MCP_HOST` / `RELAY_SERVER_MCP_PORT` | `127.0.0.1:8000` | `/mcp` for your AI agent |
| `RELAY_SERVER_CLIENT_HOST` / `RELAY_SERVER_CLIENT_PORT` | `127.0.0.1:8001` | `/ws` for your local Client |

The two listeners must be distinct. With a TLS proxy on the Server host, keep
both internal listeners on loopback. Route HTTPS `/mcp` to port 8000 and WSS
`/ws` to port 8001, preserving authentication and WebSocket Upgrade. The Client's
remote URL is the public WSS address, for example `wss://relay.example.com/ws`.
Onboarding does not provision the proxy or certificates.

### Local Client settings

Onboarding creates the Client identity. This illustrative YAML shows a Client
connected to the cloud and an already-running local MCP server; replace the
hostname and local server URL for your installation:

```yaml
identity:
  id: 00000000-0000-4000-8000-000000000001
relay_url: wss://relay.example.com/ws
workspace: ./workspace
admin: false
mcp_servers:
  localtools:
    url: http://127.0.0.1:9000/mcp
```

Keep the identity generated for your Client. Relative workspace paths resolve
from the configuration directory. `RELAY_URL`, `RELAY_CLIENT_ID` and
`RELAY_CLIENT_WORKSPACE` can override the corresponding Client settings.
For other server launch methods and credentials, see [server entries](tools.md#server-entries).

## Validate, start and stop

```sh
mcp-relay config validate
```

Validation detects the configured roles, reports unconfigured roles as skipped
and returns a nonzero exit code for invalid configuration. It is an offline
check; it does not prove connectivity to the cloud or to a local MCP server.

On the cloud host:

```sh
mcp-relay server
```

On your computer:

```sh
mcp-relay client
```

Keep both processes running. Use `Ctrl+C` in the corresponding terminal to stop
one. After editing Client YAML, restart the Client. The `relay_status` MCP tool
reports `disk_differs` when alias settings on disk have diverged from runtime.

For a live connection check, ask your AI to call `relay_status`. It reports the
Server's view and, when the Client answers, a `live` report of its MCP servers.

## Connect your AI agent

Add an MCP connection to your cloud AI agent:

| Setting | Value |
|---|---|
| Transport | Streamable HTTP |
| URL | `https://relay.example.com/mcp` with your hostname |
| Authorization header | `Bearer <your RELAY_MCP_TOKEN>` |

Supply the token through your AI host's secret settings. For clients using the
following configuration format and supporting environment interpolation:

```yaml
mcp_servers:
  mcp_relay:
    url: https://relay.example.com/mcp
    headers:
      Authorization: "Bearer ${RELAY_MCP_TOKEN}"
    supports_parallel_tool_calls: false
```

## Change Client settings

```sh
mcp-relay config show
mcp-relay config get admin
mcp-relay config set admin true
mcp-relay config validate
```

Restart the Client to allow remote server administration. To lock it again:

```sh
mcp-relay config unset admin
```

Restart again. This switch gates adding, modifying, deleting, enabling and
disabling server aliases; without it those tools are not listed. It does not
gate calls to tools of configured servers. See [administration](tools.md#manage-servers) for those tools.

`get`, `set` and `unset` take Client keys directly, without a role prefix.
Server listeners and Relay tokens are supplied through environment or `.env`.

## Logging

Both runtimes write to the console and their log file. Files include DEBUG
records; `LOG_LEVEL` changes console verbosity only. Accepted values are
`DEBUG`, `INFO`, `WARNING` and `ERROR` (case-insensitive, default `INFO`).

| Event | What to expect |
|---|---|
| Client connection | Connection, registration, disconnect and retry events |
| Server startup | Two listener startup messages, one per listener |
| HTTP requests | 2xx/3xx at INFO, 4xx at WARNING, 5xx at ERROR |
| Tool execution | Start at DEBUG, completion at INFO, refusal/failure at ERROR |
| Server administration | Mutation outcome at INFO or ERROR |

Records use UTC timestamps. Relay diagnostics omit request payloads, arguments,
credentials and configuration URLs; they report bounded identifiers and outcomes.
A provider's native error remains in the returned MCP result rather than being
copied into the log. Third-party result content is not secret-scanned by Relay.

## Troubleshooting

| Symptom | Action |
|---|---|
| Command not found after installation | Open a new terminal to pick up the updated `PATH` |
| Startup names an invalid token | Check that variable in the runtime environment or private `.env`, including its length and whitespace |
| Client cannot register | Compare `RELAY_CLIENT_TOKEN` on both machines; check WSS routing to the Client listener |
| AI cannot reach `/mcp` | Check the HTTPS URL, the MCP token and the proxy route to the MCP listener |
| `client_unavailable` | Keep the local Client running; check its token, WSS URL and proxy route to the Client listener |
| Configuration changes have no effect | Inspect `config show` for environment overrides and restart the Client after YAML edits |
| `permission_denied` on administration | Explicitly set `admin: true` locally and restart, if administration is intended |
| One MCP server is unavailable | Inspect `relay_status` and `client.log`; check that server's launcher, credentials and dependencies |

For missing tools, timeouts and uncertain tool execution, see
[tool errors](tools.md#errors-and-recovery).

## Remove the installation

Stop the Relay processes, then run:

```sh
uv tool uninstall mcp-relay
```

This removes the installed command and its tool environment. Configuration,
credentials and workspace remain under `~/.mcp-relay`; delete user data separately
only if you no longer need it.
