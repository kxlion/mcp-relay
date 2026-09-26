"""Behavioral tests for the optional diagnostics file sink."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcp_relay import diagnostics

FILE_LINE_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z) \[(INFO|WARNING|DEBUG)\] (.+)$"
)


@pytest.fixture(autouse=True)
def _isolated_file_sink() -> Iterator[None]:
    """Start and end every test with the default stderr-only sink."""
    diagnostics.set_log_file(None)
    yield
    diagnostics.set_log_file(None)


def test_emit_info_and_warning_reach_stderr_and_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "server.log"
    diagnostics.set_log_file(log)

    diagnostics.emit("INFO", "relay started")
    diagnostics.info("client connected")
    diagnostics.warning("reconnect scheduled")

    stderr_text = capsys.readouterr().err
    assert "[INFO] relay started" in stderr_text
    assert "[INFO] client connected" in stderr_text
    assert "[WARNING] reconnect scheduled" in stderr_text
    file_text = log.read_text(encoding="utf-8")
    assert "[INFO] relay started\n" in file_text
    assert "[INFO] client connected\n" in file_text
    assert "[WARNING] reconnect scheduled\n" in file_text


def test_file_sink_lines_are_timestamped_in_utc_iso_8601(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "server.log"
    diagnostics.set_log_file(log)

    diagnostics.info("timestamped line")

    stderr_line = capsys.readouterr().err.strip()
    match = FILE_LINE_PATTERN.fullmatch(stderr_line)
    assert match is not None
    stamp = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    assert stamp.utcoffset() == timedelta(0)
    assert match.group(2) == "INFO"
    assert match.group(3) == "timestamped line"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    match = FILE_LINE_PATTERN.fullmatch(lines[0])
    assert match is not None
    stamp = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    assert stamp.utcoffset() == timedelta(0)
    assert datetime.now(timezone.utc) - stamp < timedelta(minutes=5)
    assert match.group(2) == "INFO"
    assert match.group(3) == "timestamped line"


def test_file_sink_receives_every_level_regardless_of_level(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file sink is the complete record; the level gates stderr only."""
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    log = tmp_path / "client.log"
    diagnostics.set_log_file(log)

    diagnostics.debug("payload trace")
    diagnostics.info("client connected")
    diagnostics.warning("reconnect scheduled")

    stderr_text = capsys.readouterr().err
    file_text = log.read_text(encoding="utf-8")
    # File: every level, always.
    assert "[DEBUG] payload trace\n" in file_text
    assert "[INFO] client connected\n" in file_text
    assert "[WARNING] reconnect scheduled\n" in file_text
    # Stderr: default INFO gate — no debug line.
    assert "[INFO] client connected" in stderr_text
    assert "[WARNING] reconnect scheduled" in stderr_text
    assert "payload trace" not in stderr_text


def test_debug_level_surfaces_debug_on_stderr_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    log = tmp_path / "client.log"
    diagnostics.set_log_file(log)

    diagnostics.debug("payload trace")
    diagnostics.info("still visible")
    diagnostics.warning("still visible too")

    stderr_text = capsys.readouterr().err
    assert "[DEBUG] payload trace" in stderr_text
    assert "[INFO] still visible" in stderr_text
    assert "[WARNING] still visible too" in stderr_text
    file_text = log.read_text(encoding="utf-8")
    assert "[DEBUG] payload trace\n" in file_text
    assert "[INFO] still visible\n" in file_text
    assert "[WARNING] still visible too\n" in file_text


def test_warning_level_keeps_the_file_record_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    log = tmp_path / "client.log"
    diagnostics.set_log_file(log)

    diagnostics.info("hidden info")
    diagnostics.debug("hidden debug")
    diagnostics.warning("visible warning")

    stderr_text = capsys.readouterr().err
    assert "hidden info" not in stderr_text
    assert "hidden debug" not in stderr_text
    assert "[WARNING] visible warning" in stderr_text
    # The file keeps the complete record even at WARNING console verbosity.
    file_text = log.read_text(encoding="utf-8")
    assert "[INFO] hidden info\n" in file_text
    assert "[DEBUG] hidden debug\n" in file_text
    assert "[WARNING] visible warning\n" in file_text


def test_unwritable_log_file_keeps_stderr_working_with_a_single_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    diagnostics.set_log_file(tmp_path)  # a directory cannot be opened for append

    diagnostics.emit("INFO", "first message")

    first = capsys.readouterr().err
    assert "[INFO] first message" in first
    assert first.count("[WARNING]") == 1

    diagnostics.info("second message")

    second = capsys.readouterr().err
    assert "[INFO] second message" in second
    assert "[WARNING]" not in second


def test_bare_log_filename_resolves_under_mcp_relay_home(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    diagnostics.set_log_file(Path("client.log"))
    diagnostics.info("resolved under relay home")

    expected = tmp_path / ".mcp-relay" / "client.log"
    assert "[INFO] resolved under relay home" in capsys.readouterr().err
    assert "[INFO] resolved under relay home\n" in expected.read_text(encoding="utf-8")


def test_server_startup_feeds_the_default_log_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_relay import server

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(SystemExit):
        server.main(["--config", str(tmp_path / "missing.yaml")])

    diagnostics.info("server default log")

    expected = tmp_path / ".mcp-relay" / "server.log"
    match = FILE_LINE_PATTERN.fullmatch(expected.read_text(encoding="utf-8").strip())
    assert match is not None
    assert match.group(3) == "server default log"


def test_client_startup_feeds_the_default_log_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_relay import client

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(SystemExit):
        client.main(["--config", str(tmp_path / "missing.yaml")])

    diagnostics.info("client default log")

    expected = tmp_path / ".mcp-relay" / "client.log"
    match = FILE_LINE_PATTERN.fullmatch(expected.read_text(encoding="utf-8").strip())
    assert match is not None
    assert match.group(3) == "client default log"


def test_log_file_is_created_eagerly_at_configuration_time(tmp_path: Path) -> None:
    """set_log_file creates the file immediately, before any message is logged."""
    log = tmp_path / "server.log"

    diagnostics.set_log_file(log)

    assert log.exists()


def test_log_file_parent_directories_are_created(tmp_path: Path) -> None:
    log = tmp_path / "nested" / "deeper" / "server.log"
    diagnostics.set_log_file(log)

    diagnostics.info("nested sink")

    assert "[INFO] nested sink\n" in log.read_text(encoding="utf-8")


def test_without_a_log_file_stderr_remains_the_only_sink(
    capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostics.info("stderr only")

    assert "[INFO] stderr only" in capsys.readouterr().err


def test_error_level_reaches_stderr_and_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "server.log"
    diagnostics.set_log_file(log)

    diagnostics.error("relay failed hard")

    stderr_text = capsys.readouterr().err
    assert "[ERROR] relay failed hard" in stderr_text
    assert "[ERROR] relay failed hard\n" in log.read_text(encoding="utf-8")


def test_error_is_a_valid_console_log_level(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERROR suppresses lower levels on stderr but is itself always shown."""
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    log = tmp_path / "server.log"
    diagnostics.set_log_file(log)

    diagnostics.debug("hidden debug")
    diagnostics.info("hidden info")
    diagnostics.warning("hidden warning")
    diagnostics.error("fatal problem")

    stderr_text = capsys.readouterr().err
    assert "hidden debug" not in stderr_text
    assert "hidden info" not in stderr_text
    assert "hidden warning" not in stderr_text
    assert "[ERROR] fatal problem" in stderr_text
    file_text = log.read_text(encoding="utf-8")
    assert "[DEBUG] hidden debug\n" in file_text
    assert "[INFO] hidden info\n" in file_text
    assert "[WARNING] hidden warning\n" in file_text
    assert "[ERROR] fatal problem\n" in file_text


def test_console_level_resolves_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "error")
    assert diagnostics.console_level() == "ERROR"
