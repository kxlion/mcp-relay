from __future__ import annotations

import pytest

from mcp_relay import cli
from mcp_relay.version import package_version


def test_no_arguments_prints_only_public_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert "usage: mcp-relay" in output
    for command in ("onboard", "server", "client", "config"):
        assert command in output


@pytest.mark.parametrize(
    "argv",
    [
        ["config", "show"],
        ["config", "get", "relay_url"],
        ["config", "set", "admin", "true"],
        ["config", "unset", "admin"],
        ["config", "validate"],
        ["onboard"],
        ["server"],
        ["client"],
    ],
)
def test_parser_binds_exact_public_command(argv: list[str]) -> None:
    assert callable(cli._parser().parse_args(argv).handler)


@pytest.mark.parametrize(
    "argv",
    [
        ["config", "get"], ["config", "set", "admin"],
        ["config", "unset"],
    ],
)
def test_incomplete_public_grammar_is_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(argv)
    assert error.value.code == 2


def test_help_and_version_are_top_level_only(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    assert "--version" in capsys.readouterr().out
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"mcp-relay {package_version()}"
    assert cli.main(["config", "--help"]) == 0
    output = capsys.readouterr().out
    for command in ("show", "get", "set", "unset", "validate"):
        assert command in output
    for argv in (["server", "--help"], ["client", "--version"]):
        with pytest.raises(SystemExit) as error:
            cli.main(argv)
        assert error.value.code == 2
