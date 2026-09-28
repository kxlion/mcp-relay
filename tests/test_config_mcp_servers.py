"""``mcp_servers`` configuration contracts."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from mcp_relay import cli, config
from mcp_relay.config import (
    MAX_MCP_ALIASES,
    ClientConfig,
    McpServerEntry,
    alias_dotenv_path,
    mcp_entries,
    mcp_entry_add,
    mcp_entry_remove,
    mcp_entry_replace,
    mcp_entry_set_enabled,
    read_alias_env,
    show_document,
    write_alias_env,
)


def _write_yaml(path: Path, document: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _client_yaml(path: Path, **section: object) -> Path:
    document: dict[str, object] = {
        "relay_url": "wss://relay.example.test/ws",
        "workspace": str(path.parent / "workspace"),
        **section,
    }
    return _write_yaml(path, document)


# --------------------------------------------------------------------------
# Entry model: closed schema, exactly one of source/command/url
# --------------------------------------------------------------------------


def test_entry_accepts_registry_source_with_optional_pin() -> None:
    entry = McpServerEntry.model_validate(
        {"source": "io.example/author/server", "version": "1.2.3"}
    )
    assert entry.source == "io.example/author/server"
    assert entry.version == "1.2.3"
    assert entry.enabled is True
    assert entry.transport == "stdio"


def test_entry_accepts_absolute_command_argv() -> None:
    entry = McpServerEntry.model_validate({"command": ["/absolute/path/to/driver"]})
    assert entry.command == ["/absolute/path/to/driver"]
    assert entry.transport == "stdio"


def test_entry_accepts_http_url_as_streamable_http() -> None:
    entry = McpServerEntry.model_validate({"url": "http://127.0.0.1:9000/mcp"})
    assert entry.url == "http://127.0.0.1:9000/mcp"
    assert entry.transport == "streamable_http"


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"source": "io.example/a/server", "command": ["/bin/tool"]},
        {"source": "io.example/a/server", "url": "https://example.test/mcp"},
        {"command": ["/bin/tool"], "url": "https://example.test/mcp"},
        {"source": "io.example/a", "command": ["/bin/tool"], "url": "https://x.test/mcp"},
    ],
)
def test_entry_requires_exactly_one_entry_kind(entry: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        McpServerEntry.model_validate(entry)


def test_entry_version_pin_requires_source() -> None:
    with pytest.raises(ValidationError):
        McpServerEntry.model_validate({"command": ["/bin/tool"], "version": "1.0.0"})
    with pytest.raises(ValidationError):
        McpServerEntry.model_validate({"url": "http://127.0.0.1:1/mcp", "version": "1"})


def test_entry_rejects_relative_command_paths() -> None:
    with pytest.raises(ValidationError):
        McpServerEntry.model_validate({"command": ["./relative/tool"]})
    with pytest.raises(ValidationError):
        McpServerEntry.model_validate({"command": ["sub/dir/tool"]})
    assert McpServerEntry.model_validate({"command": ["tool-on-path"]}) is not None


def test_entry_rejects_empty_or_oversized_command_argv() -> None:
    with pytest.raises(ValidationError):
        McpServerEntry.model_validate({"command": []})
    with pytest.raises(ValidationError):
        McpServerEntry.model_validate({"command": ["/bin/tool", ""]})
    with pytest.raises(ValidationError):
        McpServerEntry.model_validate(
            {"command": ["/bin/tool" for _ in range(9)]}
        )


def test_entry_rejects_non_http_url_schemes() -> None:
    for url in ("ftp://example.test/mcp", "file:///etc/passwd", "wss://example.test/ws"):
        with pytest.raises(ValidationError):
            McpServerEntry.model_validate({"url": url})


def test_entry_rejects_oversized_or_malformed_source() -> None:
    with pytest.raises(ValidationError):
        McpServerEntry.model_validate({"source": "a" * 256})
    with pytest.raises(ValidationError):
        McpServerEntry.model_validate({"source": "io example/server"})
    with pytest.raises(ValidationError):
        McpServerEntry.model_validate({"source": "-io.example/server"})


def test_entry_forbids_unknown_and_reserved_fields() -> None:
    with pytest.raises(ValidationError):
        McpServerEntry.model_validate(
            {"command": ["/bin/tool"], "transport": "stdio"}
        )
    with pytest.raises(ValidationError):
        McpServerEntry.model_validate({"command": ["/bin/tool"], "env": {"A": "b"}})


# --------------------------------------------------------------------------
# ClientConfig.mcp_servers: alias rules and bounds
# --------------------------------------------------------------------------


def test_client_config_accepts_valid_alias_mapping() -> None:
    model = ClientConfig.model_validate(
        {
            "relay_url": "wss://relay.example.test/ws",
            "mcp_servers": {
                "cua": {"command": ["/absolute/cua-driver"]},
                "gh": {"url": "http://127.0.0.1:9000/mcp"},
            },
        }
    )
    assert set(model.mcp_servers) == {"cua", "gh"}
    assert model.mcp_servers["cua"].command == ["/absolute/cua-driver"]


@pytest.mark.parametrize(
    "alias", ["CUA", "cua2", "cua_driver", "cua-driver", "", "çua", "c" * 17]
)
def test_client_config_rejects_invalid_aliases(alias: str) -> None:
    with pytest.raises(ValidationError):
        ClientConfig.model_validate(
            {"mcp_servers": {alias: {"command": ["/bin/tool"]}}}
        )


def test_client_config_bounds_the_alias_count() -> None:
    entries = {
        chr(ord("a") + index % 26) * 3 + chr(ord("a") + index // 26): {
            "command": ["/bin/tool"]
        }
        for index in range(MAX_MCP_ALIASES + 1)
    }
    with pytest.raises(ValidationError):
        ClientConfig.model_validate({"mcp_servers": entries})
    ok = dict(list(entries.items())[:MAX_MCP_ALIASES])
    assert ClientConfig.model_validate({"mcp_servers": ok}) is not None


def _alias_for_index(index: int) -> str:
    return chr(ord("a") + index % 26) * 3 + chr(ord("a") + index // 26)


def test_mcp_entry_add_reports_alias_overflow_as_config_error(tmp_path: Path) -> None:
    """The alias-count bound surfaces as ConfigError, not a pydantic leak.

    ``_write_mcp_entries`` revalidates the whole client section on the closed
    model; every caller (CLI and control tools) maps ``ConfigError`` to a
    user-facing failure, so the bound must never escape as a bare
    ``pydantic.ValidationError`` (CLI traceback / unstructured tool error).
    """
    config_path = _client_yaml(tmp_path / "config.yaml")
    for index in range(MAX_MCP_ALIASES):
        mcp_entry_add(config_path, _alias_for_index(index), {"command": ["/bin/tool"]}, None)
    with pytest.raises(config.ConfigError):
        mcp_entry_add(
            config_path, _alias_for_index(MAX_MCP_ALIASES), {"command": ["/bin/tool"]}, None
        )
    # Fail-safe: the YAML still holds exactly the original aliases.
    assert len(mcp_entries(config_path)) == MAX_MCP_ALIASES


@pytest.mark.parametrize("alias", ["client", "mcp", "server"])
def test_reserved_aliases_are_refused_by_every_mcp_entry_mutation(
    tmp_path: Path, alias: str
) -> None:
    """Reserved words are never usable as MCP aliases through the CRUD paths."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    entry = {"command": ["/bin/tool"]}

    with pytest.raises(config.ConfigError, match="reserved"):
        mcp_entry_add(config_path, alias, entry, None)
    with pytest.raises(config.ConfigError, match="reserved"):
        mcp_entry_replace(config_path, alias, entry, None)
    with pytest.raises(config.ConfigError, match="reserved"):
        mcp_entry_set_enabled(config_path, alias, False)
    with pytest.raises(config.ConfigError, match="reserved"):
        mcp_entry_remove(config_path, alias)
    # Fail-safe: nothing was written.
    assert mcp_entries(config_path) == {}


def test_client_config_mcp_servers_not_env_overridable() -> None:
    model = ClientConfig.from_sources(
        {"mcp_servers": {"cua": {"command": ["/bin/tool"]}}},
        {"RELAY_CLIENT_MCP_SERVERS": "nope"},
    )
    assert set(model.mcp_servers) == {"cua"}


# --------------------------------------------------------------------------
# Alias private .env (mode 0600, existing dotenv primitives)
# --------------------------------------------------------------------------


def test_alias_dotenv_round_trip_is_private_and_yaml_free(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    write_alias_env(config_path, "cua", {"API_KEY": "secret-value"})
    dotenv = alias_dotenv_path(config_path, "cua")
    assert dotenv == config_path.parent / "mcp" / "cua.env"
    assert dotenv.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(dotenv.stat().st_mode) == 0o600
    assert read_alias_env(config_path, "cua") == {"API_KEY": "secret-value"}
    # Values never enter the YAML document.
    assert "secret-value" not in config_path.read_text(encoding="utf-8")


def test_alias_dotenv_rejects_invalid_alias_and_values(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    with pytest.raises(config.ConfigError):
        write_alias_env(config_path, "CUA", {"A": "b"})
    with pytest.raises(config.ConfigError):
        write_alias_env(config_path, "cua", {"A": ""})
    with pytest.raises(config.ConfigError):
        write_alias_env(config_path, "cua", {"1BAD": "b"})


def test_alias_dotenv_rejects_oversized_file(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    dotenv = alias_dotenv_path(config_path, "cua")
    dotenv.parent.mkdir(parents=True, exist_ok=True)
    dotenv.write_text(f"BIG={'x' * 5000}\n", encoding="utf-8")
    dotenv.chmod(0o600)
    with pytest.raises(config.ConfigError):
        read_alias_env(config_path, "cua")
    # The writer also refuses to create oversized alias files.
    with pytest.raises(config.ConfigError):
        write_alias_env(config_path, "cua", {"BIG": "x" * 5000})


def test_alias_dotenv_rejects_non_private_file(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    write_alias_env(config_path, "cua", {"A": "b"})
    dotenv = alias_dotenv_path(config_path, "cua")
    dotenv.chmod(0o644)
    with pytest.raises(config.ConfigError):
        read_alias_env(config_path, "cua")


# --------------------------------------------------------------------------
# Mutation functions shared by CLI and control tools (one mutation layer)
# --------------------------------------------------------------------------


def test_mcp_entry_add_writes_yaml_atomically(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    mcp_entry_add(
        config_path, "cua", {"command": ["/absolute/cua-driver"]}, {"API_KEY": "v"}
    )
    entries = mcp_entries(config_path)
    assert entries["cua"] == {"command": ["/absolute/cua-driver"], "enabled": True}
    assert read_alias_env(config_path, "cua") == {"API_KEY": "v"}


def test_mcp_entry_add_rejects_conflicting_alias(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    mcp_entry_add(config_path, "cua", {"command": ["/bin/tool"]}, None)
    with pytest.raises(config.ConfigError, match="exists"):
        mcp_entry_add(config_path, "cua", {"url": "http://127.0.0.1:1/mcp"}, None)
    assert mcp_entries(config_path)["cua"] == {
        "command": ["/bin/tool"],
        "enabled": True,
    }


def test_mcp_entry_add_rejects_invalid_entry_without_writing(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    with pytest.raises(config.ConfigError):
        mcp_entry_add(config_path, "cua", {"command": ["/bin/tool"], "url": "x"}, None)
    assert "cua" not in mcp_entries(config_path)


def test_mcp_entry_add_requires_existing_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    with pytest.raises(config.ConfigError, match="does not exist"):
        mcp_entry_add(config_path, "cua", {"command": ["/bin/tool"]}, None)


def test_mcp_entry_replace_is_full_and_strict(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    mcp_entry_add(config_path, "cua", {"command": ["/bin/tool"]}, {"A": "1"})
    mcp_entry_replace(config_path, "cua", {"url": "http://127.0.0.1:9/mcp"}, {"B": "2"})
    entries = mcp_entries(config_path)
    assert entries["cua"] == {"url": "http://127.0.0.1:9/mcp", "enabled": True}
    assert read_alias_env(config_path, "cua") == {"B": "2"}
    with pytest.raises(config.ConfigError, match="unknown"):
        mcp_entry_replace(config_path, "ghost", {"command": ["/bin/tool"]}, None)


def test_mcp_entry_remove_is_strict_and_clears_env(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    mcp_entry_add(config_path, "cua", {"command": ["/bin/tool"]}, {"A": "1"})
    mcp_entry_remove(config_path, "cua")
    assert mcp_entries(config_path) == {}
    assert not alias_dotenv_path(config_path, "cua").exists()
    with pytest.raises(config.ConfigError, match="unknown"):
        mcp_entry_remove(config_path, "cua")


def _break_sibling_entry(config_path: Path) -> None:
    """Add an entry that fails the closed model at commit time."""
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    servers = document.setdefault("mcp_servers", {})
    servers["bad"] = {
        "command": ["/bin/a"],
        "url": "https://x.test/mcp",
    }
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    config_path.chmod(0o600)


def test_mcp_entry_add_rolls_back_orphan_env_on_failed_commit(tmp_path: Path) -> None:
    """A failed YAML commit leaves no orphan alias .env behind.

    The .env is written before the commit point; when the commit fails the
    freshly created credential file must be rolled back best-effort while the
    YAML stays untouched (invariant 2).
    """
    config_path = _client_yaml(tmp_path / "config.yaml")
    _break_sibling_entry(config_path)

    with pytest.raises(config.ConfigError):
        mcp_entry_add(config_path, "cua", {"command": ["/bin/cua"]}, {"API_KEY": "v"})

    assert not alias_dotenv_path(config_path, "cua").exists()
    assert "cua" not in mcp_entries(config_path)


def test_mcp_entry_replace_restores_previous_env_on_failed_commit(
    tmp_path: Path,
) -> None:
    """A failed commit does not leave the replacement credentials in place."""
    config_path = _client_yaml(tmp_path / "config.yaml")
    mcp_entry_add(config_path, "cua", {"command": ["/bin/cua"]}, {"A": "1"})
    _break_sibling_entry(config_path)

    with pytest.raises(config.ConfigError):
        mcp_entry_replace(
            config_path, "cua", {"url": "http://127.0.0.1:9/mcp"}, {"B": "2"}
        )

    assert read_alias_env(config_path, "cua") == {"A": "1"}
    assert "cua" in mcp_entries(config_path)


def test_mcp_entry_set_enabled_preserves_entry(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    mcp_entry_add(config_path, "cua", {"command": ["/bin/tool"]}, None)
    mcp_entry_set_enabled(config_path, "cua", False)
    entries = mcp_entries(config_path)
    assert entries["cua"]["enabled"] is False
    mcp_entry_set_enabled(config_path, "cua", True)
    assert mcp_entries(config_path)["cua"]["enabled"] is True
    with pytest.raises(config.ConfigError, match="unknown"):
        mcp_entry_set_enabled(config_path, "ghost", False)


# --------------------------------------------------------------------------
# CLI config get/set/unset coverage for the new keys
# --------------------------------------------------------------------------



def test_cli_set_unset_covers_mcp_servers_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "set", "mcp_servers.cua.command", '["/absolute/cua-driver"]']) == 0
    assert mcp_entries(config_path)["cua"]["command"] == ["/absolute/cua-driver"]
    assert cli.main(["config", "set", "mcp_servers.cua.enabled", "false"]) == 0
    assert mcp_entries(config_path)["cua"]["enabled"] is False
    assert cli.main(["config", "unset", "mcp_servers.cua"]) == 0
    assert mcp_entries(config_path) == {}


def test_cli_set_rejects_unknown_mcp_servers_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "set", "mcp_servers.cua.transport", "stdio"]) == 1


def test_cli_set_rejects_entry_that_would_be_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    mcp_entry_add(config_path, "cua", {"command": ["/bin/tool"]}, None)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", config_path)
    assert cli.main(["config", "unset", "mcp_servers.cua.command"]) == 1
    # Fail-safe: the file is untouched.
    assert mcp_entries(config_path)["cua"]["command"] == ["/bin/tool"]



# --------------------------------------------------------------------------
# config show: no leaks, per-alias rendering
# --------------------------------------------------------------------------


def test_show_document_renders_mcp_servers_without_secrets(tmp_path: Path) -> None:
    config_path = _client_yaml(tmp_path / "config.yaml")
    secret = "s3cret-env-value-xyz"
    mcp_entry_add(
        config_path, "cua", {"command": ["/absolute/cua-driver"]}, {"API_KEY": secret}
    )
    shown = show_document(config_path, env={})
    client_tree = shown
    serialized = yaml.safe_dump(client_tree)
    assert "mcp_servers" in serialized
    assert "/absolute/cua-driver" in serialized
    assert secret not in serialized
    mcp_tree = client_tree["mcp_servers"]
    assert mcp_tree["cua"]["command"]["value"] == ["/absolute/cua-driver"]
    assert mcp_tree["cua"]["command"]["source"] == "file"
    assert mcp_tree["cua"]["enabled"]["value"] is True
    assert "API_KEY" not in serialized


def test_validate_document_reports_broken_mcp_servers_entry(tmp_path: Path) -> None:
    config_path = _client_yaml(
        tmp_path / "config.yaml",
        mcp_servers={"cua": {"command": ["/bin/tool"], "url": "https://x.test/mcp"}},
    )
    report = config.validate_document(config_path, "client", env={})
    assert not report.valid
    assert any("mcp_servers" in issue.message for issue in report.errors)
