"""The CLI configures the relay, not a removed static tool inventory."""
from __future__ import annotations

import pytest
import yaml

from mcp_relay import cli, config


@pytest.mark.parametrize("arguments", [
    ["tools", "list"],
    ["tools", "enable", "relay_sample_ping"],
    ["tools", "disable", "relay_sample_exec"],
    ["tools", "enable", "relay_server_status"],
    ["tools", "enable", "relay_registry_search"],
    ["config", "init", "client", "--tools", "relay_sample_echo"],
    ["config", "init", "client", "--no-tools"],
    ["onboard", "--tools", "relay_sample_echo"],
    ["onboard", "--no-tools"],
])
def test_static_tool_selection_flags_are_not_cli_options(tmp_path, arguments):
    path = tmp_path / "config.yaml"
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(path), *arguments])
    assert exc.value.code == 2
    assert not path.exists()


def test_tools_allowlist_is_not_a_settable_key(
    tmp_path, capsys, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", path)
    config.init_config(path, "client", token="synthetic-token-synthetic-credential-0000000000000000", env={})
    before = path.read_bytes()
    assert cli.main(["config", "set", "tools.allowlist", "relay_sample_echo"]) == 1
    assert "unknown client configuration key" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_a_tools_section_fails_validation_without_rewriting(tmp_path):
    path = tmp_path / "config.yaml"
    config.init_config(path, "client", token='synthetic-token-synthetic-credential-0000000000000000', env={})
    document = yaml.safe_load(path.read_text())
    document["tools"] = {"allowlist": ["relay_sample_echo"]}
    path.write_text(yaml.safe_dump(document))
    before = path.read_bytes()
    report = config.validate_document(path, "client", env={})
    assert not report.valid
    assert path.read_bytes() == before


def test_config_show_is_offline_and_has_no_synthetic_inventory(tmp_path):
    path = tmp_path / "config.yaml"
    config.init_config(path, "client", token='synthetic-token-synthetic-credential-0000000000000000', env={})
    shown = config.show_document(path, env={})
    assert "tools" not in shown
    assert "mcp_servers" not in shown
