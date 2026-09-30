# Deploy the cloud Server

[README](../README.md) · [CLI](cli.md) · [Tools](tools.md) · [Protocol](protocol.md)

This guide covers the cloud side of MCP Relay: exposing Relay Server behind
HTTPS/WSS, running it with Docker, and the one-computer setup. Credentials,
settings files and diagnostics are in the [CLI guide](cli.md).

MCP Relay does not provision hosting, DNS or TLS. You supply a cloud host and
either a TLS reverse proxy or a secure tunnel in front of the Server.

## Route the public addresses

The Server has two separate listeners, both bound to loopback by default:

| Public address (replace the hostname) | Internal destination | Used by |
|---|---|---|
| `https://relay.example.com/mcp` | `http://127.0.0.1:8000/mcp` | Your cloud AI agent |
| `wss://relay.example.com/ws` | `http://127.0.0.1:8001/ws` with WebSocket Upgrade | Your local Client |

With the proxy on the same host, keep both listeners on loopback and keep the
internal ports private. The proxy must:

- preserve the `Authorization` header on both routes;
- forward the WebSocket Upgrade on `/ws` and allow long-lived connections.

If the proxy runs on another host, bind the listeners to private addresses it
can reach and restrict access with a firewall. The listener settings
(`RELAY_SERVER_MCP_HOST`, `RELAY_SERVER_MCP_PORT`, `RELAY_SERVER_CLIENT_HOST`,
`RELAY_SERVER_CLIENT_PORT`) belong in the Server environment or private `.env`,
never in YAML; see [Cloud Server listeners](cli.md#cloud-server-listeners).

Onboarding (`mcp-relay onboard`, **Server only**, **Remote** topology) writes
these listener settings; you configure the proxy yourself. Then start the
Server:

```sh
mcp-relay config validate
mcp-relay server
```

## Run the Server with Docker

Each release publishes a Server image for `linux/amd64` and `linux/arm64` on
[GitHub Container Registry](https://github.com/kxlion/mcp-relay/pkgs/container/mcp-relay),
tagged with its full version, its `<major>.<minor>` and `latest`. It needs no
configuration file: put both tokens in a private `.env` file as plain
`KEY=value` lines, then run:

```sh
docker run -d --name mcp-relay --restart unless-stopped --env-file .env \
  -e RELAY_SERVER_MCP_HOST=0.0.0.0 -e RELAY_SERVER_CLIENT_HOST=0.0.0.0 \
  -p 127.0.0.1:8000:8000 -p 127.0.0.1:8001:8001 \
  ghcr.io/kxlion/mcp-relay:latest server
```

Inside the container the listeners bind every interface so that Docker can
forward them; the `127.0.0.1` port mappings keep them reachable only from the
host, for a TLS proxy on the same host.

The repository's
[`docker-compose.yml`](https://github.com/kxlion/mcp-relay/blob/main/docker-compose.yml)
runs the same Server but publishes both ports on every host interface: restrict
them with a firewall or change the mappings.

The image is for the cloud Server only. Run the Client on your computer, next
to your MCP servers.

## Run everything on one computer

To try MCP Relay without a cloud host, choose **Local Server + Client** during
onboarding. The MCP endpoint defaults to `http://127.0.0.1:8000/mcp` and the
Client connects to `ws://127.0.0.1:8001/ws`. Both tokens are still required.

Start the Server and the Client in two terminals, then point any MCP client
that supports Streamable HTTP and an Authorization header at the local
endpoint. A cloud AI agent cannot reach your computer through these loopback
addresses.
