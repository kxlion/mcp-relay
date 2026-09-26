"""Authentication helpers for untrusted relay credentials."""

from __future__ import annotations

import secrets


def credentials_match(supplied: str, expected: str) -> bool:
    """Compare credentials in constant time without raising on malformed text."""
    try:
        return secrets.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        )
    except UnicodeError:
        return False
