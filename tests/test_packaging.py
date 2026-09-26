from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from mcp_relay import cli

ROOT = Path(__file__).parents[1]


def test_no_cua_dependency_or_optional_extra_remains() -> None:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]

    requirements = list(project["dependencies"])
    for group in project.get("optional-dependencies", {}).values():
        requirements.extend(group)
    assert all(not requirement.startswith("cua-driver") for requirement in requirements)
    assert "optional-dependencies" not in project


def test_project_requires_python_314_or_newer() -> None:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]

    assert project["requires-python"] == ">=3.14"


def test_docker_runtime_contract_has_no_implicit_role_or_secret_build_inputs() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    instructions = [line.strip() for line in dockerfile.splitlines() if line.strip()]

    assert any(line == 'ENTRYPOINT ["mcp-relay"]' for line in instructions)
    assert any(line == "USER relay" for line in instructions)
    assert any(line == "WORKDIR /workspace" for line in instructions)
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "ca-certificates" in dockerfile
    assert re.search(r"\bapt-get install\b[^\n]*\bgit\b", dockerfile)
    assert "uv sync --frozen" in dockerfile
    assert "FROM python:3.14.4-slim-bookworm AS builder" in dockerfile
    assert "FROM python:3.14.4-slim-bookworm AS runtime" in dockerfile
    assert "COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/" in dockerfile
    assert not any(line.startswith("EXPOSE ") for line in instructions)
    assert not any(
        re.match(r"(?:ARG|ENV)\s+.*(?:SECRET|TOKEN|PASSWORD|API_KEY)", line, re.I)
        for line in instructions
    )


def test_docker_builder_copies_package_metadata_before_project_install() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    metadata_copy = "COPY README.md LICENSE ./"
    project_install = "RUN uv sync --frozen --no-dev --no-editable"

    assert dockerfile.count(metadata_copy) == 1
    assert dockerfile.count(project_install) == 1
    assert dockerfile.index(metadata_copy) < dockerfile.index(project_install)


def test_docker_build_context_keeps_lockfiles_and_excludes_local_state() -> None:
    ignored = set((ROOT / ".dockerignore").read_text().splitlines())

    assert {".git", ".env", ".venv", "tests/"} <= ignored
    assert "pyproject.toml" not in ignored
    assert "uv.lock" not in ignored


def test_cli_version_reports_package_metadata_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("mcp_relay.version.package_version", lambda: "9.9.9-test")

    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "mcp-relay 9.9.9-test"


def test_cli_version_falls_back_to_unknown_without_package_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("mcp_relay.version.package_version", lambda: None)

    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "mcp-relay unknown"


def _compose_document() -> dict:
    import yaml

    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_compose_publishes_both_listener_ports() -> None:
    """MCP on 8000 and WS on 8001 must both be published to the host."""
    ports = _compose_document()["services"]["relay-server"]["ports"]

    assert ports == ["8000:8000", "8001:8001"]


def test_compose_listener_environments_match_published_ports() -> None:
    """The active per-listener bind environments must match the published
    container ports; a drifted value would silently serve a listener
    off-host."""
    service = _compose_document()["services"]["relay-server"]
    published = dict(
        entry.split(":", 1) for entry in service["ports"] if ":" in entry
    )

    assert service["environment"]["RELAY_SERVER_MCP_PORT"] == published["8000"]
    assert service["environment"]["RELAY_SERVER_CLIENT_PORT"] == published["8001"]
    assert service["environment"]["RELAY_SERVER_MCP_HOST"] == "0.0.0.0"
    assert service["environment"]["RELAY_SERVER_CLIENT_HOST"] == "0.0.0.0"


def test_compose_uses_real_token_variable_names() -> None:
    """The code reads RELAY_MCP_TOKEN and RELAY_CLIENT_TOKEN (config.py).
    Any other token variable name is a phantom that fail-closes deployments.
    """
    environment = _compose_document()["services"]["relay-server"]["environment"]

    assert "RELAY_MCP_TOKEN" in environment
    assert "RELAY_CLIENT_TOKEN" in environment
    assert "RELAY_AGENT_TOKEN" not in environment


def test_compose_does_not_reattribute_removed_knobs() -> None:
    """Knobs removed during this branch must not resurface in Compose."""
    compose_text = (ROOT / "docker-compose.yml").read_text()
    assert "ALLOWED_HOSTS" not in compose_text
    assert "ALLOWED_ORIGINS" not in compose_text
    assert "v2/invoke" not in compose_text


def test_env_example_uses_real_token_variable_names() -> None:
    text = (ROOT / ".env.example").read_text()
    assert "RELAY_MCP_TOKEN=" in text
    assert "RELAY_CLIENT_TOKEN=" in text
    assert "RELAY_AGENT_TOKEN" not in text
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            _name, _, value = stripped.partition("=")
            assert value.startswith("[") and value.endswith("]"), (
                f"placeholder required for {_name}, got {value!r}"
            )
