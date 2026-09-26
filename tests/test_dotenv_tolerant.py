"""The .env file is a flat operator override surface (python-dotenv based).

Any entry except the relay credentials becomes a process environment
variable (shell wins). Syntax is delegated to python-dotenv; file-level
guards (symlink, size, permissions, UTF-8) stay ours.

The relay NEVER writes the .env: no key generation at init, no CLI
rotation. The file belongs to the operator (env in Docker Compose,
secrets manager, or hand-edited). Missing credentials fail closed with
an actionable message.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_relay import config


def _server_config(tmp_path: Path) -> tuple[Path, Path]:
    # Server settings are environment-only: there is no server YAML surface,
    # so the test plays the operator by creating the dotenv directly.
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    # The relay never writes the .env; tests play the operator role.
    dotenv = config_path.parent / ".env"
    dotenv.write_text(
        'RELAY_MCP_TOKEN=test-mcp-token-synthetic-credential-0000000000000000\nRELAY_CLIENT_TOKEN=test-client-token-synthetic-credential-0000000000000000\n',
        encoding="utf-8",
    )
    if os.name != "nt":
        dotenv.chmod(0o600)
    return config_path, dotenv


def test_any_non_secret_key_is_exported_without_overwriting_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, dotenv = _server_config(tmp_path)
    dotenv.write_text(
        dotenv.read_text(encoding="utf-8")
        + "RELAY_MAX_TOOL_RESULT_BYTES=1048576\n"
        + "SOME_FUTURE_FLAG=enabled\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("RELAY_MAX_TOOL_RESULT_BYTES", raising=False)
    monkeypatch.delenv("SOME_FUTURE_FLAG", raising=False)
    try:
        # The real process environment is passed, so dotenv-only non-secret
        # values are exported (export is enabled only for os.environ itself).
        config.load_server_runtime(config_path)
        assert os.environ["RELAY_MAX_TOOL_RESULT_BYTES"] == "1048576"
        assert os.environ["SOME_FUTURE_FLAG"] == "enabled"

        # An explicit process variable wins over the .env fallback.
        monkeypatch.setenv("SOME_FUTURE_FLAG", "from-compose")
        config.load_server_runtime(config_path)
        assert os.environ["SOME_FUTURE_FLAG"] == "from-compose"
    finally:
        # The loader exports .env entries straight into os.environ (that is
        # the behavior under test); pop them so the process environment is
        # left clean for the rest of the suite.
        os.environ.pop("RELAY_MAX_TOOL_RESULT_BYTES", None)
        os.environ.pop("SOME_FUTURE_FLAG", None)


def test_credentials_are_never_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, dotenv = _server_config(tmp_path)
    dotenv.write_text(
        dotenv.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.delenv("RELAY_MCP_TOKEN", raising=False)
    monkeypatch.delenv("RELAY_CLIENT_TOKEN", raising=False)
    config.load_server_runtime(config_path, env={})
    assert "RELAY_MCP_TOKEN" not in os.environ
    assert "RELAY_CLIENT_TOKEN" not in os.environ


def test_post_load_check_reports_missing_required_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, _ = _server_config(tmp_path)
    for token in ("RELAY_MCP_TOKEN", "RELAY_CLIENT_TOKEN"):
        monkeypatch.delenv(token, raising=False)
    # The .env holds the credentials, so startup succeeds — the post-load
    # check verifies the required keys are effectively resolvable.
    config.load_server_runtime(config_path, env={})

    # With no source at all the load fails closed, naming both channels.
    dotenv_path = config_path.parent / ".env"
    saved = dotenv_path.read_text(encoding="utf-8")
    dotenv_path.unlink()
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_server_runtime(config_path, env={})
    message = str(excinfo.value)
    assert "RELAY_MCP_TOKEN" in message
    dotenv_path.write_text(saved, encoding="utf-8")


def test_init_server_scope_is_rejected_and_writes_nothing(tmp_path: Path) -> None:
    """Server settings are environment-only: no server YAML init surface."""
    config_path = tmp_path / "config.yaml"
    with pytest.raises(config.ConfigError, match="environment-only"):
        config.init_config(config_path, "server", env={})
    assert not config_path.parent.exists() or not config_path.exists()


def test_missing_credentials_fail_closed_with_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No token anywhere: startup refuses and says how to provide one."""
    config_path, _ = _server_config(tmp_path)
    monkeypatch.delenv("RELAY_MCP_TOKEN", raising=False)
    monkeypatch.delenv("RELAY_CLIENT_TOKEN", raising=False)
    (config_path.parent / ".env").unlink()
    with pytest.raises(config.ConfigError, match="RELAY_MCP_TOKEN"):
        config.load_server_runtime(config_path, env={})


def test_foreign_lines_survive_a_relay_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator comments/keys are never touched: the relay only reads."""
    config_path, dotenv = _server_config(tmp_path)
    original = dotenv.read_text(encoding="utf-8")
    dotenv.write_text(
        "# operator comment\n" + original + "SOME_FUTURE_FLAG=1\n",
        encoding="utf-8",
    )
    config.load_server_runtime(config_path, env={})
    text = dotenv.read_text(encoding="utf-8")
    assert "# operator comment" in text
    assert "SOME_FUTURE_FLAG=1" in text


def test_quoted_values_are_parsed_by_the_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, dotenv = _server_config(tmp_path)
    dotenv.write_text(
        dotenv.read_text(encoding="utf-8") + 'SOME_FLAG="hello world"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("SOME_FLAG", raising=False)
    try:
        # Real process environment: quoted dotenv-only values are exported.
        config.load_server_runtime(config_path)
        assert os.environ["SOME_FLAG"] == "hello world"
    finally:
        # The export under test mutates the real process environment; pop
        # the key so the rest of the suite sees a clean environment.
        os.environ.pop("SOME_FLAG", None)


def test_invalid_line_fails_closed(tmp_path: Path) -> None:
    config_path, dotenv = _server_config(tmp_path)
    dotenv.write_text("NOT_A_VALID_LINE\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="invalid"):
        config.load_server_runtime(config_path, env={})
