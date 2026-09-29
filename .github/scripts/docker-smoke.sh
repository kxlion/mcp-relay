#!/usr/bin/env bash
# Smoke-test a locally loaded MCP Relay image: it reports the expected version
# and architecture, starts the Relay Server from environment variables only,
# answers 401 to an unauthenticated /mcp request and 200 to an authenticated
# initialize. Used by the CI and Docker image workflows.
#
# Usage: docker-smoke.sh <image> <version> <arch>

set -euo pipefail

if [[ $# -ne 3 ]]; then
    printf 'Usage: %s <image> <version> <arch>\n' "$0" >&2
    exit 2
fi
image="$1"
version="$2"
arch="$3"

fail() {
    if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
        printf '::error::%s\n' "$1"
    else
        printf 'error: %s\n' "$1" >&2
    fi
    exit 1
}

actual_arch="$(docker image inspect "$image" --format '{{.Architecture}}')"
if [[ "$actual_arch" != "$arch" ]]; then
    fail "image architecture is '$actual_arch', expected '$arch'"
fi

reported="$(docker run --rm "$image" --version)"
if [[ "$reported" != "mcp-relay $version" ]]; then
    fail "image reports '$reported', expected 'mcp-relay $version'"
fi

# Synthetic, single-use tokens for this run only.
mcp_token="$(openssl rand -hex 32)"
client_token="$(openssl rand -hex 32)"
if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
    printf '::add-mask::%s\n' "$mcp_token" "$client_token"
fi

container="mcp-relay-smoke-$$"
docker run -d --name "$container" \
    -e RELAY_SERVER_MCP_HOST=0.0.0.0 -e RELAY_SERVER_CLIENT_HOST=0.0.0.0 \
    -e RELAY_MCP_TOKEN="$mcp_token" -e RELAY_CLIENT_TOKEN="$client_token" \
    -p 127.0.0.1:8000:8000 -p 127.0.0.1:8001:8001 \
    "$image" server >/dev/null
trap 'docker logs "$container"; docker rm -f "$container" >/dev/null' EXIT

code=000
for _ in $(seq 1 30); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8000/mcp || true)"
    [[ "$code" != 000 ]] && break
    sleep 1
done
if [[ "$code" != 401 ]]; then
    fail "unauthenticated /mcp returned $code, expected 401"
fi

code="$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8000/mcp \
    -H "Authorization: Bearer $mcp_token" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}')"
if [[ "$code" != 200 ]]; then
    fail "authenticated initialize returned $code, expected 200"
fi

printf 'Smoke test passed: %s (%s) reports mcp-relay %s.\n' "$image" "$arch" "$version"
