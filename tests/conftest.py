"""Shared fixtures: home isolation and protocol-bound mutation hygiene.

Every test runs with an isolated home directory, so a runtime command that
resolves ``~/.mcp-relay`` (log file, dotenv) never touches the real one.

The RELAY_MAX_* overrides are applied at runtime into ``json_bounds``
module globals (single override mechanism). Any test touching them must
leave the calibrated defaults behind for the rest of the suite.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import mcp_relay.diagnostics as diagnostics
import mcp_relay.json_bounds as jb

_ENV_KEYS = (
    "RELAY_MAX_TOOL_RESULT_BYTES",
    "RELAY_MAX_RESULT_NODES",
    "RELAY_MAX_WS_MESSAGE_BYTES",
)


@pytest.fixture(autouse=True)
def _isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
):
    # Path.home is patched as well as HOME/USERPROFILE: several tests replace
    # os.environ with a bare dict, and on Windows Path.home() then has no
    # variable to resolve from.
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: cls(home)))
    log_path = diagnostics._log_path
    yield home
    # A runtime main() opens a log file under the isolated home; close it so
    # later tests do not keep writing there.
    if diagnostics._log_path != log_path:
        diagnostics.set_log_file(log_path)


@pytest.fixture(autouse=True)
def _restore_protocol_bounds():
    yield
    for name, value in jb._DEFAULT_BOUNDS.items():
        setattr(jb, name, value)
    jb.APPLIED_SIZE_OVERRIDES.clear()
    # Direct pop, NOT monkeypatch.delenv: the loader exports .env entries
    # straight into os.environ, and a delenv here would register a monkeypatch
    # undo that re-installs the leaked value at fixture finalization.
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
