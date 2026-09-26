"""Single source of truth for the installed mcp-relay package version.

The Relay Server reports this value to the Client inside the existing
authenticated ``registered`` handshake frame. It is the *package* version
declared by ``pyproject.toml`` for the installed build, never a value
supplied by the Client and never a protocol version.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version

PACKAGE_DISTRIBUTION = "mcp-relay"

VERSION_LABEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$"
VERSION_LABEL_MAX_LENGTH = 64

_version_label_re = re.compile(VERSION_LABEL_PATTERN)


def package_version() -> str | None:
    """Return the installed ``mcp-relay`` package version, or ``None``.

    ``None`` means the distribution metadata is unavailable (for example a
    non-installed source checkout); servers then omit the handshake field and
    Clients fall back to ``unknown``.
    """
    try:
        return version(PACKAGE_DISTRIBUTION)
    except PackageNotFoundError:
        return None


def bounded_version_label(value: object) -> str | None:
    """Return ``value`` when it is a bounded, version-safe label.

    Anything else — non-strings, empty or over-long values, or anything that
    could carry a filesystem path, credential, or shell text — is rejected so
    callers can omit the field or fall back to ``unknown``.
    """
    if not isinstance(value, str) or len(value) > VERSION_LABEL_MAX_LENGTH:
        return None
    if _version_label_re.fullmatch(value) is None:
        return None
    return value
