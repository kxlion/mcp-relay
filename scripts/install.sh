#!/usr/bin/env bash

set -euo pipefail

package_version="${MCP_RELAY_VERSION:-}"
project_root="${MCP_RELAY_PROJECT_ROOT:-}"

if [[ "$(uname -s)" != "Linux" ]]; then
    printf 'MCP Relay Linux installer requires Linux.\n' >&2
    exit 1
fi
if [[ -n "$package_version" && ! "$package_version" =~ ^[0-9]+(\.[0-9]+){1,2}([a-z]+[0-9]+)?$ ]]; then
    printf 'MCP_RELAY_VERSION must be a release version such as 0.1.0.\n' >&2
    exit 1
fi
if [[ -n "$project_root" ]]; then
    if [[ ! -d "$project_root" || ! -f "$project_root/pyproject.toml" ]]; then
        printf 'MCP_RELAY_PROJECT_ROOT is not a valid MCP Relay project: %s\n' "$project_root" >&2
        exit 1
    fi
    package_spec="$project_root"
elif [[ -n "$package_version" ]]; then
    package_spec="mcp-relay==$package_version"
else
    package_spec="mcp-relay"
fi

setup_mode="${MCP_RELAY_SETUP:-prompt}"
case "$setup_mode" in
    prompt|skip) ;;
    *)
        printf "MCP_RELAY_SETUP must be 'prompt' or 'skip'; for unattended setup, deploy config.yaml and environment variables yourself.\n" >&2
        exit 1
        ;;
esac

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

if ! uv_path="$(find_uv)"; then
    if ! command -v curl >/dev/null 2>&1; then
        printf 'curl is required to install uv. Install curl and rerun the installer.\n' >&2
        exit 1
    fi
    printf 'uv not found; running the official uv installer...\n'
    curl -fsSL https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if ! uv_path="$(find_uv)"; then
        printf 'uv was installed but could not be found on PATH.\n' >&2
        exit 1
    fi
fi

# The installer may arrive via a pipe, so use the controlling terminal for
# onboarding rather than forwarding the install script as CLI input.
if [[ "$setup_mode" == "prompt" ]] && ! ( : </dev/tty ) 2>/dev/null; then
    setup_mode='skip'
fi

printf 'Installing %s for the current user...\n' "$package_spec"
"$uv_path" tool install --force --python 3.14 "$package_spec"
tool_bin="$("$uv_path" tool dir --bin)"
if [[ ! -d "$tool_bin" ]]; then
    printf 'uv did not report a valid tool bin directory.\n' >&2
    exit 1
fi
export PATH="$tool_bin:$PATH"
if [[ "${MCP_RELAY_SKIP_PATH_UPDATE:-0}" != "1" ]]; then
    if ! "$uv_path" tool update-shell >/dev/null 2>&1; then
        printf 'Warning: uv could not update the shell profile; add %s to PATH manually.\n' "$tool_bin" >&2
    fi
fi

mcp_relay_command="$tool_bin/mcp-relay"
if [[ ! -x "$mcp_relay_command" ]]; then
    mcp_relay_command="$(command -v mcp-relay || true)"
fi
if [[ -z "$mcp_relay_command" || ! -x "$mcp_relay_command" ]]; then
    printf 'The mcp-relay command was not found in %s.\n' "$tool_bin" >&2
    exit 1
fi

if [[ "$setup_mode" == "prompt" ]]; then
    "$mcp_relay_command" onboard </dev/tty
else
    printf 'Skipping interactive onboarding. For unattended deployment, supply ~/.mcp-relay/config.yaml and Server/Client environment variables (or private ~/.mcp-relay/.env) yourself.\n'
fi

printf '\n%s installed for the current user.\n' "$("$mcp_relay_command" --version)"
if [[ "$setup_mode" == "skip" ]]; then
    printf 'Run guided setup later from a terminal with: mcp-relay onboard\n'
fi
printf 'Start the configured runtime with mcp-relay server and/or mcp-relay client.\n'
