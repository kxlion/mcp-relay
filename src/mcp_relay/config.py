"""Canonical YAML configuration and private dotenv credential primitives."""

from __future__ import annotations

import copy
import io
import os
import re
import stat
import tempfile
import uuid
from dataclasses import dataclass
from importlib import import_module
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Any, Callable, ClassVar, Literal, Mapping
from urllib.parse import unquote_plus, urlparse

import yaml
from dotenv import dotenv_values as dotenv_dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .environment import UnknownRelayEnvironmentError
from .json_bounds import is_sensitive_query_key

CONFIG_DIR_NAME = ".mcp-relay"
DOTENV_FILENAME = ".env"
DOTENV_MAX_BYTES = 4096
#: Credentials are read directly from the .env by their own loaders; they
#: are never exported into the process environment by the override loader.
DOTENV_NEVER_EXPORTED = frozenset({"RELAY_MCP_TOKEN", "RELAY_CLIENT_TOKEN"})
DEFAULT_CONFIG_PATH = Path.home() / CONFIG_DIR_NAME / "config.yaml"

# --------------------------------------------------------------------------
# Client-side MCP server aliases (``mcp_servers``)
# --------------------------------------------------------------------------
MAX_MCP_ALIASES = 32
#: Aliases are lowercase alphabetic words of at most 16 characters; they name
#: the YAML key, the private ``.env`` file, and the client-side catalog key,
#: so the strictest useful alphabet is enforced everywhere.
MCP_ALIAS_PATTERN = re.compile(r"^[a-z]{1,16}$")
#
# Words owned by the Relay dispatcher itself. They are never usable as MCP
# server aliases: they identify fixed Relay operations (client.*, mcp.*,
# server-local tools), not third-party servers.
# ``relay`` would publish ``relay_*`` names that shadow the Relay tools.
RESERVED_MCP_ALIASES = frozenset({"client", "mcp", "relay", "server"})
MCP_ALIAS_ENV_DIRNAME = "mcp"
MCP_ENV_MAX_KEYS = 32
MCP_ENV_MAX_BYTES = DOTENV_MAX_BYTES
MAX_MCP_COMMAND_ITEMS = 8
MAX_MCP_COMMAND_ITEM_LENGTH = 512
MAX_MCP_SOURCE_LENGTH = 255
MAX_MCP_URL_LENGTH = 2048
MAX_MCP_VERSION_LENGTH = 64
#: Registry ids are lowercase reverse-DNS names (``io.example/author/server``).
MCP_SOURCE_PATTERN = r"^[a-z0-9]([a-z0-9._/-]{0,253}[a-z0-9])?$"
MCP_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$"
MCP_TOOL_NAME_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"
MAX_MCP_TOOL_FILTER_ITEMS = 128
MAX_MCP_TOOL_DESCRIPTION_LENGTH = 2048
_ALIAS_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_client_admin_setting(config_path: str | Path | None) -> bool:
    """Read ``admin`` from disk; fail-closed.

    Administration is an explicit opt-in: the setting is unlocked ONLY
    when the key is literally ``true`` in the YAML. A missing key,
    ``false``, an unreadable or invalid document all read as locked
    (``False``): absence of evidence never grants administration.
    """
    try:
        return _client_admin_setting(config_path)
    except ConfigError:
        return False


def _client_admin_setting(config_path: str | Path | None) -> bool:
    try:
        document = _load_yaml(_config_path(config_path))
    except Exception as exc:
        raise ConfigError("client configuration is unreadable") from exc
    issues: list[ValidationIssue] = []
    _validate_root(document, issues)
    if issues:
        raise ConfigError("unknown root key(s)")
    value = document.get("admin", False)
    if not isinstance(value, bool):
        raise ConfigError("admin must be a boolean")
    return value


def _validate_mcp_alias(alias: str) -> str:
    if not isinstance(alias, str) or not MCP_ALIAS_PATTERN.fullmatch(alias):
        raise ConfigError("MCP server alias must be 1-16 lowercase letters")
    if alias in RESERVED_MCP_ALIASES:
        raise ConfigError(f"MCP server alias '{alias}' is reserved")
    return alias


class ConfigError(ValueError):
    """A safe, user-facing configuration error with no secret interpolation."""


@dataclass(frozen=True)
class ValidationIssue:
    level: Literal["ERROR", "WARNING", "INFO"]
    message: str


@dataclass(frozen=True)
class ValidationReport:
    scope: str
    issues: tuple[ValidationIssue, ...]

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level == "ERROR")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level == "WARNING")

    @property
    def valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class ServerRuntime:
    settings: Any
    mcp_bind_host: str
    mcp_port: int
    client_bind_host: str
    client_port: int


def _set_overlay(document: dict[str, Any], path: str, value: object) -> None:
    current = document
    for part in path.split(".")[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            return
        current = child
    current[path.rsplit(".", 1)[-1]] = value


class ConfigModel(BaseModel):
    """Closed YAML schema with declarative process-environment overlays."""

    model_config = ConfigDict(extra="forbid", validate_default=True)
    env_fields: ClassVar[dict[str, str]] = {}
    csv_env_fields: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def from_sources(cls, raw: Mapping[str, Any], env: Mapping[str, str]):
        values = copy.deepcopy(dict(raw))
        environment_paths: set[str] = set()
        for env_name, path in cls.env_fields.items():
            if env_name in env:
                value: object = env[env_name]
                if env_name in cls.csv_env_fields:
                    value = [item.strip() for item in env[env_name].split(",") if item.strip()]
                _set_overlay(values, path, value)
                environment_paths.add(path)
        return cls.model_validate(values, context={"environment_paths": environment_paths})


class ClientIdentityConfig(ConfigModel):
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
    )

    @field_validator("id")
    @classmethod
    def valid_uuid(cls, value: str, info: ValidationInfo) -> str:
        environment_paths = (info.context or {}).get("environment_paths", set())
        if "identity.id" in environment_paths:
            return value
        try:
            uuid.UUID(value)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("identity must be a UUID") from exc
        return value


class McpToolOverride(ConfigModel):
    """Per-tool settings inside an entry's ``tools`` allowlist."""

    description: (
        Annotated[
            str, Field(min_length=1, max_length=MAX_MCP_TOOL_DESCRIPTION_LENGTH)
        ]
        | None
    ) = None


class McpServerEntry(ConfigModel):
    """One local MCP server alias entry; transport is derived, never stored."""

    source: (
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_MCP_SOURCE_LENGTH,
                pattern=MCP_SOURCE_PATTERN,
            ),
        ]
        | None
    ) = None
    command: (
        list[
            Annotated[
                str, Field(min_length=1, max_length=MAX_MCP_COMMAND_ITEM_LENGTH)
            ]
        ]
        | None
    ) = None
    url: (
        Annotated[str, Field(min_length=1, max_length=MAX_MCP_URL_LENGTH)] | None
    ) = None
    version: (
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_MCP_VERSION_LENGTH,
                pattern=MCP_VERSION_PATTERN,
            ),
        ]
        | None
    ) = None
    enabled: bool = True
    #: Optional allowlist: only these tools are published and callable.
    tools: dict[str, McpToolOverride] | None = None

    @field_validator("tools", mode="before")
    @classmethod
    def _tool_allowlist(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError("tools must map tool names to settings")
        if not 1 <= len(value) <= MAX_MCP_TOOL_FILTER_ITEMS:
            raise ValueError(
                f"tools must list 1 to {MAX_MCP_TOOL_FILTER_ITEMS} tool names"
            )
        for name in value:
            if not isinstance(name, str) or not re.fullmatch(
                MCP_TOOL_NAME_PATTERN, name
            ):
                raise ValueError("tools keys must be MCP tool names")
        # ``navigate:`` (null) and ``navigate: {}`` both mean "no override".
        return {name: {} if item is None else item for name, item in value.items()}

    def tool_filter(self) -> dict[str, str | None] | None:
        """The allowlist as ``{tool: description override or None}``."""
        if self.tools is None:
            return None
        return {name: item.description for name, item in self.tools.items()}

    @field_validator("command")
    @classmethod
    def _valid_command(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not 1 <= len(value) <= MAX_MCP_COMMAND_ITEMS:
            raise ValueError(
                f"command argv must hold 1 to {MAX_MCP_COMMAND_ITEMS} items"
            )
        first = value[0]
        if "/" in first and not first.startswith("/"):
            # A path-carrying executable must be absolute; bare names stay
            # resolvable through PATH. No shell is ever involved.
            raise ValueError("command executable must be absolute or resolvable")
        return value

    @field_validator("url")
    @classmethod
    def _valid_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = urlparse(value)
        except ValueError as exc:
            raise ValueError("url is invalid") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("url must use http:// or https://")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("url must not include userinfo")
        return value

    @model_validator(mode="after")
    def _exactly_one_entry_kind(self) -> McpServerEntry:
        kinds = sum(
            (self.source is not None, self.command is not None, self.url is not None)
        )
        if kinds != 1:
            raise ValueError(
                "entry requires exactly one of source, command, or url"
            )
        if self.version is not None and self.source is None:
            raise ValueError("version pin requires a source entry")
        return self

    @property
    def transport(self) -> Literal["stdio", "streamable_http"]:
        """Derived transport: source/command spawn stdio, url is streamable HTTP."""
        return "streamable_http" if self.url is not None else "stdio"

    def yaml_value(self) -> dict[str, Any]:
        """Return the stored YAML shape: explicit fields, no secrets, no nulls."""
        return self.model_dump(mode="json", exclude_none=True)


class ClientConfig(ConfigModel):
    identity: ClientIdentityConfig = Field(default_factory=ClientIdentityConfig)
    relay_url: str = "ws://127.0.0.1:8001/ws"
    workspace: str = "./workspace"
    mcp_servers: dict[str, McpServerEntry] = Field(default_factory=dict)
    # The single administration switch, fail-closed: only an explicit
    # ``true`` in the YAML unlocks the admin verbs (``load_client_admin_setting``).
    admin: Annotated[bool, Field(strict=True)] = False
    env_fields = {
        "RELAY_URL": "relay_url",
        "RELAY_CLIENT_ID": "identity.id",
        "RELAY_CLIENT_WORKSPACE": "workspace",
    }

    @field_validator("relay_url")
    @classmethod
    def valid_relay_url(cls, value: str) -> str:
        try:
            parsed = urlparse(value)
            parsed.port
        except ValueError as exc:
            raise ValueError("invalid relay URL") from exc
        if (
            parsed.scheme not in {"ws", "wss"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("relay URL must use ws:// or wss://")
        return value

    @field_validator("workspace")
    @classmethod
    def nonempty_workspace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("workspace must not be empty")
        return value

    @field_validator("mcp_servers")
    @classmethod
    def bounded_mcp_servers(
        cls, value: dict[str, McpServerEntry]
    ) -> dict[str, McpServerEntry]:
        if len(value) > MAX_MCP_ALIASES:
            raise ValueError(
                f"at most {MAX_MCP_ALIASES} MCP server aliases are supported"
            )
        for alias in value:
            _validate_mcp_alias(alias)
        return value

    def runtime_settings(self, *, token: str, config_path: Path) -> dict[str, Any]:
        identity = self.identity.id
        return {
            "server_url": self.relay_url,
            "client_id": identity,
            "client_token": token,
            "workspace": _relative_path(self.workspace, config_path),
        }


def configuration_keys(model: type[ConfigModel]) -> frozenset[str]:
    """Derive dotted CLI keys from the canonical model tree."""
    keys: set[str] = set()

    def walk(current: type[ConfigModel], prefix: str = "") -> None:
        for name, field in current.model_fields.items():
            path = f"{prefix}.{name}" if prefix else name
            if isinstance(field.annotation, type) and issubclass(field.annotation, ConfigModel):
                walk(field.annotation, path)
            else:
                keys.add(path)

    walk(model)
    return frozenset(keys)


_CONFIG_MODELS: dict[str, type[ConfigModel]] = {
    "client": ClientConfig,
}
_CONFIG_KEYS = {scope: configuration_keys(model) for scope, model in _CONFIG_MODELS.items()}


def _deep_merge(base: dict[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _default_document(scope: Literal["server", "client"]) -> dict[str, Any]:
    return _CONFIG_MODELS[scope]().model_dump(mode="json")


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        if os.name != "nt":
            raise
    _check_private_path(path, directory=True)


def _check_private_path(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ConfigError("configuration path must not be a symlink")
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode):
        raise ConfigError("configuration path has an invalid type")
    if os.name != "nt":
        if stat.S_IMODE(info.st_mode) not in ({0o700} if directory else {0o600}):
            raise ConfigError("configuration path is not private")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ConfigError("configuration path has an invalid owner")


def _check_parent_chain(path: Path) -> None:
    current = path.parent
    while True:
        if current.exists() or current.is_symlink():
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise ConfigError("configuration parent path must not be a symlink")
            except FileNotFoundError:
                pass
        if current.parent == current:
            return
        current = current.parent


def _assert_no_symlink(path: Path) -> None:
    _check_parent_chain(path)
    if path.is_symlink():
        raise ConfigError("configuration path must not be a symlink")


def _write_private_text(path: Path, content: str, *, overwrite: bool = True) -> None:
    _check_parent_chain(path)
    _ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        _check_private_path(path, directory=False)
        if not overwrite:
            raise ConfigError("refusing to overwrite an existing secret file")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content.rstrip("\r\n") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            if os.name != "nt":
                raise
        _check_private_path(path, directory=False)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def dotenv_path(path: str | Path | None) -> Path:
    """Return the private credential file next to the selected YAML file."""
    return _config_path(path).parent / DOTENV_FILENAME


def _read_private_file(path: Path) -> str:
    """Read a small private KEY=VALUE file with the shared file guards.

    Guards (symlink, regular file, size, permissions, owner, UTF-8) are
    the security boundary for credential files; parsing stays with the
    caller.
    """
    _assert_no_symlink(path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ConfigError(".env file is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(".env file is not a private regular file")
        if info.st_size > DOTENV_MAX_BYTES:
            raise ConfigError(".env file is too large")
        if os.name != "nt" and (
            stat.S_IMODE(info.st_mode) != 0o600
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        ):
            raise ConfigError(".env file is not private")
        raw = os.read(fd, DOTENV_MAX_BYTES + 1)
    except OSError as exc:
        raise ConfigError(".env file could not be read") from exc
    finally:
        os.close(fd)
    if len(raw) > DOTENV_MAX_BYTES:
        raise ConfigError(".env file is too large")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(".env file is not valid UTF-8") from exc


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read the MCP Relay dotenv file with the library parser.

    File-level guards stay ours (symlink, size, permissions, UTF-8);
    syntax handling (quotes, comments, escapes) is delegated to
    python-dotenv. Every key is returned — the file is a flat
    operator-controlled override source, not a closed list.
    """
    if not path.exists() and not path.is_symlink():
        return {}
    text = _read_private_file(path)
    parsed = dotenv_dotenv_values(stream=io.StringIO(text)) or {}
    values: dict[str, str] = {}
    for key, value in parsed.items():
        if not key or value is None:
            raise ConfigError(".env contains an invalid line")
        values[key] = value
    return values


#: Topology keys are the declared non-secret RELAY_* environment names: the
#: ones steering binds, ports, relay URL, workspace and bounds. Losing them to
#: a tolerated dotenv read error would silently fall back to defaults (e.g.
#: the loopback relay URL), so any error on a topology-carrying dotenv must
#: block startup. Token names are excluded: credentials keep their tolerant,
#: dedicated loaders and a missing token fails naturally anyway.
_DOTENV_TOKEN_KEYS = DOTENV_NEVER_EXPORTED


def read_dotenv_values(path: str | Path | None) -> dict[str, str]:
    """Read the private .env with the full file guards; missing file -> {}."""
    dotenv_file = dotenv_path(path)
    if not dotenv_file.exists() and not dotenv_file.is_symlink():
        return {}
    return _read_dotenv(dotenv_file)


def merge_dotenv_values(
    path: str | Path | None,
    updates: Mapping[str, str],
    *,
    force: bool = False,
) -> dict[str, str]:
    """Merge non-secret values into the private .env, preserving credentials.

    Onboarding writes server topology (RELAY_SERVER_*) here. Credential keys
    (``RELAY_MCP_TOKEN``, ``RELAY_CLIENT_TOKEN``) are refused as updates and
    are always preserved verbatim from the existing file, as is every other
    entry the operator wrote: absent ``force``, an existing key keeps its
    value; ``force`` overrides only the supplied update keys. Existing entry
    order is preserved and new keys are appended. Comments are not retained
    (the file is rewritten atomically via the shared private-write guards,
    0600, owner-checked). The merged result must only use declared RELAY_*
    names — the same fail-closed contract the startup loader enforces.
    """
    from .environment import UnknownRelayEnvironmentError, validate_relay_environment

    dotenv_file = dotenv_path(path)
    for key in updates:
        if key in DOTENV_NEVER_EXPORTED:
            raise ConfigError("credential keys are never written to the .env by the relay")
        if not _ALIAS_ENV_KEY_PATTERN.fullmatch(key):
            raise ConfigError("invalid .env key name")
    existing: dict[str, str] = {}
    order: list[str] = []
    if dotenv_file.exists() or dotenv_file.is_symlink():
        text = _read_private_file(dotenv_file)
        parsed = dotenv_dotenv_values(stream=io.StringIO(text)) or {}
        for key, value in parsed.items():
            if not key or value is None:
                raise ConfigError(".env contains an invalid line")
            if key not in existing:
                order.append(key)
            existing[key] = value
    merged = dict(existing)
    for key, value in updates.items():
        if force or key not in merged:
            merged[key] = value
            if key not in order:
                order.append(key)
    try:
        validate_relay_environment(merged)
    except UnknownRelayEnvironmentError as exc:
        raise ConfigError(str(exc)) from exc
    content = "".join(f"{key}={merged[key]}\n" for key in order)
    _write_private_text(dotenv_file, content)
    return merged


def _declared_topology_env_keys() -> frozenset[str]:
    """Declared RELAY_* env names minus the credential token names."""
    from .environment import environment_fields

    allowed: set[str] = set()
    for module_name, model_name in (
        (".client", "ClientSettings"),
        (".json_bounds", "SizeOverrideSettings"),
        (".server", "RelaySettings"),
    ):
        module = import_module(module_name, package=__package__)
        allowed.update(environment_fields(getattr(module, model_name)))
    return frozenset(allowed - _DOTENV_TOKEN_KEYS)


_TOPOLOGY_KEY_PATTERN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)")


def _dotenv_raw_preview(path: Path) -> str | None:
    """Best-effort bounded raw read used only for error classification.

    Deliberately bypasses the private-file guards (permissions, size, owner):
    the goal is to see which key names the operator wrote so a dotenv read
    failure can be classified as topology-carrying or tokens-only. Values are
    never used from this preview and never exported. Returns ``None`` when the
    file cannot be inspected at all (unreadable, symlinked, ...); callers must
    treat that inability as topology risk and fail closed.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        raw = os.read(fd, DOTENV_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return raw.decode("utf-8", errors="replace")


def _dotenv_carries_topology_keys(path: Path) -> bool:
    """Report whether a failing dotenv plausibly carries topology keys.

    A file that cannot be inspected at all is treated as topology-carrying:
    the safe default is to block startup rather than silently drop whatever
    the operator wrote.
    """
    text = _dotenv_raw_preview(path)
    if text is None:
        return True
    topology_keys = _declared_topology_env_keys()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _TOPOLOGY_KEY_PATTERN.match(stripped)
        if match and match.group(1) in topology_keys:
            return True
    return False


def _dotenv_effective_environment(
    config_path: Path,
    environ: Mapping[str, str],
    *,
    export: bool,
) -> tuple[dict[str, str], dict[str, str]]:
    """Merge dotenv fallbacks with ``environ`` and track per-key origins.

    Returns ``(effective, origins)``. ``effective`` is the shell > `.env` >
    defaults view used by the rest of the loader; ``origins`` maps every key
    present in ``effective`` to exactly ``"environment"`` (the value came
    from the supplied process-environment view, which always wins) or
    ``".env"`` (the value came from the dotenv file alone). Keys added later
    by bound resolution carry no origin and render as ``"default"``.

    When ``export`` is true, dotenv-only non-secret values are exported for
    process-global consumers such as diagnostics (without replacing a value
    already present in either the supplied environment view or
    ``os.environ``). Read-only callers such as ``config show`` pass
    ``export=False`` so showing a configuration never mutates the process
    environment. Previously the dotenv-only export used
    ``os.environ.setdefault`` directly, which both lost the origin of every
    merged key and made read-only paths mutate global state; the origin map
    returned here is the fix.

    Fail-closed topology rule: when the dotenv cannot be read cleanly
    (malformed line, too large, bad permissions/owner, unreadable) and the
    file carries topology keys — or cannot be inspected to prove otherwise —
    the ConfigError is re-raised naming the dotenv path. A silent tolerated
    read must never drop operator topology back onto defaults (loopback).
    Tokens-only erroneous dotenvs stay tolerated; the missing credentials
    fail naturally on their dedicated loaders.
    """
    from .environment import validate_relay_environment

    # Fail closed before any tolerant read: an unknown RELAY_* name in the
    # process environment or the adjacent .env blocks startup, naming the key
    # (never a value) — even when all credentials come from the process env.
    validate_relay_environment(environ)
    dotenv_file = dotenv_path(config_path)
    try:
        values = _read_dotenv(dotenv_file)
    except ConfigError as exc:
        if _dotenv_carries_topology_keys(dotenv_file):
            raise ConfigError(f"{dotenv_file}: {exc}") from exc
        values = {}
    validate_relay_environment(values)
    dotenv_environment = {
        key: value
        for key, value in values.items()
        if key not in DOTENV_NEVER_EXPORTED
    }
    effective = dict(dotenv_environment)
    effective.update(environ)
    # Origin tracking during the merge itself: an environment key keeps its
    # "environment" origin even when the dotenv also carries it, and a
    # dotenv-only key is exactly ".env".
    origins = {
        key: ("environment" if key in environ else ".env")
        for key in effective
    }
    if export:
        for key, value in dotenv_environment.items():
            if key not in environ:
                os.environ.setdefault(key, value)

    from .json_bounds import resolve_size_overrides

    resolve_size_overrides(effective)
    return effective, origins


def _apply_dotenv_environment(
    config_path: Path, environ: Mapping[str, str]
) -> dict[str, str]:
    """Merge non-secret dotenv fallbacks, then resolve bounds.

    ``environ`` is the loader's process-environment view (the real
    ``os.environ`` or an injected mapping). Its values win over `.env`; `.env`
    fills only absent non-secret keys. Export to ``os.environ`` is only a
    process-global convenience when the caller is genuinely reading the real
    process environment; an injected view (tests, alternate loaders) never
    mutates global state. Credentials remain on their dedicated
    loaders and are never exported. A dotenv read error may already have been
    tolerated because all required credentials came from ``environ``; that
    must not prevent valid shell bounds from being resolved.
    """
    export = environ is os.environ
    return _dotenv_effective_environment(
        config_path, environ, export=export
    )[0]


def _read_simple_key_values(
    path: Path, *, validate_key: Callable[[str], None]
) -> dict[str, str]:
    """Securely read one small private KEY=VALUE file with keyed validation."""
    text = _read_private_file(path)
    values: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "\x00" in line or "=" not in line:
            raise ConfigError(f".env line {line_number} is invalid")
        raw_key, raw_value = line.split("=", 1)
        key = raw_key.strip()
        value = raw_value.strip()
        if raw_key != key:
            raise ConfigError(f".env line {line_number} is invalid")
        try:
            validate_key(key)
        except ConfigError as exc:
            raise ConfigError(f".env line {line_number} is invalid") from exc
        if key in values:
            raise ConfigError(f".env key is duplicated: {key}")
        if not value:
            raise ConfigError(f".env value is empty: {key}")
        values[key] = value
    return values


# --------------------------------------------------------------------------
# Per-alias private .env files (never YAML, never tool results)
# --------------------------------------------------------------------------


def alias_dotenv_path(path: str | Path | None, alias: str) -> Path:
    """Return the private per-alias credential file next to the YAML file."""
    _validate_mcp_alias(alias)
    return (
        _config_path(path).parent / MCP_ALIAS_ENV_DIRNAME / f"{alias}{DOTENV_FILENAME}"
    )


def alias_cache_dir(path: str | Path | None, alias: str) -> Path:
    """Return the per-alias launcher cache directory in the relay home.

    Declarative launchers (``npx``/``uvx``) cache their downloads under
    ``<config dir>/mcp/<alias>`` (by default ``~/.mcp-relay/mcp/<alias>``):
    the same relay home parent that holds the per-alias ``.env`` credential
    files. The cache never lives inside the client workspace.
    """
    _validate_mcp_alias(alias)
    return _config_path(path).parent / MCP_ALIAS_ENV_DIRNAME / alias


def _validate_alias_env_key(key: str) -> None:
    if not _ALIAS_ENV_KEY_PATTERN.fullmatch(key):
        raise ConfigError(".env key is not allowed")


def write_alias_env(
    path: str | Path | None, alias: str, values: Mapping[str, str] | None
) -> None:
    """Replace the alias private .env with the given non-empty string values."""
    _validate_mcp_alias(alias)
    if values is None:
        values = {}
    if len(values) > MCP_ENV_MAX_KEYS:
        raise ConfigError("too many MCP server environment keys")
    encoded: dict[str, str] = {}
    for key, value in values.items():
        _validate_alias_env_key(key)
        if not isinstance(value, str) or not value or any(
            character in value for character in "\r\n\x00"
        ):
            raise ConfigError(".env value is invalid")
        encoded[key] = value
    content = "".join(f"{key}={encoded[key]}\n" for key in sorted(encoded))
    if len(content.encode("utf-8")) > MCP_ENV_MAX_BYTES:
        raise ConfigError("MCP server .env content is too large")
    _write_private_text(alias_dotenv_path(path, alias), content)


def read_alias_env(path: str | Path | None, alias: str) -> dict[str, str]:
    """Read the alias private .env; missing files are empty mappings."""
    alias_file = alias_dotenv_path(path, alias)
    if not alias_file.exists() and not alias_file.is_symlink():
        return {}
    return _read_simple_key_values(alias_file, validate_key=_validate_alias_env_key)


def has_token_source(
    path: str | Path | None, key: Literal["RELAY_MCP_TOKEN", "RELAY_CLIENT_TOKEN"],
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Report whether a non-empty process or dotenv token is available."""
    effective_env = os.environ if env is None else env
    if key in effective_env:
        return bool(effective_env[key])
    try:
        return key in _read_dotenv(dotenv_path(path))
    except ConfigError:
        return False


def _token_source_present(
    path: str | Path | None,
    key: Literal["RELAY_MCP_TOKEN", "RELAY_CLIENT_TOKEN"],
    *,
    env: Mapping[str, str],
) -> bool:
    """Report whether a source exists, including an explicitly empty env value."""
    return key in env or has_token_source(path, key, env=env)


def _write_config(path: Path, document: Mapping[str, Any]) -> None:
    _check_parent_chain(path)
    _ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        _check_private_path(path, directory=False)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            yaml.safe_dump(
                _serializable(document),
                stream,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _check_private_path(path, directory=False)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _serializable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value


def _load_yaml(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"configuration file does not exist: {path}")
        return {}
    _check_parent_chain(path)
    _check_private_path(path, directory=False)
    try:
        with path.open("r", encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream)
    except ConfigError:
        raise
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError("configuration file could not be read") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError("configuration root must be a mapping")
    return loaded


def _config_path(path: str | Path | None) -> Path:
    return (DEFAULT_CONFIG_PATH if path is None else Path(path)).expanduser()


def _relative_path(value: object, config_path: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("configuration path must be a non-empty string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    _assert_no_symlink(path)
    return path.resolve(strict=False)


def _effective_client(document: Mapping[str, Any], env: Mapping[str, str]) -> ClientConfig:
    raw = document
    if not isinstance(raw, Mapping):
        raise ConfigError("client configuration must be a mapping")
    try:
        return ClientConfig.from_sources(raw, env)
    except ValidationError as exc:
        raise ConfigError("client configuration is invalid") from exc


def _secret_value(
    document: Mapping[str, Any],
    scope: Literal["server", "client"],
    name: Literal["mcp", "client"],
    path: Path,
    env: Mapping[str, str],
) -> str:
    env_key = "RELAY_MCP_TOKEN" if name == "mcp" else "RELAY_CLIENT_TOKEN"
    if env_key in env:
        value = env[env_key]
    else:
        values = _read_dotenv(path.parent / DOTENV_FILENAME)
        try:
            value = values[env_key]
        except KeyError as exc:
            raise ConfigError(
                f"required token is unavailable; set {env_key} or create {DOTENV_FILENAME}"
            ) from exc
    if not value:
        raise ConfigError(f"required token is empty: {env_key}")
    _validate_token(value, env_key)
    return value


def _validate_token(value: str, name: str = "token") -> None:
    """Reject invalid Bearer credentials without interpolating their contents."""
    from .protocol import MAX_TOKEN_LENGTH, MIN_TOKEN_LENGTH

    if not (MIN_TOKEN_LENGTH <= len(value) <= MAX_TOKEN_LENGTH) or not all(
        33 <= ord(character) <= 126 for character in value
    ):
        raise ConfigError(
            f"{name} is invalid: must be 32–256 printable ASCII characters without spaces"
        )


def read_server_client_token(
    path: str | Path | None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Read the effective Server-to-Client credential without exposing its path."""
    config_path = _config_path(path)
    effective_env = os.environ if env is None else env
    return _secret_value({}, "server", "client", config_path, effective_env)


def validate_relay_url(value: str) -> None:
    """Validate a Relay WebSocket URL without exposing user input in errors."""
    try:
        ClientConfig.model_validate({"relay_url": value})
    except ValidationError as exc:
        raise ConfigError("client relay_url must be a ws:// or wss:// URL") from exc


def validate_client_transport(value: str) -> None:
    """Validate the structural Relay WebSocket URL contract."""
    validate_relay_url(value)


def _validate_root(document: Mapping[str, Any], report: list[ValidationIssue]) -> None:
    allowed = set(ClientConfig.model_fields)
    unknown = set(document) - allowed
    if unknown:
        report.append(ValidationIssue("ERROR", "unknown root key(s)"))


def _model_validation_issues(
    error: ValidationError, scope: Literal["server", "client"]
) -> list[ValidationIssue]:
    """Translate canonical model failures to stable, sanitized CLI messages."""
    messages: list[str] = []
    for detail in error.errors(include_url=False, include_input=False):
        path = ".".join(str(part) for part in detail["loc"])
        error_type = str(detail["type"])
        if error_type == "extra_forbidden":
            message = f"unknown {scope} configuration key: {path}"
        elif path.startswith("identity"):
            message = "client identity.id must be a UUID"
        elif path.startswith(MCP_SERVERS_PREFIX):
            message = f"client mcp_servers entry is invalid: {path}"
        elif path == "relay_url":
            message = "client relay_url must be a ws:// or wss:// URL"
        elif path == "workspace":
            message = "workspace is invalid (configuration path must be a non-empty string)"
        elif path.startswith("runtime"):
            message = "client runtime limits are invalid"
        else:
            message = f"client configuration field is invalid: {path}"
        if message not in messages:
            messages.append(message)
    return [ValidationIssue("ERROR", message) for message in messages]


def _validate_server(
    document: Mapping[str, Any], path: Path, env: Mapping[str, str], *, require: bool
) -> ValidationReport:
    try:
        runtime = load_server_runtime(path, env=env)
    except ConfigError as exc:
        return ValidationReport("server", (ValidationIssue("ERROR", str(exc)),))
    settings = runtime.settings
    return ValidationReport("server", (
        ValidationIssue("INFO", f"mcp={settings.mcp_bind_host}:{settings.mcp_port}"),
        ValidationIssue("INFO", f"client={settings.client_bind_host}:{settings.client_port}"),
    ))


def _validate_client(
    document: Mapping[str, Any],
    path: Path,
    env: Mapping[str, str],
    *,
    require: bool,
) -> ValidationReport:
    issues: list[ValidationIssue] = []
    _validate_root(document, issues)
    if issues:
        return ValidationReport("client", tuple(issues))
    raw = document
    try:
        section = ClientConfig.from_sources(raw, env)
    except ValidationError as exc:
        section = None
        issues.extend(_model_validation_issues(exc, "client"))

    if section is not None:
        parsed = urlparse(section.relay_url)
        issues.append(
            ValidationIssue(
                "INFO",
                "transport=ws:// (unencrypted; intended for local or trusted LAN use)"
                if parsed.scheme == "ws"
                else "transport=wss:// (TLS expected)",
            )
        )
        try:
            workspace = _relative_path(section.workspace, path)
            if workspace.is_symlink() or not workspace.is_dir():
                raise ConfigError("workspace must be an existing directory")
            issues.append(ValidationIssue("INFO", f"workspace={workspace}"))
        except ConfigError as exc:
            issues.append(ValidationIssue("ERROR", f"workspace is invalid ({exc})"))

    try:
        _secret_value(document, "client", "client", path, env)
        source = "environment" if "RELAY_CLIENT_TOKEN" in env else ".env"
        issues.append(ValidationIssue("INFO", f"client_token source={source}"))
    except ConfigError as exc:
        issues.append(ValidationIssue("ERROR", f"client token is unavailable ({exc})"))

    return ValidationReport("client", tuple(issues))


def validate_document(
    path: str | Path | None,
    scope: Literal["server", "client"],
    *,
    env: Mapping[str, str] | None = None,
    require: bool = True,
) -> ValidationReport:
    config_path = _config_path(path)
    effective_env = os.environ if env is None else env
    if scope == "server":
        return _validate_server({}, config_path, effective_env, require=require)
    try:
        document = _load_yaml(config_path)
    except ConfigError as exc:
        if require:
            raise
        # An existing but broken YAML file is never an environment-only Client.
        if config_path.exists() or config_path.is_symlink():
            return ValidationReport(scope, (ValidationIssue("ERROR", str(exc)),))
        effective_env = _dotenv_effective_environment(config_path, effective_env, export=False)[0]
        if not (effective_env.get("RELAY_URL") and effective_env.get("RELAY_CLIENT_WORKSPACE") and _token_source_present(path, "RELAY_CLIENT_TOKEN", env=effective_env)):
            return ValidationReport(scope, (ValidationIssue("ERROR", str(exc)),))
        document = _default_document("client")
    effective_env = _dotenv_effective_environment(config_path, effective_env, export=False)[0]
    return _validate_client(
        document,
        config_path,
        effective_env,
        require=require,
    )


def _invalid_configuration_message(scope: str, report: ValidationReport) -> str:
    """Render only sanitized validation errors for runtime startup failures."""
    details = "; ".join(issue.message for issue in report.errors)
    prefix = f"invalid {scope} configuration"
    return f"{prefix}: {details}" if details else prefix


def init_config(
    path: str | Path | None,
    scope: Literal["server", "client"],
    *,
    force: bool = False,
    token: str | None = None,
    env: Mapping[str, str] | None = None,
    relay_url: str | None = None,
    workspace: str | Path | None = None,
) -> Path:
    if scope != "client":
        raise ConfigError("server settings are environment-only")
    config_path = _config_path(path)
    effective_env = os.environ if env is None else env
    document = _load_yaml(config_path, required=False)
    _read_dotenv(config_path.parent / DOTENV_FILENAME)
    defaults = _default_document(scope)
    existing = document or None
    if scope == "client" and isinstance(existing, Mapping):
        unknown = set(existing) - ClientConfig.model_fields.keys()
        if unknown:
            raise ConfigError("unknown root key(s)")
    if force and isinstance(existing, Mapping):
        preserved = {
            key: copy.deepcopy(existing[key])
            for key in ("identity",)
            if key in existing
        }
        section = _deep_merge(defaults, preserved)
    else:
        section = _deep_merge(defaults, existing if isinstance(existing, Mapping) else {})
    if relay_url is not None:
        section["relay_url"] = relay_url
    if workspace is not None:
        section["workspace"] = str(workspace)
    # Validate all provided inputs and the effective source BEFORE creating the
    # workspace or writing YAML. An existing .env is operator-owned, read-only.
    if token is not None:
        _validate_token(token, "RELAY_CLIENT_TOKEN")
    if "RELAY_CLIENT_TOKEN" in effective_env:
        _validate_token(effective_env["RELAY_CLIENT_TOKEN"], "RELAY_CLIENT_TOKEN")
    elif token is None:
        _secret_value({}, "client", "client", config_path, effective_env)
    workspace = _relative_path(section["workspace"], config_path)
    _ensure_private_directory(workspace)
    section["workspace"] = _relative_config_value(section["workspace"], config_path)
    document = section
    _write_config(config_path, document)

    # Read-only contract: the token provided via --stdin/prompt is only
    # validated here; writing it into the .env is the operator's job.
    return config_path


def _relative_config_value(value: object, path: Path) -> str:
    if isinstance(value, str) and not Path(value).expanduser().is_absolute():
        return value
    return str(_relative_path(value, path))


def get_section(path: str | Path | None, scope: Literal["server", "client"]) -> dict[str, Any]:
    config_path = _config_path(path)
    document = _load_yaml(config_path)
    if scope != "client":
        raise ConfigError("server settings are environment-only")
    issues: list[ValidationIssue] = []
    _validate_root(document, issues)
    if issues:
        raise ConfigError("unknown root key(s)")
    section = document
    # Defaults fill absent YAML keys without writing them; admin stays locked.
    return copy.deepcopy(
        dict(_deep_merge(_default_document(scope), dict(section)))
    )


def _parse_value(value: str) -> Any:
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError as exc:
        raise ConfigError("value is not valid YAML") from exc


def _set_nested(section: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    current = section
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _delete_nested(section: dict[str, Any], key: str) -> None:
    parts = key.split(".")
    current: Any = section
    for part in parts[:-1]:
        if not isinstance(current, Mapping) or part not in current:
            return
        current = current[part]
    if isinstance(current, dict):
        current.pop(parts[-1], None)


def _canonical_key(scope: str, key: str) -> str:
    aliases = {
        ("client", "client_id"): "identity.id",
        ("client", "server_url"): "relay_url",
        ("client", "workspace_dir"): "workspace",
    }
    return aliases.get((scope, key), key)


def set_value(
    path: str | Path | None,
    scope: Literal["server", "client"],
    key: str,
    value: str,
) -> None:
    config_path = _config_path(path)
    document = _load_yaml(config_path)
    if scope != "client":
        raise ConfigError("server settings are environment-only")
    issues: list[ValidationIssue] = []
    _validate_root(document, issues)
    if issues:
        raise ConfigError("unknown root key(s)")
    section = copy.deepcopy(document)
    canonical = _canonical_key(scope, key)
    if scope == "client" and canonical.startswith(MCP_SERVERS_PREFIX):
        _set_mcp_servers_value(config_path, document, canonical, _parse_value(value))
        return
    if canonical not in _CONFIG_KEYS[scope]:
        raise ConfigError(f"unknown {scope} configuration key: {key}")
    parsed_value: Any = _parse_value(value)
    _set_nested(section, canonical, parsed_value)
    document = section
    _write_config(config_path, document)


def unset_value(path: str | Path | None, scope: Literal["server", "client"], key: str) -> None:
    if scope == "client" and key == "mcp_token":
        raise ConfigError("Client configuration only accepts client_token")
    config_path = _config_path(path)
    document = _load_yaml(config_path)
    if scope != "client":
        raise ConfigError("server settings are environment-only")
    issues: list[ValidationIssue] = []
    _validate_root(document, issues)
    if issues:
        raise ConfigError("unknown root key(s)")
    if key in {"mcp_token", "client_token"}:
        # Read-only contract: credentials live in the process environment
        # or the operator-owned .env; the relay never edits them.
        raise ConfigError(
            "tokens are not managed by the relay; remove "
            f"{'RELAY_MCP_TOKEN' if key == 'mcp_token' else 'RELAY_CLIENT_TOKEN'}"
            " from the environment or the .env file yourself"
        )
    canonical = _canonical_key(scope, key)
    if scope == "client" and canonical.startswith(MCP_SERVERS_PREFIX):
        _unset_mcp_servers_value(config_path, document, canonical)
        return
    if canonical not in _CONFIG_KEYS[scope]:
        raise ConfigError(f"unknown {scope} configuration key: {key}")
    section = copy.deepcopy(document)
    _delete_nested(section, canonical)
    # Keep the unset key absent, particularly the fail-closed admin switch.
    merged = _deep_merge(_default_document(scope), section)
    _delete_nested(merged, canonical)
    document = merged
    _write_config(config_path, document)


MCP_SERVERS_PREFIX = "mcp_servers"
_MCP_ENTRY_FIELDS = frozenset(
    {"source", "command", "url", "version", "enabled", "tools"}
)


def _mcp_client_section(document: Mapping[str, Any]) -> dict[str, Any]:
    issues: list[ValidationIssue] = []
    _validate_root(document, issues)
    if issues:
        raise ConfigError("unknown root key(s)")
    return copy.deepcopy(dict(document))


def _validated_mcp_entry(entry: Mapping[str, Any]) -> McpServerEntry:
    try:
        return McpServerEntry.model_validate(dict(entry))
    except ValidationError as exc:
        raise ConfigError("MCP server entry is invalid") from exc


def _mcp_entries_section(document: Mapping[str, Any]) -> dict[str, Any]:
    section = _mcp_client_section(document)
    raw = section.get(MCP_SERVERS_PREFIX, {})
    if not isinstance(raw, Mapping):
        raise ConfigError("client mcp_servers must be a mapping")
    return copy.deepcopy(dict(raw))


def _write_mcp_entries(
    config_path: Path,
    document: dict[str, Any],
    entries: Mapping[str, McpServerEntry],
) -> None:
    """Write the client section with validated entries; the model stays closed."""
    section = _mcp_client_section(document)
    section[MCP_SERVERS_PREFIX] = {
        alias: entry.yaml_value() for alias, entry in entries.items()
    }
    try:
        ClientConfig.model_validate(section)
    except ValidationError as exc:
        # Cross-entry bounds (alias count, alias names) live on the closed
        # model; every caller surfaces ConfigError, so the bound must never
        # escape as a bare pydantic error (CLI traceback, unstructured tool
        # failure). The write has not happened yet: fail-safe, file untouched.
        raise ConfigError("client mcp_servers configuration is invalid") from exc
    document = section
    _write_config(config_path, document)


def _require_mcp_document(config_path: Path) -> dict[str, Any]:
    document = _load_yaml(config_path)
    _mcp_client_section(document)
    return document


def mcp_entries(path: str | Path | None) -> dict[str, dict[str, Any]]:
    """Return the raw ``mcp_servers`` mapping (deep-copied, no secrets)."""
    config_path = _config_path(path)
    document = _require_mcp_document(config_path)
    raw = _mcp_entries_section(document)
    return {
        str(alias): copy.deepcopy(dict(entry)) if isinstance(entry, Mapping) else entry
        for alias, entry in raw.items()
    }


def _snapshot_alias_env(
    config_path: Path, alias: str
) -> tuple[bool, dict[str, str] | None]:
    """Best-effort pre-state of one alias .env: ``(existed, values-or-None)``."""
    env_file = alias_dotenv_path(config_path, alias)
    if not env_file.exists() and not env_file.is_symlink():
        return False, None
    try:
        return True, read_alias_env(config_path, alias)
    except ConfigError:
        # An unreadable pre-state cannot be restored; the rollback leaves it
        # untouched and the commit failure is reported unchanged.
        return True, None


def _rollback_alias_env(
    config_path: Path, alias: str, snapshot: tuple[bool, dict[str, str] | None]
) -> None:
    """Best-effort alias .env rollback after a failed commit; never raises.

    The YAML is never touched here: a failed ``_write_mcp_entries`` already
    leaves it intact. Absent-before means the freshly written file is an
    orphan and is removed; present-before means the previous credentials are
    restored.
    """
    try:
        existed, values = snapshot
        if not existed:
            alias_dotenv_path(config_path, alias).unlink(missing_ok=True)
        elif values is not None:
            write_alias_env(config_path, alias, values)
    except Exception:
        # The original commit error keeps priority; the rollback is silent.
        pass


def mcp_entry_add(
    path: str | Path | None,
    alias: str,
    entry: Mapping[str, Any],
    env: Mapping[str, str] | None,
) -> None:
    """Create one alias; strict conflict check, then the YAML write commits."""
    _validate_mcp_alias(alias)
    parsed = _validated_mcp_entry(entry)
    config_path = _config_path(path)
    document = _require_mcp_document(config_path)
    entries = _mcp_entries_section(document)
    if alias in entries:
        raise ConfigError("an MCP server alias already exists")
    # Credential material is validated and written before the commit point, so
    # a failure here leaves the configuration untouched (invariant 2). A
    # failed commit rolls the alias .env back best-effort: no orphan 0600
    # credential file survives without its YAML entry.
    env_snapshot = _snapshot_alias_env(config_path, alias)
    write_alias_env(config_path, alias, env)
    entries[alias] = parsed
    try:
        _write_mcp_entries(config_path, document, _validated_entries(entries))
    except Exception:
        _rollback_alias_env(config_path, alias, env_snapshot)
        raise


def mcp_entry_replace(
    path: str | Path | None,
    alias: str,
    entry: Mapping[str, Any],
    env: Mapping[str, str] | None,
) -> None:
    """Full-replace one existing alias entry (commit), leaving others alone."""
    _validate_mcp_alias(alias)
    parsed = _validated_mcp_entry(entry)
    config_path = _config_path(path)
    document = _require_mcp_document(config_path)
    entries = _mcp_entries_section(document)
    if alias not in entries:
        raise ConfigError("unknown MCP server alias")
    # Same pre-commit ordering as add; a failed commit restores the previous
    # alias credentials instead of leaving the replacement applied.
    env_snapshot = _snapshot_alias_env(config_path, alias)
    write_alias_env(config_path, alias, env)
    entries[alias] = parsed
    try:
        _write_mcp_entries(config_path, document, _validated_entries(entries))
    except Exception:
        _rollback_alias_env(config_path, alias, env_snapshot)
        raise


def mcp_entry_remove(path: str | Path | None, alias: str) -> None:
    """Remove one existing alias entry and its private .env file."""
    _validate_mcp_alias(alias)
    config_path = _config_path(path)
    document = _require_mcp_document(config_path)
    entries = _mcp_entries_section(document)
    if alias not in entries:
        raise ConfigError("unknown MCP server alias")
    del entries[alias]
    _write_mcp_entries(config_path, document, _validated_entries(entries))
    alias_dotenv_path(config_path, alias).unlink(missing_ok=True)


def mcp_entry_set_enabled(
    path: str | Path | None, alias: str, enabled: bool
) -> None:
    """Flip ``enabled`` on an existing alias; the entry itself is preserved."""
    _validate_mcp_alias(alias)
    config_path = _config_path(path)
    document = _require_mcp_document(config_path)
    entries = _mcp_entries_section(document)
    if alias not in entries:
        raise ConfigError("unknown MCP server alias")
    current = _validated_mcp_entry(entries[alias])
    current = current.model_copy(update={"enabled": enabled})
    entries[alias] = current
    _write_mcp_entries(config_path, document, _validated_entries(entries))


def _validated_entries(
    entries: Mapping[str, Any]
) -> dict[str, McpServerEntry]:
    validated: dict[str, McpServerEntry] = {}
    for alias, entry in entries.items():
        _validate_mcp_alias(alias)
        validated[alias] = (
            entry if isinstance(entry, McpServerEntry) else _validated_mcp_entry(entry)
        )
    return validated


def _set_mcp_servers_value(config_path: Path, document: dict[str, Any], key: str, value: Any) -> None:
    """Apply one dotted ``mcp_servers.<alias>[.<field>]`` CLI write."""
    parts = key.split(".")
    if len(parts) == 2:
        alias = _validate_mcp_alias(parts[1])
        if not isinstance(value, Mapping):
            raise ConfigError("MCP server entry must be a mapping")
        entries = _mcp_entries_section(document)
        entries[alias] = _validated_mcp_entry(value)
        _write_mcp_entries(config_path, document, _validated_entries(entries))
        return
    if len(parts) != 3:
        raise ConfigError(f"unknown client configuration key: {key}")
    alias = _validate_mcp_alias(parts[1])
    field = parts[2]
    if field not in _MCP_ENTRY_FIELDS:
        raise ConfigError(f"unknown client configuration key: {key}")
    entries = _mcp_entries_section(document)
    current = dict(entries.get(alias, {}))
    if field == "enabled" and not isinstance(value, bool):
        raise ConfigError("MCP server entry is invalid")
    current[field] = value
    parsed = _validated_mcp_entry(current)
    entries[alias] = parsed
    _write_mcp_entries(config_path, document, _validated_entries(entries))


def _unset_mcp_servers_value(
    config_path: Path, document: dict[str, Any], key: str
) -> None:
    """Remove one dotted ``mcp_servers.<alias>[.<field>]`` key safely."""
    parts = key.split(".")
    if len(parts) == 2:
        alias = _validate_mcp_alias(parts[1])
        mcp_entry_remove(config_path, alias)
        return
    if len(parts) != 3:
        raise ConfigError(f"unknown client configuration key: {key}")
    alias = _validate_mcp_alias(parts[1])
    field = parts[2]
    if field not in _MCP_ENTRY_FIELDS:
        raise ConfigError(f"unknown client configuration key: {key}")
    entries = _mcp_entries_section(document)
    if alias not in entries:
        raise ConfigError("unknown MCP server alias")
    current = dict(entries[alias])
    current.pop(field, None)
    # Removing a field may break the exactly-one rule; validate fail-safe and
    # leave the file untouched when the resulting entry would be invalid.
    _validated_mcp_entry(current)
    entries[alias] = current
    _write_mcp_entries(config_path, document, _validated_entries(entries))


def load_client_settings(
    path: str | Path | None,
    *,
    env: Mapping[str, str] | None = None,
) -> Any:
    config_path = _config_path(path)
    effective_env = _apply_dotenv_environment(
        config_path, os.environ if env is None else env
    )
    try:
        document = _load_yaml(config_path)
    except ConfigError:
        if not (
            effective_env.get("RELAY_URL")
            and effective_env.get("RELAY_CLIENT_WORKSPACE")
            and _token_source_present(path, "RELAY_CLIENT_TOKEN", env=effective_env)
        ):
            raise
        document = _default_document("client")
        document["identity"]["id"] = effective_env.get("RELAY_CLIENT_ID", str(uuid.uuid4()))
    report = _validate_client(
        document,
        config_path,
        effective_env,
        require=True,
    )
    if not report.valid:
        raise ConfigError(_invalid_configuration_message("client", report))
    section = _effective_client(document, effective_env)
    token = _secret_value(document, "client", "client", config_path, effective_env)
    from .client import ClientSettings

    return ClientSettings(**section.runtime_settings(token=token, config_path=config_path))


def load_server_runtime(path: str | Path | None, *, env: Mapping[str, str] | None = None) -> ServerRuntime:
    config_path = _config_path(path)
    effective_env = _apply_dotenv_environment(
        config_path, os.environ if env is None else env
    )
    values = dict(effective_env)
    values["RELAY_MCP_TOKEN"] = _secret_value({}, "server", "mcp", config_path, effective_env)
    values["RELAY_CLIENT_TOKEN"] = _secret_value({}, "server", "client", config_path, effective_env)
    from .server import RelaySettings

    try:
        settings = RelaySettings.from_environment(values)
    except UnknownRelayEnvironmentError:
        raise
    except ValueError as exc:
        raise ConfigError("invalid relay server configuration") from exc
    return ServerRuntime(
        settings=settings,
        mcp_bind_host=settings.mcp_bind_host, mcp_port=settings.mcp_port,
        client_bind_host=settings.client_bind_host, client_port=settings.client_port,
    )


def _raw_leaf_paths(value: Mapping[str, Any], prefix: str = "") -> set[str]:
    """Collect the dotted paths of every scalar leaf in a raw YAML section."""
    paths: set[str] = set()
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, Mapping):
            paths.update(_raw_leaf_paths(item, path))
        else:
            paths.add(path)
    return paths


def _nested_source_tree(entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Nest dotted leaf entries into stable, sorted YAML sections."""
    tree: dict[str, Any] = {}
    for path in sorted(entries):
        current = tree
        parts = path.split(".")
        for part in parts[:-1]:
            child = current.setdefault(part, {})
            if not isinstance(child, dict):
                raise ConfigError("configuration model nesting is invalid")
            current = child
        current[parts[-1]] = entries[path]
    return tree


def _bind_show_node(
    value: object, env_key: str, origins: Mapping[str, str]
) -> dict[str, Any]:
    """One listener bind address with provenance and LAN-exposure classification."""
    node: dict[str, Any] = {
        "value": value,
        "source": origins.get(env_key, "default"),
    }
    address = ip_address(str(value))
    if not address.is_loopback:
        kind = "wildcard" if address.is_unspecified else "specific"
        node["warning"] = f"⚠ LAN-exposed ({kind})"
    return node


def _port_show_node(
    value: object, env_key: str, origins: Mapping[str, str]
) -> dict[str, Any]:
    """One listener port with provenance (no exposure classification)."""
    return {"value": value, "source": origins.get(env_key, "default")}


def _server_show_section(
    effective_env: Mapping[str, str], origins: Mapping[str, str]
) -> dict[str, Any]:
    """Resolve the env-only server binds exactly as runtime would.

    The four bind fields are environment-only (never YAML), so they are
    resolved here through the same runtime model validation used at startup —
    with distinct placeholder credentials, so showing a configuration never
    depends on token availability and never loads the real tokens.
    """
    from .server import RelaySettings

    try:
        settings = RelaySettings.model_validate(
            {
                "client_token": "config-show-placeholder-client-credential",
                "mcp_token": "config-show-placeholder-mcp-credential",
                "mcp_bind_host": effective_env.get(
                    "RELAY_SERVER_MCP_HOST", "127.0.0.1"
                ),
                "mcp_port": int(effective_env.get("RELAY_SERVER_MCP_PORT", "8000")),
                "client_bind_host": effective_env.get(
                    "RELAY_SERVER_CLIENT_HOST", "127.0.0.1"
                ),
                "client_port": int(
                    effective_env.get("RELAY_SERVER_CLIENT_PORT", "8001")
                ),
            }
        )
    except ValueError as exc:
        raise ConfigError("invalid relay server configuration") from exc
    return {
        "mcp": {
            "bind_host": _bind_show_node(
                settings.mcp_bind_host, "RELAY_SERVER_MCP_HOST", origins
            ),
            "port": _port_show_node(
                settings.mcp_port, "RELAY_SERVER_MCP_PORT", origins
            ),
        },
        "client": {
            "bind_host": _bind_show_node(
                settings.client_bind_host, "RELAY_SERVER_CLIENT_HOST", origins
            ),
            "port": _port_show_node(
                settings.client_port, "RELAY_SERVER_CLIENT_PORT", origins
            ),
        },
    }


def _token_show_node(
    config_path: Path,
    environ: Mapping[str, str],
    key: Literal["RELAY_MCP_TOKEN", "RELAY_CLIENT_TOKEN"],
) -> dict[str, Any]:
    """Label one credential without ever showing its value or length.

    The mask is fixed-size regardless of the real token, so the output can
    never leak even the token's length. The shared Relay Client Token is
    labeled with its dual role: the server accepts it and the client
    presents it.
    """
    node: dict[str, Any] = {"value": "[REDACTED]"}
    if key in environ and environ[key]:
        node["present"] = True
        node["source"] = "environment"
        return node
    try:
        dotenv_present = key in _read_dotenv(dotenv_path(config_path))
    except ConfigError:
        dotenv_present = False
    if dotenv_present:
        node["present"] = True
        node["source"] = ".env"
    else:
        node["present"] = False
    return node


def _secrets_show_section(
    config_path: Path, environ: Mapping[str, str]
) -> dict[str, Any]:
    client_token = _token_show_node(config_path, environ, "RELAY_CLIENT_TOKEN")
    if client_token.get("present"):
        # The relay accepts this shared credential from clients and the
        # client presents it; the label names the role, never the value.
        client_token["role"] = "shared server-client credential"
    return {
        "mcp_token": _token_show_node(config_path, environ, "RELAY_MCP_TOKEN"),
        "client_token": client_token,
    }


def show_document(
    path: str | Path | None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return the complete effective configuration model with per-leaf sources.

    The result is a read-only, redacted view derived from the canonical Pydantic
    models: every supported dotted CLI/YAML path appears exactly once with its
    effective value and where that value came from (default, file, or
    environment). Tokens never enter the model tree and credentials are
    redacted with the same rendering rules as ``config get``.
    """
    config_path = _config_path(path)
    supplied_env = os.environ if env is None else env
    # Show resolves the same effective view as runtime (dotenv included) but
    # never exports dotenv values into the process environment: showing a
    # configuration is a read-only operation.
    effective_env, origins = _dotenv_effective_environment(
        config_path, supplied_env, export=False
    )
    try:
        document = _load_yaml(config_path, required=False)
    except ConfigError:
        raise
    issues: list[ValidationIssue] = []
    _validate_root(document, issues)
    if issues:
        raise ConfigError(
            "configuration is invalid: "
            + "; ".join(issue.message for issue in issues)
        )
    shown: dict[str, Any] = {"config_file": str(config_path)}
    for scope, model in _CONFIG_MODELS.items():
        raw = document
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ConfigError(f"{scope} configuration must be a mapping")
        try:
            effective = model.from_sources(raw, effective_env)
        except ValidationError as exc:
            details = "; ".join(
                issue.message for issue in _model_validation_issues(exc, scope)  # type: ignore[arg-type]
            )
            raise ConfigError(
                f"{scope} configuration is invalid" + (f": {details}" if details else "")
            ) from exc
        values = effective.model_dump(mode="json")
        file_paths = _raw_leaf_paths(raw)
        environment_paths = {
            dotted: origins[name]
            for name, dotted in model.env_fields.items()
            if name in origins
        }
        entries: dict[str, dict[str, Any]] = {}
        for dotted in configuration_keys(model):
            leaf_value: Any = values
            for part in dotted.split("."):
                leaf_value = leaf_value[part]
            if dotted in environment_paths:
                source = environment_paths[dotted]
            elif dotted in file_paths:
                source = "file"
            else:
                source = "default"
            if dotted == MCP_SERVERS_PREFIX and isinstance(leaf_value, Mapping):
                # Per-alias expansion: the mapping holds validated entries only,
                # so nothing credential-shaped can appear here.
                for alias, entry_value in sorted(leaf_value.items()):
                    if not isinstance(entry_value, Mapping):
                        continue
                    for field_name, field_value in sorted(entry_value.items()):
                        sub_path = f"{dotted}.{alias}.{field_name}"
                        entries[sub_path] = {
                            "value": redact_for_output(field_value, str(field_name)),
                            "source": "file" if sub_path in file_paths else "default",
                        }
                continue
            entries[dotted] = {
                "value": redact_for_output(leaf_value, dotted.rsplit(".", 1)[-1]),
                "source": source,
            }
        shown.update(_nested_source_tree(entries))
    shown["server"] = _server_show_section(effective_env, origins)
    shown["secrets"] = _secrets_show_section(config_path, supplied_env)
    return shown


def _is_secret_output_key(key: str) -> bool:
    """Mask relay tokens and credential-like keys in config show/get output.

    AGENTS.md: the Relay Client Token and the public MCP access token never
    appear in the YAML configuration nor in ``config show`` output. Secret
    material is stored in ``.env`` files, but the presentation layer keeps
    masking these keys whatever their value source is. (The 2026-09-07
    "retire secret-scan filters" decision applies to tool error messages
    and spawn diagnostics, not to this presentation boundary.)
    """
    normalized = key.lower()
    return normalized != "secrets" and (
        is_sensitive_query_key(key)
        or normalized == "key"
        or (
            any(
                marker in normalized
                for marker in ("token", "secret", "password", "api_key", "api-key", "apikey")
            )
            and not normalized.endswith("_file")
        )
    )


def _redact_url_query(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.query:
        return value
    query = "&".join(
        f"{name}=[REDACTED]" if separator and _is_secret_output_key(unquote_plus(name)) else part
        for part in parsed.query.split("&")
        for name, separator, _value in (part.partition("="),)
    )
    return parsed._replace(query=query).geturl()


def redact_for_output(value: Any, key: str = "") -> Any:
    """Return a recursively redacted copy suitable for safe presentation."""
    if _is_secret_output_key(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(child_key): redact_for_output(child_value, str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [redact_for_output(item, key) for item in value]
    if isinstance(value, str):
        return _redact_url_query(value)
    return value
