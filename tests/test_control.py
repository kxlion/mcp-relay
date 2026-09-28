"""Client status and admin verbs: YAML commit first, closed refusals, safe logs."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from mcp_relay.config import alias_cache_dir, mcp_entries
from mcp_relay.control import Control
from mcp_relay.diagnostics import set_log_file
from mcp_relay.mcp_catalog import ClientCatalog
from mcp_relay.mcp_command import CommandError
from mcp_relay.mcp_hub import AliasLaunch, McpHub


class FakeTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.closed = False
        self.on_tools_changed = None

    async def list_tools(self, cursor: str | None = None) -> object:
        if self.fail:
            raise ConnectionError("synthetic failure")
        return {
            "tools": [
                {"name": "ping", "description": "ping", "inputSchema": {"type": "object"}}
            ]
        }

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> object:
        return {"content": [{"type": "text", "text": "pong"}]}

    async def close(self) -> None:
        self.closed = True


class Factory:
    def __init__(self, *, fail_aliases: frozenset[str] = frozenset()) -> None:
        self.launches: list[AliasLaunch] = []
        self.transports: list[FakeTransport] = []
        self.fail_aliases = fail_aliases

    def __call__(self, launch: AliasLaunch) -> FakeTransport:
        self.launches.append(launch)
        transport = FakeTransport(fail=launch.alias in self.fail_aliases)
        self.transports.append(transport)
        return transport


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {"relay_url": "wss://relay.example.test/ws", "workspace": str(tmp_path)}
        ),
        encoding="utf-8",
    )
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _control(
    config_path: Path, factory: Factory | None = None, *, admin: bool = True
) -> Control:
    hub = McpHub(
        config_path,
        config_path.parent,
        transport_factory=factory or Factory(),
        spawn_attempts=1,
        provider_timeout_seconds=2.0,
    )
    catalog = ClientCatalog()
    hub.bind_on_change(lambda: hub.publish_catalog(catalog))
    return Control(hub=hub, catalog=catalog, client_version="0.1.0", admin_enabled=admin)


_LOOP = asyncio.new_event_loop()


def _invoke(control: Control, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    # One loop for the whole module: the hub keeps watch tasks across calls.
    return _LOOP.run_until_complete(
        control.invoke(operation, arguments, request_id="req-1")
    )


def _refusal(control: Control, operation: str, arguments: dict[str, Any]) -> str:
    with pytest.raises(CommandError) as error:
        _invoke(control, operation, arguments)
    assert error.value.execution_state == "not_started"
    return error.value.code


def test_add_commits_yaml_starts_the_alias_and_redacts_env(config_path: Path) -> None:
    control = _control(config_path)
    result = _invoke(
        control,
        "mcp.add",
        {
            "alias": "tools",
            "entry": {
                "command": ["/opt/tool"],
                "tools": {"ping": {"description": "Ping."}},
                "env": {"API_KEY": "s3cret"},
            },
        },
    )
    assert result["status"] == "running"
    assert result["entry"]["tools"] == {"ping": {"description": "Ping."}}
    assert result["entry"]["env_keys"] == ["API_KEY"]
    assert "s3cret" not in str(result)
    assert "tools" in mcp_entries(config_path)


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"alias": "Bad1", "entry": {"url": "http://127.0.0.1:9/mcp"}}, "invalid_alias"),
        ({"alias": "mcp", "entry": {"url": "http://127.0.0.1:9/mcp"}}, "invalid_alias"),
        ({"alias": "two", "entry": {"url": "http://x/mcp", "command": ["/a"]}}, "invalid_entry"),
        ({"alias": "env", "entry": {"url": "http://x/mcp", "env": {"K": 1}}}, "invalid_entry"),
        ({"alias": "extra", "entry": {"url": "http://x/mcp"}, "more": 1}, "invalid_entry"),
    ],
)
def test_add_refusals_leave_the_yaml_untouched(
    config_path: Path, arguments: dict[str, Any], code: str
) -> None:
    before = config_path.read_text(encoding="utf-8")
    assert _refusal(_control(config_path), "mcp.add", arguments) == code
    assert config_path.read_text(encoding="utf-8") == before


def test_add_existing_alias_conflicts(config_path: Path) -> None:
    control = _control(config_path)
    _invoke(control, "mcp.add", {"alias": "one", "entry": {"command": ["/a"]}})
    assert _refusal(control, "mcp.add", {"alias": "one", "entry": {"command": ["/b"]}}) == "alias_conflict"


def test_spawn_failure_keeps_the_commit(config_path: Path) -> None:
    control = _control(config_path, Factory(fail_aliases=frozenset({"down"})))
    code = _refusal(control, "mcp.add", {"alias": "down", "entry": {"command": ["/a"]}})
    assert code in {"spawn_failed", "startup_budget_exhausted"}
    assert "down" in mcp_entries(config_path)


def test_modify_replaces_and_unknown_alias_is_refused(config_path: Path) -> None:
    factory = Factory()
    control = _control(config_path, factory)
    _invoke(control, "mcp.add", {"alias": "one", "entry": {"command": ["/a"]}})
    result = _invoke(control, "mcp.modify", {"alias": "one", "entry": {"command": ["/b"]}})
    assert result["entry"] == {"command": ["/b"], "enabled": True}
    assert factory.transports[0].closed is True
    assert _refusal(control, "mcp.modify", {"alias": "two", "entry": {"command": ["/b"]}}) == "alias_unknown"


def test_enable_disable_and_delete(config_path: Path) -> None:
    control = _control(config_path)
    _invoke(control, "mcp.add", {"alias": "one", "entry": {"command": ["/a"]}})
    cache = alias_cache_dir(config_path, "one")
    cache.mkdir(parents=True, exist_ok=True)
    disabled = _invoke(control, "mcp.disable", {"alias": "one"})
    assert disabled == {"alias": "one", "enabled": False, "runtime_state": "disabled"}
    assert _invoke(control, "mcp.disable", {"alias": "one"})["runtime_state"] == "disabled"
    assert _invoke(control, "mcp.delete", {"alias": "one"}) == {"alias": "one", "status": "deleted"}
    assert "one" not in mcp_entries(config_path)
    assert not cache.exists()
    assert _refusal(control, "mcp.delete", {"alias": "one"}) == "alias_unknown"
    assert _refusal(control, "mcp.enable", {"alias": 3}) == "invalid_alias"


def test_locked_admin_refuses_every_verb_before_any_mutation(config_path: Path) -> None:
    control = _control(config_path, admin=False)
    before = config_path.read_text(encoding="utf-8")
    for operation, arguments in (
        ("mcp.add", {"alias": "one", "entry": {"command": ["/a"]}}),
        ("mcp.modify", {"alias": "one", "entry": {"command": ["/a"]}}),
        ("mcp.delete", {"alias": "one"}),
        ("mcp.enable", {"alias": "one"}),
        ("mcp.disable", {"alias": "one"}),
    ):
        assert _refusal(control, operation, arguments) == "permission_denied"
    assert config_path.read_text(encoding="utf-8") == before


def test_status_reports_servers_without_paths_or_secrets(config_path: Path) -> None:
    control = _control(config_path, Factory(fail_aliases=frozenset({"down"})))
    _invoke(control, "mcp.add", {"alias": "up", "entry": {"command": ["/a"], "env": {"K": "s3cret"}}})
    with contextlib.suppress(CommandError):
        _invoke(control, "mcp.add", {"alias": "down", "entry": {"command": ["/b"]}})
    status = _invoke(control, "client.status", {})
    assert status["admin"] is True
    servers = {server["alias"]: server for server in status["mcp_servers"]}
    assert servers["up"]["runtime_state"] == "running"
    assert servers["up"]["published_tools"] == 1
    assert servers["down"]["runtime_state"] == "unavailable"
    assert servers["down"]["error"]["code"] in {"spawn_failed", "startup_budget_exhausted"}
    text = str(status)
    assert "s3cret" not in text and str(config_path.parent) not in text
    assert _refusal(control, "client.status", {"x": 1}) == "invalid_arguments"


def test_status_without_a_hub_is_available() -> None:
    control = Control(hub=None, catalog=ClientCatalog(), client_version="0.1.0")
    status = _invoke(control, "client.status", {})
    assert status["mcp_servers"] == [] and status["admin"] is False


@contextlib.contextmanager
def _admin_log() -> Iterator[Path]:
    handle = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
    handle.close()
    path = Path(handle.name)
    old_stderr, sys.stderr = sys.stderr, io.StringIO()
    set_log_file(path)
    try:
        yield path
    finally:
        set_log_file(None)
        sys.stderr = old_stderr


def _admin_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if "mcp.admin" in line]


def test_admin_outcomes_log_one_sanitized_line(config_path: Path) -> None:
    control = _control(config_path)
    with _admin_log() as path:
        _invoke(
            control,
            "mcp.add",
            {"alias": "cua", "entry": {"command": ["/opt/cua-driver"], "env": {"API_KEY": "s3cret"}}},
        )
        _refusal(control, "mcp.delete", {"alias": "Bad\nalias=x"})
        _invoke(control, "client.status", {})
    ok, refused = _admin_lines(path)
    assert "operation=mcp.add" in ok and "alias=cua" in ok and "result=ok" in ok
    assert "cua-driver" not in ok and "s3cret" not in ok and "API_KEY" not in ok
    assert "code=invalid_alias" in refused and "alias=" not in refused
