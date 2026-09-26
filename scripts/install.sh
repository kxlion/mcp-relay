#!/usr/bin/env bash

set -euo pipefail

repository="https://github.com/kxlion/mcp-relay"
source_ref="${MCP_RELAY_REF:-main}"
source_ref_kind="${MCP_RELAY_REF_KIND:-heads}"
python_version="${MCP_RELAY_PYTHON_VERSION:-3.14.4}"
project_root="${MCP_RELAY_PROJECT_ROOT:-}"
archive_source="${MCP_RELAY_ARCHIVE_SOURCE:-}"
sync_root="${MCP_RELAY_SYNC_ROOT:-}"

if [[ "$(uname -s)" != "Linux" ]]; then
    printf 'MCP Relay Linux installer requires Linux.\n' >&2
    exit 1
fi
if [[ "$source_ref_kind" != "heads" && "$source_ref_kind" != "tags" ]]; then
    printf "MCP_RELAY_REF_KIND must be 'heads' or 'tags'.\n" >&2
    exit 1
fi
if [[ ! "$source_ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then
    printf 'MCP_RELAY_REF contains unsupported characters.\n' >&2
    exit 1
fi
if [[ ! "$python_version" =~ ^[0-9]+(\.[0-9]+){1,2}$ ]]; then
    printf 'MCP_RELAY_PYTHON_VERSION contains unsupported characters.\n' >&2
    exit 1
fi
if [[ -z "$project_root" && -n "$archive_source" ]]; then
    if [[ ! -f "$archive_source" || ! -r "$archive_source" ]]; then
        printf 'MCP_RELAY_ARCHIVE_SOURCE is not a file or is unreadable: %s\n' "$archive_source" >&2
        exit 1
    fi
fi
if ! command -v curl >/dev/null 2>&1; then
    printf 'curl is required. Install curl and rerun the installer.\n' >&2
    exit 1
fi
if ! command -v tar >/dev/null 2>&1; then
    printf 'tar is required. Install tar and rerun the installer.\n' >&2
    exit 1
fi

temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/mcp-relay-install.XXXXXXXX")"
cleanup() {
    if [[ -d "$temporary_root" ]]; then
        rm -rf -- "$temporary_root"
    fi
}
trap cleanup EXIT

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
    printf 'uv not found; downloading the official uv installer...\n'
    curl -fsSL https://astral.sh/uv/install.sh -o "$temporary_root/uv-install.sh"
    sh "$temporary_root/uv-install.sh"
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if ! uv_path="$(find_uv)"; then
        printf 'uv was installed but could not be found on PATH.\n' >&2
        exit 1
    fi
fi

printf 'Installing or verifying Python %s with uv...\n' "$python_version"
"$uv_path" python install "$python_version"
export UV_PYTHON="$python_version"

if [[ -n "$sync_root" ]]; then
    if [[ ! -d "$sync_root" || ! -f "$sync_root/pyproject.toml" || ! -f "$sync_root/uv.lock" ]]; then
        printf 'MCP_RELAY_SYNC_ROOT is not a locked MCP Relay project: %s\n' "$sync_root" >&2
        exit 1
    fi
    printf 'Installing locked MCP Relay dependencies with uv...\n'
    (
        cd -- "$sync_root"
        "$uv_path" sync --locked
    )
fi

archive_path="$temporary_root/mcp-relay.tar.gz"
expanded_root="$temporary_root/expanded"
if [[ -n "$project_root" ]]; then
    if [[ ! -d "$project_root" || ! -f "$project_root/pyproject.toml" ]]; then
        printf 'MCP_RELAY_PROJECT_ROOT is not a valid MCP Relay project: %s\n' "$project_root" >&2
        exit 1
    fi
else
    if [[ -n "$archive_source" ]]; then
        cp -- "$archive_source" "$archive_path"
    else
        archive_uri="https://codeload.github.com/kxlion/mcp-relay/tar.gz/refs/$source_ref_kind/$source_ref"
        printf 'Downloading MCP Relay (%s/%s)...\n' "$source_ref_kind" "$source_ref"
        curl -fsSL "$archive_uri" -o "$archive_path"
    fi
    mkdir -p "$expanded_root"
    tar -xzf "$archive_path" -C "$expanded_root"

    mapfile -t projects < <(find "$expanded_root" -type f -name pyproject.toml -print)
    if [[ "${#projects[@]}" -ne 1 ]]; then
        printf 'The downloaded MCP Relay archive did not contain exactly one project.\n' >&2
        exit 1
    fi
    project_root="$(dirname "${projects[0]}")"
fi

setup_mode="${MCP_RELAY_SETUP:-prompt}"
case "$setup_mode" in
    prompt|skip) ;;
    *)
        printf "MCP_RELAY_SETUP must be 'prompt' or 'skip'; for unattended setup, deploy config.yaml and environment variables yourself.\n" >&2
        exit 1
        ;;
esac

# The installer may arrive via a pipe, so use the controlling terminal for
# onboarding rather than forwarding the install script as CLI input.
if [[ "$setup_mode" == "prompt" ]] && ! ( : </dev/tty ) 2>/dev/null; then
    setup_mode='skip'
fi

printf 'Installing the MCP Relay command for the current user...\n'
"$uv_path" tool install --force "$project_root"
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

invoke_mcp_relay() {
    "$mcp_relay_command" "$@"
}

if [[ "$setup_mode" == "prompt" ]]; then
    invoke_mcp_relay onboard </dev/tty
else
    printf 'Skipping interactive onboarding. For unattended deployment, supply ~/.mcp-relay/config.yaml and Server/Client environment variables (or private ~/.mcp-relay/.env) yourself.\n'
fi

printf '\nMCP Relay installed for the current user.\n'
if [[ "$setup_mode" == "skip" ]]; then
    printf 'Run guided setup later from a terminal with: mcp-relay onboard\n'
fi
printf 'Start the configured runtime with mcp-relay server and/or mcp-relay client.\n'
