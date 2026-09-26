"""Small, sanitized operator log helpers used by MCP Relay runtime code.

Runtime logging is intentionally minimal: one sanitized single-line message
per event. Two sinks exist: stderr, plus a default-on file sink for the
Server and Client runtime commands. The file sink always receives every
level (``DEBUG`` included) so an offline ``~/.mcp-relay/*.log`` file is a
complete diagnostic record; ``LOG_LEVEL`` (``DEBUG`` < ``INFO`` <
``WARNING`` < ``ERROR``, case-insensitive; default ``INFO``) gates
**stderr only**, so console verbosity stays under operator control without
touching the file.
The value is read on every emission so a level exported from a ``.env`` file
during startup still takes effect; diagnostics never imports the
configuration stack.
The file sink is process-global by design (single process, single sink); it
is configured once at startup via ``set_log_file`` — the Server feeds
``server.log`` and the Client ``client.log``, and a bare filename always
resolves under the relay home directory ``~/.mcp-relay`` — and is never
configured from the CLI. The file is opened eagerly (parent directories are
created) so it exists from startup even before the first logged message,
opened in append mode, and flushed per message. A minimal guard keeps
log-write errors from ever failing the relay: on the first filesystem error
the sink stops after exactly one stderr warning, and stderr keeps working.
File and stderr lines share the ISO-8601 UTC millisecond timestamp prefix
(``...Z [LEVEL] message``) since the logging contract unification.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Literal

LogLevel = Literal["INFO", "WARNING", "DEBUG", "ERROR"]

#: Severity ordering backing the ``LOG_LEVEL`` minimum-level gate.
_LOG_LEVEL_SEVERITY: dict[str, int] = {
    "DEBUG": 0,
    "INFO": 1,
    "WARNING": 2,
    "ERROR": 3,
}
_DEFAULT_LOG_LEVEL = "INFO"

# Runtime Server/Client commands log to fixed files in the relay home; a
# bare filename passed to set_log_file always resolves there, mirroring
# config.CONFIG_DIR_NAME without importing the configuration stack.
_RELAY_HOME_DIR_NAME = ".mcp-relay"


def _utc_timestamp() -> str:
    """Return the current time as ISO-8601 UTC with milliseconds (``...Z``)."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

_log_path: Path | None = None
_log_handle: IO[str] | None = None
_log_sink_failed = False


def set_log_file(path: str | os.PathLike[str] | None) -> None:
    """Configure the process file sink (single process, single sink).

    A bare filename (no directory component) resolves under ``~/.mcp-relay``,
    the fixed log destination of the runtime commands; a path with a directory
    component is used as given. Pass ``None`` to disable the file sink and
    restore stderr-only logging. The file itself is opened eagerly in append
    mode (parent directories are created) so it exists from startup even
    before the first logged message, and is flushed per message.
    """
    global _log_path, _log_handle, _log_sink_failed
    _close_log_handle()
    _log_sink_failed = False
    if path is None:
        _log_path = None
        return
    resolved = Path(path).expanduser()
    if resolved.parent == Path("."):
        resolved = Path.home() / _RELAY_HOME_DIR_NAME / resolved
    _log_path = resolved
    _open_log_file()


def close_log_file() -> None:
    """Close the file sink handle, keeping the configured path for later use."""
    _close_log_handle()


def _close_log_handle() -> None:
    global _log_handle
    if _log_handle is not None:
        try:
            _log_handle.close()
        except OSError:
            pass
        _log_handle = None


def _disable_file_sink() -> None:
    """Permanently stop file logging with exactly one stderr warning."""
    global _log_sink_failed
    _close_log_handle()
    if not _log_sink_failed:
        _log_sink_failed = True
        print(
            f"[WARNING] log file {_log_path} is not writable; "
            "continuing with stderr only",
            file=sys.stderr,
            flush=True,
        )


def _open_log_file() -> bool:
    """Open the configured file sink eagerly; False (plus one warning) on error."""
    global _log_handle
    if _log_path is None or _log_sink_failed:
        return False
    if _log_handle is not None:
        return True
    try:
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        _log_handle = _log_path.open("a", encoding="utf-8")
    except OSError:
        _disable_file_sink()
        return False
    return True


def format_line(level: str, message: str) -> str:
    """Return the single operator line shared by every sink and logger.

    ``2026-09-05T22:45:23.534Z [INFO] message`` — the format of file lines
    and of the unified Server logger; stderr uses it too since the logging
    contract unification.
    """
    return f"{_utc_timestamp()} [{level}] {message}"


def console_level() -> str:
    """Resolve ``LOG_LEVEL`` to a console minimum-level name (INFO default)."""
    return ("DEBUG", "INFO", "WARNING", "ERROR")[_minimum_level()]


def _write_log_line(line: str) -> None:
    """Best-effort append of one already-formatted line; never raises."""
    global _log_handle
    if _log_path is None or _log_sink_failed:
        return
    if not _open_log_file():
        return
    handle = _log_handle
    if handle is None:  # pragma: no cover - defensive, _open_log_file ensures this
        return
    try:
        handle.write(f"{line}\n")
        handle.flush()
    except (OSError, ValueError):
        _disable_file_sink()


def _minimum_level() -> int:
    """Resolve the ``LOG_LEVEL`` minimum severity (default INFO).

    Read on every emission so a level exported from a ``.env`` file during
    startup (after ``set_log_file``) still takes effect. Unknown values fail
    safe to the INFO default; configuration loading rejects them earlier.
    """
    raw = os.environ.get("LOG_LEVEL", _DEFAULT_LOG_LEVEL)
    severity = _LOG_LEVEL_SEVERITY.get(raw.strip().upper())
    if severity is None:
        return _LOG_LEVEL_SEVERITY[_DEFAULT_LOG_LEVEL]
    return severity


def write_file_line(line: str) -> None:
    """Append one already-formatted operator line to the process file sink.

    Public entry point for structured loggers (the Server runtime) that need
    to mirror their records into ``server.log`` without going through
    ``emit``. Never raises; a no-op when no file sink is configured.
    """
    _write_log_line(line)


def emit(level: LogLevel, message: str) -> None:
    """Write one already-sanitized runtime message to both sinks.

    The file sink receives every level — it is the complete diagnostic
    record. stderr drops lines below the resolved ``LOG_LEVEL``
    minimum. Both sinks carry the same timestamped operator line; every
    line stays single-line.
    """
    line = format_line(level, message)
    if _log_path is not None:
        _write_log_line(line)
    if _LOG_LEVEL_SEVERITY[level] < _minimum_level():
        return
    print(line, file=sys.stderr, flush=True)


def info(message: str) -> None:
    emit("INFO", message)


def warning(message: str) -> None:
    emit("WARNING", message)


def debug(message: str) -> None:
    """Emit a debug message to the file sink always, to stderr under DEBUG.

    Under the default ``LOG_LEVEL=INFO`` the line reaches only the
    ``.log`` file; with ``LOG_LEVEL=DEBUG`` it reaches both sinks.
    """
    emit("DEBUG", message)


def error(message: str) -> None:
    emit("ERROR", message)


__all__ = [
    "LogLevel",
    "close_log_file",
    "console_level",
    "debug",
    "emit",
    "error",
    "format_line",
    "info",
    "set_log_file",
    "warning",
    "write_file_line",
]
