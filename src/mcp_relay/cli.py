"""Strict public command-line interface for MCP Relay."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml

from . import client, config, server, version


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-relay", add_help=False, description="MCP Relay")
    parser.add_argument("--help", action="help", help="show this help message and exit")
    parser.add_argument(
        "--version", action="version",
        version=f"%(prog)s {version.package_version() or 'unknown'}",
        help="show the program version and exit",
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    config_parser = commands.add_parser("config", add_help=False, help="manage configuration")
    config_parser.add_argument("--help", action="help", help="show this help message and exit")
    config_parser.set_defaults(handler=_run_config)
    config_commands = config_parser.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show", add_help=False)
    get_parser = config_commands.add_parser("get", add_help=False)
    get_parser.add_argument("key", metavar="KEY")
    set_parser = config_commands.add_parser("set", add_help=False)
    set_parser.add_argument("key", metavar="KEY")
    set_parser.add_argument("value", metavar="VALUE")
    unset_parser = config_commands.add_parser("unset", add_help=False)
    unset_parser.add_argument("key", metavar="KEY")
    config_commands.add_parser("validate", add_help=False)
    commands.add_parser("onboard", add_help=False, help="run guided setup").set_defaults(handler=_run_onboard)
    commands.add_parser("server", add_help=False, help="start the Relay Server").set_defaults(handler=_run_server)
    commands.add_parser("client", add_help=False, help="start the outbound Client").set_defaults(handler=_run_client)
    return parser


def _render_validation(report: config.ValidationReport) -> str:
    lines = [report.scope.capitalize()]
    lines.extend(f"[{issue.level}] {issue.message}" for issue in report.issues)
    lines.append("result=valid" if report.valid else "result=invalid")
    return "\n".join(lines)


def _render_show(path: Path) -> str:
    return yaml.safe_dump(config.show_document(path), sort_keys=False, allow_unicode=True)


def _get_value(path: Path, key: str) -> object:
    # Never look up arbitrary paths in show: server topology and secret metadata
    # live there too. This allowlist is derived from the Client schema only.
    if key not in config.configuration_keys(config.ClientConfig):
        raise config.ConfigError(f"unknown client configuration key: {key}")
    shown = config.show_document(path)
    if key == "mcp_servers":
        return {alias: value for alias, value in shown.get("mcp_servers", {}).items()}
    node: object = shown
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise config.ConfigError(f"unknown client configuration key: {key}")
        node = node[part]
    if not isinstance(node, dict) or "value" not in node:
        raise config.ConfigError(f"unknown client configuration key: {key}")
    return node["value"]


def _role_intent(path: Path) -> tuple[bool, bool, str | None]:
    """Detect configured roles without confusing an absent role with a broken one.

    A YAML file always denotes Client intent, even when unreadable. Server
    intent is an MCP token or Server topology key. A present .env is inspected
    explicitly: unreadable/malformed files fail closed rather than skipping.
    """
    env_file = config.dotenv_path(path)
    values: dict[str, str] = {}
    if env_file.exists() or env_file.is_symlink():
        try:
            values = config.read_dotenv_values(path)
        except config.ConfigError as exc:
            return True, True, str(exc)
    names = set(os.environ) | set(values)
    server = "RELAY_MCP_TOKEN" in names or any(name.startswith("RELAY_SERVER_") for name in names)
    client = path.exists() or path.is_symlink() or bool(
        names & {"RELAY_URL", "RELAY_CLIENT_WORKSPACE", "RELAY_CLIENT_ID"}
    ) or ("RELAY_CLIENT_TOKEN" in names and not server)
    # A shared Client token alone can belong to a Server deployment; without
    # Server intent it signals a partially configured Client.
    return server, client, None


def _validate(path: Path) -> int:
    server_present, client_present, error = _role_intent(path)
    if error is not None:
        print(f"Server\n[ERROR] .env is invalid ({error})\nresult=invalid")
        print("Client\nresult=invalid")
        return 1
    valid = server_present or client_present
    for role, present in (("server", server_present), ("client", client_present)):
        if not present:
            print(f"{role.capitalize()}\nresult=skipped (not configured)")
            continue
        try:
            report = config.validate_document(path, role, require=(role == "server" or path.exists()))
        except config.ConfigError as exc:
            print(f"{role.capitalize()}\n[ERROR] {exc}\nresult=invalid")
            valid = False
            continue
        print(_render_validation(report))
        valid = valid and report.valid
    if not server_present and not client_present:
        print("[ERROR] neither Server nor Client is configured")
    return 0 if valid else 1


def _run_config(args: argparse.Namespace, path: Path) -> int:
    if args.config_command == "show":
        print(_render_show(path), end="")
        return 0
    if args.config_command == "get":
        print(json.dumps(_get_value(path, args.key), ensure_ascii=False))
        return 0
    if args.config_command == "set":
        if args.key in {"mcp_token", "client_token"}:
            raise config.ConfigError("tokens are not managed by the relay; set them in the process environment or .env yourself")
        config.set_value(path, "client", args.key, args.value)
        print(f"updated client.{args.key}")
        return 0
    if args.config_command == "unset":
        config.unset_value(path, "client", args.key)
        print(f"reset client.{args.key}")
        return 0
    return _validate(path)


def _run_onboard(_args: argparse.Namespace, path: Path) -> int:
    return run_onboarding(path)


def _run_server(_args: argparse.Namespace, path: Path) -> int:
    config.load_server_runtime(path)
    server.main(["--config", str(path)])
    return 0


def _run_client(_args: argparse.Namespace, path: Path) -> int:
    if _client_environment_is_available(path):
        client.main([])
        return 0
    config.load_client_settings(path)
    client.main(["--config", str(path)])
    return 0


def _client_environment_is_available(path: Path) -> bool:
    return (
        path == config.DEFAULT_CONFIG_PATH
        and not path.exists()
        and bool(os.environ.get("RELAY_URL"))
        and bool(os.environ.get("RELAY_CLIENT_WORKSPACE"))
        and bool(os.environ.get("RELAY_CLIENT_TOKEN"))
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the strict MCP Relay CLI and return a process-compatible status."""
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    if not raw:
        parser.print_help()
        return 0
    if "--help" in raw and raw not in (["--help"], ["config", "--help"]):
        parser.error("only top-level --help and config --help are supported")
    if "--version" in raw and raw != ["--version"]:
        parser.error("only top-level --version is supported")
    try:
        args = parser.parse_args(raw)
    except SystemExit as exc:
        if raw in (["--help"], ["--version"], ["config", "--help"]) and exc.code == 0:
            return 0
        raise
    try:
        return args.handler(args, config.DEFAULT_CONFIG_PATH)
    except config.ConfigError as exc:
        print(f"mcp-relay: error: {exc}", file=sys.stderr)
        return 1


OnboardingRole = Literal["local", "server", "client"]
OnboardingTopology = Literal["local", "lan", "remote"]

SERVER_TOPOLOGY_KEYS = (
    "RELAY_SERVER_MCP_HOST",
    "RELAY_SERVER_MCP_PORT",
    "RELAY_SERVER_CLIENT_HOST",
    "RELAY_SERVER_CLIENT_PORT",
)
DEFAULT_MCP_PORT = 8000
DEFAULT_CLIENT_PORT = 8001
DEFAULT_RELAY_HOST = "127.0.0.1"


@dataclass(frozen=True)
class OnboardingOptions:
    role: OnboardingRole | None = None
    non_interactive: bool = False
    force: bool = False
    mcp_host: str | None = None
    mcp_port: str | None = None
    client_host: str | None = None
    client_port: str | None = None
    topology: OnboardingTopology | None = None
    relay_url: str | None = None
    workspace: str | None = None
    check: bool | None = None

    @classmethod
    def from_namespace(cls, args: object) -> "OnboardingOptions":
        return cls(
            role=getattr(args, "role"),
            non_interactive=getattr(args, "non_interactive"),
            force=getattr(args, "force"),
            mcp_host=getattr(args, "mcp_host"),
            mcp_port=getattr(args, "mcp_port"),
            client_host=getattr(args, "client_host"),
            client_port=getattr(args, "client_port"),
            topology=getattr(args, "topology"),
            relay_url=getattr(args, "relay_url"),
            workspace=getattr(args, "workspace"),
            check=getattr(args, "check"),
        )


class _Prompter:
    def __init__(self, *, non_interactive: bool) -> None:
        self.non_interactive = non_interactive

    def required(self, prompt: str, *, default: str | None = None) -> str:
        if self.non_interactive:
            if default is not None:
                return default
            raise config.ConfigError(
                "non-interactive onboarding requires an explicit value"
            )
        try:
            answer = input(prompt)
        except (EOFError, OSError) as exc:
            raise config.ConfigError("onboarding cancelled") from exc
        answer = answer.strip()
        if answer:
            return answer
        if default is not None:
            return default
        raise config.ConfigError("onboarding cancelled")

    def optional_yes_no(self, prompt: str, *, default: bool = False) -> bool:
        if self.non_interactive:
            return default
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, OSError):
            return default
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        raise config.ConfigError("please answer yes or no")


def _section_exists(path: Path, scope: Literal["client"]) -> bool:
    try:
        config.get_section(path, scope)
    except config.ConfigError as exc:
        if "is not initialized" in str(exc) or "file does not exist" in str(exc):
            return False
        raise
    return True


def _select_role(options: OnboardingOptions, prompter: _Prompter) -> OnboardingRole:
    if options.role is not None:
        return options.role
    if options.non_interactive:
        raise config.ConfigError(
            "non-interactive onboarding requires an explicit role (local, server, or client)"
        )
    print("MCP Relay onboarding")
    print("  1. Local Server + Client")
    print("  2. Server only")
    print("  3. Client connected to a remote Server")
    choice = prompter.required("Choose a setup [1]: ", default="1")
    roles = {"1": "local", "2": "server", "3": "client"}
    try:
        return roles[choice]  # type: ignore[return-value]
    except KeyError as exc:
        raise config.ConfigError("onboarding role selection is invalid") from exc


def _select_topology(
    options: OnboardingOptions,
    prompter: _Prompter,
    *,
    default: OnboardingTopology,
) -> OnboardingTopology:
    if options.topology is not None:
        return options.topology
    if prompter.non_interactive:
        return default
    print("Deployment topology:")
    print("  1. Local — Server and Client on this machine")
    print("  2. LAN — clients on a trusted local network")
    print("  3. Remote — WSS through a reverse proxy or secure tunnel")
    choice = prompter.required("Choose a topology [1]: ", default="1")
    topologies = {"1": "local", "2": "lan", "3": "remote"}
    try:
        return topologies[choice]  # type: ignore[return-value]
    except KeyError as exc:
        raise config.ConfigError("deployment topology selection is invalid") from exc


def _topology_server_host(topology: OnboardingTopology) -> str:
    if topology == "local":
        return "127.0.0.1"
    return "0.0.0.0"


def _parse_port(value: str, *, label: str = "server port") -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise config.ConfigError(f"{label} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise config.ConfigError(f"{label} must be between 1 and 65535")
    return port


def _server_values(
    options: OnboardingOptions,
    prompter: _Prompter,
    *,
    default_topology: OnboardingTopology,
    topology: OnboardingTopology | None = None,
) -> tuple[str, int, str, int]:
    """Resolve the four bind values: (mcp_host, mcp_port, client_host, client_port)."""
    selected_topology = topology or _select_topology(
        options, prompter, default=default_topology
    )
    default_host = _topology_server_host(selected_topology)
    mcp_host = options.mcp_host or prompter.required(
        f"Server MCP bind host [{default_host}]: ", default=default_host
    )
    raw_mcp_port = options.mcp_port or prompter.required(
        f"Server MCP port [{DEFAULT_MCP_PORT}]: ", default=str(DEFAULT_MCP_PORT)
    )
    mcp_port = _parse_port(raw_mcp_port, label="server MCP port")
    client_host = options.client_host or mcp_host
    raw_client_port = options.client_port or prompter.required(
        f"Server Client port [{DEFAULT_CLIENT_PORT}]: ",
        default=str(DEFAULT_CLIENT_PORT),
    )
    client_port = _parse_port(raw_client_port, label="server Client port")
    if mcp_port == client_port:
        raise config.ConfigError("the MCP port and Client port must be distinct")
    return mcp_host, mcp_port, client_host, client_port


def _workspace_value(options: OnboardingOptions, prompter: _Prompter) -> str:
    return options.workspace or prompter.required(
        "Client workspace [./workspace]: ", default="./workspace"
    )


def _report(path: Path, scope: Literal["server", "client"]) -> None:
    report = config.validate_document(path, scope)
    print(_render_validation(report))
    if not report.valid:
        raise config.ConfigError(f"invalid {scope} configuration")


def _report_dotenv(config_path: Path) -> None:
    """Reload and validate the written .env the way ``_report`` does for YAML.

    Reload goes through the guarded private-file readers, so a file that
    lost its 0600 mode or owner fails here instead of silently passing.
    Only key names are reported — never a value.
    """
    issues: list[str] = []
    values: dict[str, str] = {}
    try:
        values = config.read_dotenv_values(config_path)
    except config.ConfigError as exc:
        issues.append(f"[ERROR] {exc}")
    if values:
        carried = [key for key in SERVER_TOPOLOGY_KEYS if key in values]
        if carried:
            issues.append(f"[INFO] topology keys: {', '.join(carried)}")
        missing = [key for key in SERVER_TOPOLOGY_KEYS if key not in values]
        if missing:
            issues.append(
                "[WARNING] topology keys not set: " + ", ".join(missing)
            )
    lines = ["Server (.env)", *issues]
    lines.append("result=valid" if not any(i.startswith("[ERROR]") for i in issues) else "result=invalid")
    print("\n".join(lines))
    if lines[-1] == "result=invalid":
        raise config.ConfigError("invalid server .env configuration")


def _dotenv_display_path(path: Path) -> str | None:
    """Render the private .env location, or None if it would leak the home directory."""
    dotenv_path = path.parent / config.DOTENV_FILENAME
    try:
        return "~/" + dotenv_path.relative_to(Path.home()).as_posix()
    except ValueError:
        return None


def _effective_value(config_path: Path, key: str, default: str) -> str:
    """Resolve one server topology value: environment > .env > default."""
    value = os.environ.get(key)
    if value:
        return value
    values = config.read_dotenv_values(config_path)
    value = values.get(key)
    if value:
        return value
    return default


def _effective_client_endpoint(config_path: Path) -> tuple[str, int]:
    """The effective Client endpoint the server advertises (host, port)."""
    host = _effective_value(config_path, "RELAY_SERVER_CLIENT_HOST", DEFAULT_RELAY_HOST)
    raw_port = _effective_value(
        config_path, "RELAY_SERVER_CLIENT_PORT", str(DEFAULT_CLIENT_PORT)
    )
    return host, _parse_port(raw_port, label="server Client port")


def _relay_host_for_url(host: str) -> str:
    """Format a bind host for use inside a ws:// URL (IPv6 literals bracketed)."""
    host = host.strip()
    if not host or any(ch in host for ch in "/@? #"):
        raise config.ConfigError("server Client bind host is not usable in a relay URL")
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host


def _write_server_topology(
    config_path: Path,
    *,
    force: bool,
    mcp_host: str,
    mcp_port: int,
    client_host: str,
    client_port: int,
) -> None:
    """Merge the server topology into the private .env (never the YAML)."""
    config.merge_dotenv_values(
        config_path,
        {
            "RELAY_SERVER_MCP_HOST": mcp_host,
            "RELAY_SERVER_MCP_PORT": str(mcp_port),
            "RELAY_SERVER_CLIENT_HOST": client_host,
            "RELAY_SERVER_CLIENT_PORT": str(client_port),
        },
        force=force,
    )


def _configure_server(
    path: Path,
    options: OnboardingOptions,
    prompter: _Prompter,
) -> int:
    mcp_host, mcp_port, client_host, client_port = _server_values(
        options, prompter, default_topology="local"
    )
    _write_server_topology(
        path,
        force=options.force,
        mcp_host=mcp_host,
        mcp_port=mcp_port,
        client_host=client_host,
        client_port=client_port,
    )
    _report_dotenv(path)
    location = _dotenv_display_path(path)
    if location is not None:
        print(
            f"Server topology was merged into {location}; existing credentials "
            "and unrelated entries were preserved."
        )
    else:
        print(
            "Server topology was merged into the private .env; existing "
            "credentials and unrelated entries were preserved."
        )
    print(
        "Give a Client administrator the Client secret through a secure "
        "channel; do not paste it into a command or log."
    )
    print("Start the Server with: mcp-relay server")
    return 0


def _configure_local(
    path: Path,
    options: OnboardingOptions,
    prompter: _Prompter,
    *,
    topology: OnboardingTopology,
) -> int:
    client_created = not _section_exists(path, "client") or options.force
    mcp_host, mcp_port, client_host, client_port = _server_values(
        options,
        prompter,
        default_topology="local",
        topology=topology,
    )
    if client_created:
        # A missing token must not leave even a partial topology .env behind.
        config.read_server_client_token(path)
    _write_server_topology(
        path,
        force=options.force,
        mcp_host=mcp_host,
        mcp_port=mcp_port,
        client_host=client_host,
        client_port=client_port,
    )
    _report_dotenv(path)

    if client_created:
        # The local relay URL derives from the EFFECTIVE Client endpoint
        # (environment > .env > defaults): the MCP port never appears in a
        # Client URL, and IPv6 bind hosts are bracketed.
        url_host, url_port = _effective_client_endpoint(path)
        relay_url = f"ws://{_relay_host_for_url(url_host)}:{url_port}/ws"
        config.validate_client_transport(relay_url)
        token = config.read_server_client_token(path)
        workspace = _workspace_value(options, prompter)
        config.init_config(
            path,
            "client",
            force=options.force,
            token=token,
            relay_url=relay_url,
            workspace=workspace,
        )
    else:
        print("Existing Client configuration found; leaving it unchanged.")

    _report(path, "client")
    print("MCP and Client credentials are distinct; neither credential is printed.")
    print("Start the local deployment in two terminals:")
    print("  mcp-relay server")
    print("  mcp-relay client")
    return 0


def _check_connection(
    path: Path,
    options: OnboardingOptions,
) -> None:
    if options.check is False:
        print("Connection check not run; validation above is offline only.")
        return
    if options.check is None and options.non_interactive:
        print("Connection check not run; validation above is offline only.")
        return
    if options.check is None:
        should_check = _Prompter(non_interactive=False).optional_yes_no(
            "Run an authenticated connection check now? [y/N] ", default=False
        )
        if not should_check:
            print("Connection check not run; validation above is offline only.")
            return
    settings = config.load_client_settings(path)
    target = client.safe_server_target(settings.server_url)
    try:
        asyncio.run(client.check_connection(settings))
    except Exception:
        print(
            f"Connection check failed for {target}; configuration is valid, "
            "but the Server was not authenticated."
        )
    else:
        print(
            f"Connection check succeeded for {target}: authenticated "
            "registration confirmed."
        )


def _configure_client(
    path: Path,
    options: OnboardingOptions,
    prompter: _Prompter,
) -> int:
    if _section_exists(path, "client") and not options.force:
        print("Existing Client configuration found; leaving it unchanged.")
        _report(path, "client")
        _check_connection(path, options)
        print("Start the Client with: mcp-relay client")
        return 0

    topology = _select_topology(options, prompter, default="remote")
    if options.relay_url is not None:
        relay_url = options.relay_url.strip()
    else:
        # The local default derives from the EFFECTIVE Client port
        # (environment > .env > default 8001), never the MCP port.
        default_port = (
            _effective_client_endpoint(path)[1] if topology == "local" else DEFAULT_CLIENT_PORT
        )
        default_url = f"ws://127.0.0.1:{default_port}/ws" if topology == "local" else None
        prompt = {
            "local": f"Relay URL [ws://127.0.0.1:{default_port}/ws]: ",
            "lan": "Relay URL (ws://<LAN-IP>:<port>/ws): ",
            "remote": "Public Relay URL (wss://.../ws): ",
        }[topology]
        relay_url = prompter.required(prompt, default=default_url)
    config.validate_client_transport(relay_url)
    if topology == "remote" and urlparse(relay_url).scheme != "wss":
        raise config.ConfigError("remote topology requires a wss:// relay URL")
    workspace = _workspace_value(options, prompter)
    # Credentials are operator-owned: onboarding never accepts or writes tokens.
    # Resolve through the guarded environment/.env reader before creating YAML.
    config.read_server_client_token(path)
    config.init_config(
        path,
        "client",
        force=options.force,
        relay_url=relay_url,
        workspace=workspace,
    )
    _report(path, "client")
    print("Client credential read from the environment or private .env; never printed.")
    _check_connection(path, options)
    print("Start the Client with: mcp-relay client")
    return 0


def run_onboarding(
    path: str | Path,
    options: OnboardingOptions | None = None,
) -> int:
    """Run one guided onboarding flow and return a CLI-compatible status."""
    if options is None:
        if not sys.stdin.isatty():
            raise config.ConfigError(
                "onboarding requires an interactive terminal; for automation, "
                "configure config.yaml and a private .env manually"
            )
        options = OnboardingOptions()
    config_path = Path(path).expanduser()
    prompter = _Prompter(non_interactive=options.non_interactive)
    role = _select_role(options, prompter)
    if role == "server":
        return _configure_server(path=config_path, options=options, prompter=prompter)
    if role == "client":
        return _configure_client(path=config_path, options=options, prompter=prompter)
    if options.topology is not None and options.topology != "local":
        raise config.ConfigError("local role requires local topology")
    print("Deployment topology: local")
    return _configure_local(
        path=config_path,
        options=options,
        prompter=prompter,
        topology="local",
    )


if __name__ == "__main__":
    raise SystemExit(main())
