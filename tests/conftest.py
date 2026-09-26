"""Shared fixtures: protocol-bound mutation hygiene.

The RELAY_MAX_* overrides are applied at runtime into ``json_bounds``
module globals (single override mechanism). Any test touching them must
leave the calibrated defaults behind for the rest of the suite.
"""

from __future__ import annotations

import os

import pytest

import mcp_relay.json_bounds as jb

_ENV_KEYS = (
    "RELAY_MAX_TOOL_RESULT_BYTES",
    "RELAY_MAX_RESULT_NODES",
    "RELAY_MAX_WS_MESSAGE_BYTES",
)


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
