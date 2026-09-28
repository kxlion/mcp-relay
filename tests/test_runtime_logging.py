"""Runtime logging unification: uvicorn and MCP loggers through diagnostics.

These tests really apply the Server's logging setup (``dictConfig`` of
``_uvicorn_logging_config``) and emit records on the runtime loggers,
asserting the diagnostics contract: one line per event per sink (stderr
+ ``server.log`` via ``diagnostics.set_log_file``), the unified
UTC-ms ``[LEVEL]`` format, DEBUG only in the file, no duplicates after
re-applying the config, and identifiable uvicorn-origin lines.
"""

from __future__ import annotations

import contextlib
import io
import logging
import logging.config
import re
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType

import pytest

import mcp_relay.diagnostics as diagnostics_module
import mcp_relay.server as server_module

_RUNTIME_LOGGERS = (
    "",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "mcp",
    "mcp.server.streamable_http_manager",
    "mcp_relay",
    "mcp_relay.server",
)

_FORMAT = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z \[([A-Z]+)\] (.*)"


class _LoggingState:
    """Snapshot of the logging tree, restored on exit."""

    def __init__(self) -> None:
        self._snapshots: dict[str, tuple[list[logging.Handler], int, bool]] = {}
        self._root_level = logging.getLogger().level

    def __enter__(self) -> _LoggingState:
        for name in _RUNTIME_LOGGERS:
            lg = logging.getLogger(name)
            self._snapshots[name] = (lg.handlers[:], lg.level, lg.propagate)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for name, (handlers, level, propagate) in self._snapshots.items():
            lg = logging.getLogger(name)
            for handler in lg.handlers[:]:
                if handler not in handlers:
                    handler.close()
                    lg.removeHandler(handler)
            lg.handlers[:] = handlers
            lg.setLevel(level)
            lg.propagate = propagate
        logging.getLogger().setLevel(self._root_level)


@contextlib.contextmanager
def _applied_logging(tmp_path: Path, log_level_env: str = "INFO") -> Iterator[tuple[io.StringIO, Path]]:
    """Apply the server logging config with stderr and the file sink captured."""
    import os

    stderr = io.StringIO()
    log = tmp_path / "server.log"
    old_stderr = __import__("sys").stderr
    old_env = os.environ.get("LOG_LEVEL")
    os.environ["LOG_LEVEL"] = log_level_env
    __import__("sys").stderr = stderr
    try:
        logging.config.dictConfig(server_module._uvicorn_logging_config())
        diagnostics_module.set_log_file(log)
        yield stderr, log
    finally:
        diagnostics_module.set_log_file(None)
        __import__("sys").stderr = old_stderr
        if old_env is None:
            os.environ.pop("LOG_LEVEL", None)
        else:
            os.environ["LOG_LEVEL"] = old_env


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def test_uvicorn_and_mcp_loggers_each_reach_both_sinks_once(tmp_path: Path) -> None:
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, log):
        logging.getLogger("uvicorn.error").info("uvicorn error event")
        logging.getLogger("uvicorn.access").info("uvicorn access event")
        logging.getLogger("mcp.server.streamable_http_manager").info(
            "StreamableHTTP session manager started"
        )
        logging.getLogger("mcp_relay.server").info("relay runtime event")

    stderr_lines = _lines(stderr.getvalue())
    file_lines = _lines(log.read_text(encoding="utf-8"))

    # Exactly one line per event per destination.
    assert len(stderr_lines) == 4, stderr_lines
    assert len(file_lines) == 4, file_lines
    for line in stderr_lines + file_lines:
        assert re.fullmatch(_FORMAT, line), line


def test_uvicorn_origin_lines_are_identifiable(tmp_path: Path) -> None:
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, _log):
        logging.getLogger("uvicorn.error").info("Application startup complete")

    stderr_lines = _lines(stderr.getvalue())
    assert len(stderr_lines) == 1
    assert "uvicorn.error" in stderr_lines[0]
    assert "Application startup complete" in stderr_lines[0]


def test_debug_reaches_the_file_but_is_gated_on_stderr(tmp_path: Path) -> None:
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, log):
        logging.getLogger("mcp_relay.server").debug("debug-only event")

    file_lines = _lines(log.read_text(encoding="utf-8"))
    assert file_lines == [f"{file_lines[0].split(' [')[0]} [DEBUG] debug-only event"]
    assert "debug-only event" not in stderr.getvalue()


def test_no_duplicates_after_reapplying_the_config(tmp_path: Path) -> None:
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, log):
        logging.config.dictConfig(server_module._uvicorn_logging_config())
        logging.config.dictConfig(server_module._uvicorn_logging_config())
        logging.getLogger("uvicorn.error").info("single event")
        logging.getLogger("mcp").info("single mcp event")

    stderr_lines = _lines(stderr.getvalue())
    file_lines = _lines(log.read_text(encoding="utf-8"))
    assert stderr_lines == [
        f"{stderr_lines[0].split(' [')[0]} [INFO] uvicorn.error: single event",
        f"{stderr_lines[1].split(' [')[0]} [INFO] single mcp event",
    ]
    # Each event exactly once in the file despite the re-applied configs.
    assert file_lines == [
        f"{file_lines[0].split(' [')[0]} [INFO] uvicorn.error: single event",
        f"{file_lines[1].split(' [')[0]} [INFO] single mcp event",
    ]


def test_warning_level_message_keeps_its_visible_level(tmp_path: Path) -> None:
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, log):
        logging.getLogger("uvicorn.error").warning("watch out")

    stderr_lines = _lines(stderr.getvalue())
    file_lines = _lines(log.read_text(encoding="utf-8"))
    assert stderr_lines == [
        f"{stderr_lines[0].split(' [')[0]} [WARNING] uvicorn.error: watch out"
    ]
    assert file_lines == [
        f"{file_lines[0].split(' [')[0]} [WARNING] uvicorn.error: watch out"
    ]


@pytest.mark.parametrize(
    ("logger_name", "message"),
    [
        ("uvicorn", "uvicorn base event"),
        ("uvicorn.error", "uvicorn error event"),
        ("mcp", "mcp library event"),
        ("mcp_relay.server", "relay event"),
    ],
)
def test_each_runtime_logger_is_wired_through_the_bridge(
    tmp_path: Path, logger_name: str, message: str
) -> None:
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, log):
        logging.getLogger(logger_name).info(message)

    expected = f"{logger_name}: {message}" if logger_name.startswith("uvicorn") else message
    stderr_lines = _lines(stderr.getvalue())
    file_lines = _lines(log.read_text(encoding="utf-8"))
    assert stderr_lines == [f"{stderr_lines[0].split(' [')[0]} [INFO] {expected}"], stderr_lines
    assert file_lines == [f"{file_lines[0].split(' [')[0]} [INFO] {expected}"], file_lines


# ---------------------------------------------------------------------------
# Task 3: uvicorn.access records — HTTP status level mapping and sanitization
# ---------------------------------------------------------------------------

_ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'


def _emit_http_access(
    path: str,
    status: int,
    *,
    client: str = "127.0.0.1:5000",
    method: str = "GET",
    http_version: str = "1.1",
    exc_info: object = None,
) -> None:
    logging.getLogger("uvicorn.access").info(
        _ACCESS_FORMAT,
        client,
        method,
        path,
        http_version,
        status,
        exc_info=exc_info,  # type: ignore[arg-type]
    )


def _emit_ws_access(path: str, status: int | None) -> None:
    logger = logging.getLogger("uvicorn.access")
    if status is None:
        # uvicorn WS handshake accepted variant (no status code).
        logger.info('%s - "WebSocket %s" [accepted]', "10.0.0.9:51000", path)
    else:
        logger.info('%s - "WebSocket %s" %d', "10.0.0.9:51000", path, status)


def _level_of(line: str) -> str:
    match = re.match(_FORMAT, line)
    assert match is not None, line
    return match.group(1)


@pytest.mark.parametrize(
    ("status", "expected_level"),
    [
        (200, "INFO"),
        (302, "INFO"),
        (401, "WARNING"),
        (404, "WARNING"),
        (500, "ERROR"),
        (503, "ERROR"),
    ],
)
def test_access_status_maps_to_level_before_console_filtering(
    tmp_path: Path, status: int, expected_level: str
) -> None:
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, log):
        _emit_http_access("/mcp", status)

    stderr_lines = _lines(stderr.getvalue())
    file_lines = _lines(log.read_text(encoding="utf-8"))
    # The file sink always receives the access line.
    assert len(file_lines) == 1
    assert _level_of(file_lines[0]) == expected_level
    # stderr receives it only because LOG_LEVEL defaults to INFO here.
    assert len(stderr_lines) == 1
    assert _level_of(stderr_lines[0]) == expected_level


def test_access_level_classification_is_gated_on_stderr_by_log_level(tmp_path: Path) -> None:
    with _LoggingState(), _applied_logging(tmp_path, log_level_env="WARNING") as (stderr, log):
        _emit_http_access("/mcp", 200)
        _emit_http_access("/mcp", 500)

    stderr_lines = _lines(stderr.getvalue())
    # INFO access line is filtered out of stderr; the ERROR one is not.
    assert [_level_of(line) for line in stderr_lines] == ["ERROR"]
    file_lines = _lines(log.read_text(encoding="utf-8"))
    assert [_level_of(line) for line in file_lines] == ["INFO", "ERROR"]


def test_access_status_classification_comes_from_record_args_not_the_message(
    tmp_path: Path,
) -> None:
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, _log):
        # A message that *looks* like an ERROR access line, logged at INFO with
        # no args: the level must stay INFO (no string parsing of the message).
        logging.getLogger("uvicorn.access").info(
            'h - "GET /mcp HTTP/1.1" 500 OK'
        )

    stderr_lines = _lines(stderr.getvalue())
    assert len(stderr_lines) == 1
    assert _level_of(stderr_lines[0]) == "INFO"


@pytest.mark.parametrize(
    ("args", "raw"),
    [
        (("only-two", "args"), "only-two"),
        (("a", "b", "c", "d", "e", "f"), "f"),
        ("a plain string", "a plain string"),
        (None, None),
        ({"client": "x"}, "x"),
    ],
)
def test_incompatible_access_records_fall_back_to_a_safe_summary(
    tmp_path: Path, args: object, raw: object
) -> None:
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, log):
        logger = logging.getLogger("uvicorn.access")
        if isinstance(args, tuple) and len(args) == 2:
            logger.info("fmt %s", *args)
        elif args is None:
            logger.info("fmt")
        else:
            logger.info("fmt %r", args)

    stderr_lines = _lines(stderr.getvalue())
    file_lines = _lines(log.read_text(encoding="utf-8"))
    assert len(stderr_lines) == 1, stderr_lines
    assert len(file_lines) == 1
    # No crash, and no raw args dumped to either sink.
    for line in stderr_lines + file_lines:
        if raw is not None:
            assert raw not in line, line


def test_unexpected_access_record_does_not_crash_the_server(tmp_path: Path) -> None:
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, log):
        # Non-iterable args, unicode soup, deeply wrong shapes — none may raise.
        logging.getLogger("uvicorn.access").info("no args at all")
        logging.getLogger("uvicorn.access").info("%s %s", "one", "two")
        logging.getLogger("uvicorn.access").log(logging.INFO, "%d", object())

    assert len(_lines(stderr.getvalue())) == 3
    assert len(_lines(log.read_text(encoding="utf-8"))) == 3


def test_ws_access_format_maps_status_and_strips_the_query_string(tmp_path: Path) -> None:
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, log):
        _emit_ws_access("/ws", 101)
        _emit_ws_access("/ws", 403)
        _emit_ws_access("/ws", None)

    stderr_levels = [_level_of(line) for line in _lines(stderr.getvalue())]
    assert stderr_levels == ["INFO", "WARNING", "INFO"]
    file_lines = _lines(log.read_text(encoding="utf-8"))
    assert [_level_of(line) for line in file_lines] == ["INFO", "WARNING", "INFO"]
    # The WS path itself is kept readable (not a raw args dump).
    assert "WebSocket" in file_lines[0] or "WebSocket" in file_lines[2]


_SECRET_QS = "SECRETFORQUERY"
_SECRET_USERINFO = "SECRETFORUSERINFO"
_SECRET_HEADER = "SECRETFORHEADER"
_SECRET_BODY = "SECRETFORBODY"
_SECRET_EXC = "SECRETFORTEXT"


def test_access_lines_never_leak_sensitive_markers(tmp_path: Path) -> None:
    class _FakeResponse:
        headers = {"authorization": f"Bearer {_SECRET_HEADER}"}
        body = f'{{"error": "{_SECRET_BODY}"}}'

    with _LoggingState(), _applied_logging(tmp_path) as (stderr, log):
        request_state = {"headers": _FakeResponse.headers, "body": _FakeResponse.body}
        _emit_http_access(f"/mcp?token={_SECRET_QS}&api_key={_SECRET_QS}", 200)
        _emit_http_access(f"https://user:{_SECRET_USERINFO}@host/mcp", 200)
        _emit_http_access("/pa\r\nth\x1b", 200)
        _emit_http_access(
            "/mcp",
            500,
            exc_info=ValueError(f"boom {_SECRET_EXC}"),
        )
        assert request_state  # headers/body exist on the request object only

    stderr_text = stderr.getvalue()
    file_text = log.read_text(encoding="utf-8")
    for marker in (_SECRET_QS, _SECRET_USERINFO, _SECRET_HEADER, _SECRET_BODY, _SECRET_EXC):
        assert marker not in stderr_text, marker
        assert marker not in file_text, marker
    # Control characters are stripped from the rendered line, not passed raw.
    assert "\r" not in file_text and "\x1b" not in file_text


def test_mcp_iserror_content_does_not_upgrade_access_to_error(tmp_path: Path) -> None:
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, _log):
        # A transport-level 200 response whose MCP payload is an isError result
        # must stay INFO: the access log sees the HTTP status only.
        _emit_http_access("/mcp", 200)
        # Business-level errors surface via their own loggers at native levels.
        logging.getLogger("mcp_relay.server").error("mcp tool failed")

    stderr_lines = _lines(stderr.getvalue())
    assert [_level_of(line) for line in stderr_lines] == ["INFO", "ERROR"]


# ---------------------------------------------------------------------------
# Task 4: precise client-side mcp.command execution events
# ---------------------------------------------------------------------------


def _run_client_mcp_command_scenario(
    tmp_path: Path, request_id: str = "req-start"
) -> None:
    """One mcp.command through a real RelayClient session and a fake socket."""
    import asyncio
    import json

    from mcp_relay.client import ClientSettings, RelayClient
    from mcp_relay.mcp_catalog import AliasCatalog, ClientCatalog
    from mcp_relay.output_models import ProviderToolResult
    from mcp_relay.protocol import RELAY_CONTRACT
    from mcp_relay.provider_tools import ProviderToolDescriptor

    class Provider:
        async def call_tool(self, tool_name: str, arguments: object) -> ProviderToolResult:
            return ProviderToolResult(content=[], structuredContent={"ok": True})

    class Socket:
        def __init__(self, inbound: list[str]) -> None:
            self.inbound: asyncio.Queue[str] = asyncio.Queue()
            for item in inbound:
                self.inbound.put_nowait(item)
            self.sent: list[dict[str, object]] = []

        async def send(self, payload: str) -> None:
            self.sent.append(json.loads(payload))

        async def recv(self) -> str:
            return await self.inbound.get()

    async def scenario() -> None:
        catalog = ClientCatalog()
        catalog.update_alias(
            AliasCatalog(
                alias="sample",
                enabled=True,
                runtime_state="running",
                transport="stdio",
                catalog_available=True,
                error=None,
                descriptors=(
                    ProviderToolDescriptor(
                        provider_name="sample",
                        tool_name="click",
                        description="click",
                        input_schema={"type": "object"},
                    ),
                ),
                provider=Provider(),
            )
        )
        client = RelayClient(
            ClientSettings(
                server_url="ws://localhost/ws",
                client_id="d",
                client_token="client-synthetic-credential-0000000000000000",
                workspace=tmp_path,
            ),
            catalog=catalog,
        )
        socket = Socket(
            [
                json.dumps({"version": 1, "type": "registered", "client_id": "d", "server_version": "0.1.0", "relay_contract": RELAY_CONTRACT}),
                json.dumps(
                    {
                        "version": 2,
                        "type": "invoke",
                        "request_id": request_id,
                        "tool_name": "mcp.command",
                        "arguments": {"alias": "sample", "tool": "click", "arguments": {}},
                    }
                ),
            ]
        )
        task = asyncio.create_task(client.run_session(socket))
        for _ in range(500):
            if any(m.get("type") == "result" and m.get("request_id") == request_id for m in socket.sent):
                break
            await asyncio.sleep(0.002)
        client.stop()
        await task
        await client.aclose()

    asyncio.run(scenario())


def test_mcp_command_start_event_is_debug_file_only(tmp_path: Path) -> None:
    """The start event is DEBUG: file always, stderr only under LOG_LEVEL=DEBUG."""
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, log):
        (tmp_path / "info").mkdir()
        _run_client_mcp_command_scenario(tmp_path / "info")
    file_lines = [
        line
        for line in _lines(log.read_text(encoding="utf-8"))
        if "mcp.command start" in line
    ]
    stderr_lines = [
        line for line in _lines(stderr.getvalue()) if "mcp.command start" in line
    ]
    assert len(file_lines) == 1, file_lines
    assert stderr_lines == []
    assert re.fullmatch(_FORMAT, file_lines[0]), file_lines[0]
    assert "[DEBUG] mcp.command start" in file_lines[0]
    assert "request_id=req-start" in file_lines[0]
    assert "alias=sample tool=click" in file_lines[0]

    with _LoggingState(), _applied_logging(tmp_path, log_level_env="DEBUG") as (
        stderr,
        log,
    ):
        (tmp_path / "debug").mkdir()
        _run_client_mcp_command_scenario(tmp_path / "debug")
    stderr_lines = [
        line for line in _lines(stderr.getvalue()) if "mcp.command start" in line
    ]
    assert len(stderr_lines) == 1, stderr_lines


def test_mcp_command_done_event_reaches_each_sink_exactly_once(
    tmp_path: Path,
) -> None:
    """The client done event uses the unified format, once per sink."""
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, log):
        _run_client_mcp_command_scenario(tmp_path, "req-sink")

    stderr_lines = [line for line in _lines(stderr.getvalue()) if "mcp.command done" in line]
    file_lines = [line for line in _lines(log.read_text(encoding="utf-8")) if "mcp.command done" in line]
    assert len(stderr_lines) == 1, stderr_lines
    assert len(file_lines) == 1, file_lines
    for line in stderr_lines + file_lines:
        assert re.fullmatch(_FORMAT, line), line
        assert "[INFO] mcp.command done" in line
        assert "isError=false" in line


def test_admin_operation_event_uses_the_unified_format(tmp_path: Path) -> None:
    """Admin events ride the same unified [LEVEL] line, once per sink."""
    import asyncio

    from mcp_relay.control import Control
    from mcp_relay.mcp_catalog import ClientCatalog
    from mcp_relay.mcp_command import CommandError

    control = Control(hub=None, catalog=ClientCatalog(), client_version="0.1.0")
    with _LoggingState(), _applied_logging(tmp_path) as (stderr, log):
        with pytest.raises(CommandError) as refused:
            asyncio.run(
                control.invoke("mcp.delete", {"alias": "ghost"}, request_id="req-admin")
            )
    assert refused.value.code == "permission_denied"

    stderr_lines = [line for line in _lines(stderr.getvalue()) if "mcp.admin" in line]
    file_lines = [line for line in _lines(log.read_text(encoding="utf-8")) if "mcp.admin" in line]
    assert len(stderr_lines) == 1, stderr_lines
    assert len(file_lines) == 1, file_lines
    for line in stderr_lines + file_lines:
        assert re.fullmatch(_FORMAT, line), line
        assert "[ERROR] mcp.admin" in line
        assert "operation=mcp.delete" in line
        assert "request_id=req-admin" in line
        assert "alias=ghost" in line
        assert "code=permission_denied" in line
