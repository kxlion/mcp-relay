from __future__ import annotations

import asyncio
import inspect

import pytest

from mcp_relay.mcp_results import native_result
from mcp_relay.output_models import ProviderToolResult
from mcp_relay.protocol import (
    RELAY_CONTRACT,
    Capabilities,
    ClientError,
    ClientResult,
    InvokeMessage,
    Progress,
    Register,
    Registered,
)
from mcp_relay.registry import (
    AuthenticationError,
    ClientAlreadyConnectedError,
    ClientBusyError,
    ClientOfflineError,
    DuplicateRequestError,
    LateResponseError,
    RelayRegistry,
    RemoteClientError,
    UnknownRequestError,
    UnsupportedToolError,
)
from mcp_relay.version import package_version


class FakeSocket:
    def __init__(self) -> None:
        self.messages: list[object] = []

    async def send_json(self, message: object) -> None:
        self.messages.append(message)


class BlockingCancelSocket(FakeSocket):
    """Hold cancellation delivery open to exercise the pending-cleanup window."""

    def __init__(self) -> None:
        super().__init__()
        self.cancel_started = asyncio.Event()
        self.release_cancel = asyncio.Event()

    async def send_json(self, message: object) -> None:
        await super().send_json(message)
        if isinstance(message, dict) and message.get("type") == "cancel":
            self.cancel_started.set()
            await self.release_cancel.wait()


class FailingInitialSendSocket(FakeSocket):
    async def send_json(self, message: object) -> None:
        if isinstance(message, dict) and message.get("type") == "invoke":
            raise RuntimeError("connection lost")
        await super().send_json(message)


class BlockingInitialSendSocket(FakeSocket):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send_json(self, message: object) -> None:
        if isinstance(message, dict) and message.get("type") == "invoke":
            self.started.set()
            await self.release.wait()
        await super().send_json(message)


class ConcurrentWriteSocket(FakeSocket):
    def __init__(self) -> None:
        super().__init__()
        self.active_writes = 0
        self.max_active_writes = 0

    async def send_json(self, message: object) -> None:
        self.active_writes += 1
        self.max_active_writes = max(self.max_active_writes, self.active_writes)
        await asyncio.sleep(0)
        await super().send_json(message)
        self.active_writes -= 1


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def provider_result(value: dict[str, object] | None = None) -> ProviderToolResult:
    return ProviderToolResult(content=[], structuredContent=value or {})


def register(registry: RelayRegistry, socket: FakeSocket) -> None:
    run(
        registry.register(
            socket,
            Register(version=1, type="register", client_id="one", relay_contract=RELAY_CONTRACT),
        )
    )


def declare_ping(registry: RelayRegistry, socket: FakeSocket) -> None:
    run(
        registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                tools=["sample.ping"],
                client_version="0.2.0",
            ),
        )
    )


def test_registry_retains_the_client_announcement() -> None:
    """The registry keeps the announced wire operations; no descriptors flow."""
    registry = RelayRegistry(client_id="one", client_token="client-token")
    socket = FakeSocket()
    register(registry, socket)
    run(
        registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                tools=["mcp.list", "mcp.command"],
                client_version="0.2.0",
            ),
        )
    )

    assert registry.announced_capabilities == frozenset({"mcp.list", "mcp.command"})
    assert not hasattr(registry, "announced_descriptors")


def ping(request_id: str) -> InvokeMessage:
    return InvokeMessage(
        version=2,
        type="invoke",
        request_id=request_id,
        tool_name="sample.ping",
        arguments={},
    )


def terminal(request_id: str, command_id: str = "pwd") -> InvokeMessage:
    return InvokeMessage(
        version=2,
        type="invoke",
        request_id=request_id,
        tool_name="sample.exec",
        arguments={"command_id": command_id},
    )


def test_invoke_signature_accepts_only_a_typed_message() -> None:
    signature = inspect.signature(RelayRegistry.invoke)
    assert list(signature.parameters) == [
        "self",
        "client_id",
        "message",
        "timeout_seconds",
    ]
    assert signature.parameters["message"].annotation == "InvokeMessage"


def test_registry_relays_bounded_arguments_without_schema_validation() -> None:
    """The driver remains the sole validator.

    The registry forwards bounded arguments even when they do not match the
    declared schema; dispatch to the Client proceeds and only the Client's
    answer (or the timeout) completes the invocation.
    """
    async def scenario() -> None:
        registry = RelayRegistry(client_id="one", client_token="client-token")
        socket = FakeSocket()
        await registry.register(
            socket, Register(version=1, type="register", client_id="one", relay_contract=RELAY_CONTRACT)
        )
        await registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                tools=["mcp.command"],
                client_version="0.2.0",
            ),
        )
        schema_nonconforming = InvokeMessage(
            version=2,
            type="invoke",
            request_id="nonconforming",
            tool_name="mcp.command",
            arguments={"alias": "custom", "tool": "echo", "arguments": {}, "catalog_revision": "r"},
        )
        with pytest.raises(TimeoutError):
            await registry.invoke("one", schema_nonconforming, 0.01)
        assert any(
            isinstance(message, dict) and message.get("type") == "invoke"
            for message in socket.messages
        )

    asyncio.run(scenario())


def test_registry_serializes_generic_v2_and_returns_provider_result() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(client_id="one", client_token="client-token")
        socket = FakeSocket()
        await registry.register(socket, Register(version=1, type="register", client_id="one", relay_contract=RELAY_CONTRACT))
        await registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                tools=["sample.ping"],
                relay_contract=RELAY_CONTRACT,
                client_version="0.2.0",
            ),
        )
        pending = asyncio.create_task(
            registry.invoke(
                "one",
                InvokeMessage(
                    version=2,
                    type="invoke",
                    request_id="generic",
                    tool_name="sample.ping",
                    arguments={},
                ),
                1,
            )
        )
        await asyncio.sleep(0)
        assert socket.messages[-1] == {
            "version": 2,
            "type": "invoke",
            "request_id": "generic",
            "tool_name": "sample.ping",
            "arguments": {},
        }
        expected = ProviderToolResult(content=[{"type": "text", "text": "ok"}])
        await registry.handle_result(
            ClientResult(version=2, type="result", request_id="generic", result=expected)
        )
        delivered = await pending
        # Tranche 4: the registry delivers the NATIVE MCP result (once),
        # not the bounded wire mirror.
        from mcp.types import CallToolResult  # noqa: PLC0415

        assert isinstance(delivered, CallToolResult)
        dumped = delivered.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert dumped["content"] == [{"type": "text", "text": "ok"}]
        assert dumped["isError"] is False

    asyncio.run(scenario())


def test_status_snapshot_is_safe_and_offline() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")

    snapshot = run(registry.status_snapshot())

    assert snapshot.client_id == "one"
    assert snapshot.connected is False
    assert snapshot.capabilities == ()
    assert snapshot.invocation_state == "idle"
    assert snapshot.progress is None
    assert snapshot.heartbeat_age_seconds is None
    assert snapshot.client_version is None


def test_status_snapshot_atomically_copies_connected_state() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    socket = FakeSocket()
    register(registry, socket)
    run(
        registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                tools=["sample.exec", "sample.ping"],
                client_version="0.2.0",
            ),
        )
    )

    snapshot = run(registry.status_snapshot())

    assert snapshot.client_id == "one"
    assert snapshot.connected is True
    assert snapshot.capabilities == ("sample.exec", "sample.ping")
    assert snapshot.invocation_state == "idle"
    assert snapshot.progress is None
    assert snapshot.heartbeat_age_seconds is not None
    assert snapshot.heartbeat_age_seconds >= 0
    # The announced bounded version is reported verbatim.
    assert snapshot.client_version == "0.2.0"
    assert set(vars(snapshot)) == {
        "client_id",
        "connected",
        "capabilities",
        "invocation_state",
        "progress",
        "heartbeat_age_seconds",
        "client_version",
        "connected_since",
        "last_disconnect_at",
        "last_disconnect_reason",
        "public_tools",
        "client_operations",
    }


def test_status_snapshot_reports_client_version() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    socket = FakeSocket()
    register(registry, socket)
    run(
        registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                tools=["sample.ping"],
                client_version="0.2.0",
            ),
        )
    )

    snapshot = run(registry.status_snapshot())

    assert snapshot.connected is True
    assert snapshot.client_version == "0.2.0"


def test_status_snapshot_captures_busy_progress_under_registry_lock() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(client_id="one", client_token="client-token")
        socket = FakeSocket()
        await registry.register(
            socket,
            Register(version=1, type="register", client_id="one", relay_contract=RELAY_CONTRACT),
        )
        await registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                tools=["sample.ping"],
                relay_contract=RELAY_CONTRACT,
                client_version="0.2.0",
            ),
        )
        pending = asyncio.create_task(
            registry.invoke("one", ping("snapshot"), 1)
        )
        await asyncio.sleep(0)
        await registry.handle_progress(
            Progress(
                version=2,
                type="progress",
                request_id="snapshot",
                progress=40,
            )
        )

        snapshot = await registry.status_snapshot()

        assert snapshot.invocation_state == "busy"
        assert snapshot.progress == 40
        await registry.handle_result(
            ClientResult(version=2, type="result", request_id="snapshot", result=provider_result())
        )
        assert await pending == native_result(provider_result())

    run(scenario())


def test_unknown_client_and_second_connection_are_rejected() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    socket = FakeSocket()
    with pytest.raises(AuthenticationError):
        run(
            registry.register(
                socket,
                Register(version=1, type="register", client_id="other", relay_contract=RELAY_CONTRACT),
            )
        )
    register(registry, socket)
    with pytest.raises(ClientAlreadyConnectedError):
        register(registry, FakeSocket())



def test_registry_starts_offline_without_a_preconfigured_client_identity() -> None:
    try:
        registry = RelayRegistry(client_token="client-token")
    except TypeError as exc:
        pytest.fail(f"server-side Client identity must be dynamic: {exc}")
        raise AssertionError("unreachable")

    snapshot = run(registry.status_snapshot())

    assert snapshot.client_id is None
    assert snapshot.connected is False
    assert snapshot.capabilities == ()


def test_registry_binds_first_identity_and_rejects_a_different_identity() -> None:
    try:
        registry = RelayRegistry(client_token="client-token")
    except TypeError as exc:
        pytest.fail(f"server-side Client identity must be dynamic: {exc}")
        raise AssertionError("unreachable")
    first_socket = FakeSocket()
    first = Register.model_construct(version=1, type="register", client_id="one")
    run(registry.register(first_socket, first))
    assert run(registry.status_snapshot()).client_id == "one"
    run(registry.disconnect(first_socket))

    second_socket = FakeSocket()
    second = Register.model_construct(version=1, type="register", client_id="two")
    with pytest.raises(AuthenticationError):
        run(registry.register(second_socket, second))


def test_every_tool_requires_a_declared_capability() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    socket = FakeSocket()
    register(registry, socket)

    with pytest.raises(UnsupportedToolError, match="sample.ping"):
        run(registry.invoke("one", ping("ping"), 1))
    with pytest.raises(UnsupportedToolError, match="sample.exec"):
        run(registry.invoke("one", terminal("a"), 1))

    run(
        registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                tools=["sample.ping", "sample.exec"],
                client_version="0.2.0",
            ),
        )
    )

    async def scenario() -> None:
        pending = asyncio.create_task(
            registry.invoke("one", terminal("a"), 1)
        )
        await asyncio.sleep(0)
        assert socket.messages[-1] == {
            "version": 2,
            "type": "invoke",
            "request_id": "a",
            "tool_name": "sample.exec",
            "arguments": {"command_id": "pwd"},
        }
        await registry.handle_result(
            ClientResult(version=2, type="result", request_id="a", result=provider_result())
        )
        assert await pending == native_result(provider_result())

    run(scenario())


def test_offline_busy_correlated_and_unknown_results() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    with pytest.raises(ClientOfflineError):
        run(registry.invoke("one", ping("a"), 1))
    socket = FakeSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def scenario() -> None:
        first = asyncio.create_task(registry.invoke("one", ping("a"), 1))
        await asyncio.sleep(0)
        with pytest.raises(ClientBusyError):
            await registry.invoke("one", ping("b"), 1)
        await registry.handle_result(
            ClientResult(version=2, type="result", request_id="a", result=provider_result({"ok": True}))
        )
        assert await first == native_result(provider_result({"ok": True}))
        with pytest.raises(UnknownRequestError):
            await registry.handle_result(
                ClientResult(version=2, type="result", request_id="missing", result=provider_result())
            )

    run(scenario())


def test_duplicate_timeout_cancellation_and_disconnect_always_clean_up() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    socket = FakeSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def scenario() -> None:
        task = asyncio.create_task(
            registry.invoke("one", ping("a"), 0.01)
        )
        await asyncio.sleep(0)
        with pytest.raises(DuplicateRequestError):
            await registry.invoke("one", ping("a"), 1)
        with pytest.raises(TimeoutError):
            await task
        assert registry.pending_count == 0
        assert socket.messages[-1] == {
            "version": 2,
            "type": "cancel",
            "request_id": "a",
            "reason": "control request cancelled or timed out",
        }

        waiting = asyncio.create_task(
            registry.invoke("one", ping("b"), 1)
        )
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert registry.pending_count == 0
        assert socket.messages[-1] == {
            "version": 2,
            "type": "cancel",
            "request_id": "b",
            "reason": "control request cancelled or timed out",
        }

        disconnected = asyncio.create_task(
            registry.invoke("one", ping("c"), 1)
        )
        await asyncio.sleep(0)
        await registry.disconnect(socket)
        raised = None
        try:
            await disconnected
        except Exception as error:  # noqa: BLE001
            raised = error
        # The request was already sent to the Client: the answer is lost, the
        # server cannot know whether the MCP command ran -> unknown.
        assert isinstance(raised, RemoteClientError)
        assert raised.code == "timeout"
        assert raised.execution_state == "unknown"
        assert registry.pending_count == 0

    run(scenario())


def test_progress_and_correlated_error_affect_only_the_in_flight_invocation() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    socket = FakeSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def scenario() -> None:
        pending = asyncio.create_task(registry.invoke("one", ping("a"), 1))
        await asyncio.sleep(0)
        await registry.handle_progress(
            Progress(version=2, type="progress", request_id="a", progress=45, message="work")
        )
        assert registry.current_progress == 45
        with pytest.raises(UnknownRequestError):
            await registry.handle_progress(
                Progress(version=2, type="progress", request_id="missing", progress=100)
            )
        assert registry.current_progress == 45
        await registry.handle_error(
            ClientError(
                version=2,
                type="error",
                request_id="a",
                error={
                    "code": "failed",
                    "message": "client failed",
                    "execution_state": "not_started",
                },
            )
        )
        with pytest.raises(Exception, match="client failed"):
            await pending
        assert registry.current_progress is None

    run(scenario())


def test_duplicate_and_late_responses_are_rejected_without_touching_new_work() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    socket = FakeSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def scenario() -> None:
        result_waiter = asyncio.create_task(
            registry.invoke("one", ping("result"), 1)
        )
        await asyncio.sleep(0)
        result = ClientResult(
            version=2, type="result", request_id="result", result=provider_result({"ok": True})
        )
        await registry.handle_result(result)
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_result(result)
        assert await result_waiter == native_result(provider_result({"ok": True}))

        error_waiter = asyncio.create_task(
            registry.invoke("one", ping("error"), 1)
        )
        await asyncio.sleep(0)
        error = ClientError(
            version=2,
            type="error",
            request_id="error",
            error={
                "code": "failed",
                "message": "client failed",
                "execution_state": "not_started",
            },
        )
        await registry.handle_error(error)
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_error(error)
        with pytest.raises(Exception, match="client failed"):
            await error_waiter

        timed_out = asyncio.create_task(
            registry.invoke("one", ping("timed-out"), 0.01)
        )
        with pytest.raises(TimeoutError):
            await timed_out
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_result(
                ClientResult(
                    version=2,
                    type="result",
                    request_id="timed-out",
                    result=provider_result({"too": "late"}),
                )
            )

        cancelled = asyncio.create_task(
            registry.invoke("one", ping("cancelled"), 1)
        )
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_result(
                ClientResult(
                    version=2,
                    type="result",
                    request_id="cancelled",
                    result=provider_result({"too": "late"}),
                )
            )

        next_waiter = asyncio.create_task(
            registry.invoke("one", ping("next"), 1)
        )
        await asyncio.sleep(0)
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_result(
                ClientResult(
                    version=2,
                    type="result",
                    request_id="cancelled",
                    result=provider_result({"wrong": "invocation"}),
                )
            )
        assert registry.pending_count == 1
        await registry.handle_result(
            ClientResult(version=2, type="result", request_id="next", result=provider_result())
        )
        assert await next_waiter == native_result(provider_result())
        assert registry.pending_count == 0
        assert registry.current_progress is None

    run(scenario())


def test_progress_after_terminal_result_or_error_is_rejected() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    socket = FakeSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def scenario() -> None:
        result_waiter = asyncio.create_task(
            registry.invoke("one", ping("result"), 1)
        )
        await asyncio.sleep(0)
        await registry.handle_result(
            ClientResult(version=2, type="result", request_id="result", result=provider_result())
        )
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_progress(
                Progress(version=2, type="progress", request_id="result", progress=100)
            )
        assert await result_waiter == native_result(provider_result())
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_progress(
                Progress(version=2, type="progress", request_id="result", progress=100)
            )

        error_waiter = asyncio.create_task(
            registry.invoke("one", ping("error"), 1)
        )
        await asyncio.sleep(0)
        await registry.handle_error(
            ClientError(
                version=2,
                type="error",
                request_id="error",
                error={
                    "code": "failed",
                    "message": "client failed",
                    "execution_state": "not_started",
                },
            )
        )
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_progress(
                Progress(version=2, type="progress", request_id="error", progress=100)
            )
        with pytest.raises(Exception, match="client failed"):
            await error_waiter
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_progress(
                Progress(version=2, type="progress", request_id="error", progress=100)
            )

    run(scenario())


def test_progress_during_and_after_timeout_or_cancellation_is_rejected() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    socket = BlockingCancelSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def assert_late_progress(request_id: str) -> None:
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_progress(
                Progress(version=2, type="progress", request_id=request_id, progress=100)
            )

    async def scenario() -> None:
        timed_out = asyncio.create_task(
            registry.invoke("one", ping("timed-out"), 0.01)
        )
        await socket.cancel_started.wait()
        # wait_for has already cancelled the Future, but invoke is still sending cancel.
        await assert_late_progress("timed-out")
        socket.release_cancel.set()
        with pytest.raises(TimeoutError):
            await timed_out
        await assert_late_progress("timed-out")

        socket.cancel_started = asyncio.Event()
        socket.release_cancel = asyncio.Event()
        cancelled = asyncio.create_task(
            registry.invoke("one", ping("cancelled"), 1)
        )
        await asyncio.sleep(0)
        cancelled.cancel()
        await socket.cancel_started.wait()
        # Direct HTTP cancellation is not exposed reliably by TestClient; this covers
        # the registry boundary while cancellation delivery is still in progress.
        await assert_late_progress("cancelled")
        socket.release_cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await assert_late_progress("cancelled")

    run(scenario())


def test_progress_tombstones_are_bounded_and_do_not_affect_next_invocation() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    socket = FakeSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def scenario() -> None:
        for index in range(registry._RECENTLY_COMPLETED_LIMIT + 1):
            registry._remember_completed(f"old-{index}")
        assert len(registry._recently_completed) == registry._RECENTLY_COMPLETED_LIMIT
        assert "old-0" not in registry._recently_completed

        next_waiter = asyncio.create_task(
            registry.invoke("one", ping("next"), 1)
        )
        await asyncio.sleep(0)
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_progress(
                Progress(version=2, type="progress", request_id="old-1", progress=100)
            )
        assert registry.current_progress is None
        await registry.handle_progress(
            Progress(version=2, type="progress", request_id="next", progress=50)
        )
        assert registry.current_progress == 50
        await registry.handle_result(
            ClientResult(version=2, type="result", request_id="next", result=provider_result())
        )
        assert await next_waiter == native_result(provider_result())

    run(scenario())


def test_initial_send_failure_cleans_pending_and_translates_to_offline() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    socket = FailingInitialSendSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def scenario() -> None:
        with pytest.raises(ClientOfflineError):
            await registry.invoke("one", ping("a"), 1)
        assert registry.pending_count == 0
        assert "a" in registry._recently_completed

    run(scenario())


def test_initial_send_disconnect_and_all_writes_are_serialized() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    blocking = BlockingInitialSendSocket()
    register(registry, blocking)
    declare_ping(registry, blocking)

    async def disconnect_scenario() -> None:
        pending = asyncio.create_task(
            registry.invoke("one", ping("a"), 1)
        )
        await blocking.started.wait()
        await registry.disconnect(blocking)
        blocking.release.set()
        with pytest.raises(RemoteClientError) as lost:
            await pending
        # Sent to the Client before the disconnect: answer lost -> unknown.
        assert lost.value.code == "timeout"
        assert lost.value.execution_state == "unknown"
        assert registry.pending_count == 0

    run(disconnect_scenario())

    registry = RelayRegistry(client_id="one", client_token="client-token")
    concurrent = ConcurrentWriteSocket()
    register(registry, concurrent)

    async def serialization_scenario() -> None:
        await asyncio.gather(
            registry.send(concurrent, {"type": "one"}),
            registry.send(concurrent, {"type": "two"}),
        )
        assert concurrent.max_active_writes == 1

    run(serialization_scenario())


def test_register_announces_configured_server_package_version() -> None:
    registry = RelayRegistry(
        client_id="one",
        client_token="client-token",
        server_version="9.9.9",
    )
    socket = FakeSocket()
    registered = run(
        registry.register(
            socket,
            Register(version=1, type="register", client_id="one", relay_contract=RELAY_CONTRACT),
        )
    )
    assert isinstance(registered, Registered)
    assert registered.server_version == "9.9.9"
    assert registry.server_version == "9.9.9"


def test_register_defaults_to_installed_package_version() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    assert registry.server_version == package_version()


# --------------------------------------------------------------------------
# Phase 3: connection windows and hub counts for the enriched status tool
# --------------------------------------------------------------------------


def test_status_snapshot_tracks_connection_windows() -> None:
    now = 1_000.0

    def clock() -> float:
        return now

    registry = RelayRegistry(
        client_id="one", client_token="client-token", wall_clock=clock
    )
    socket = FakeSocket()

    async def scenario() -> None:
        nonlocal now
        await registry.register(
            socket, Register(version=1, type="register", client_id="one", relay_contract=RELAY_CONTRACT)
        )
        now = 1_050.0
        await registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                tools=["sample.ping"],
                relay_contract=RELAY_CONTRACT,
                client_version="0.2.0",
            ),
        )
        connected = await registry.status_snapshot()
        assert connected.connected_since == 1_000.0
        assert connected.last_disconnect_at is None
        assert connected.last_disconnect_reason is None
        now = 1_100.0
        await registry.disconnect(socket, reason="closed:1000")
        offline = await registry.status_snapshot()
        assert offline.connected is False
        assert offline.connected_since is None
        assert offline.last_disconnect_at == 1_100.0
        assert offline.last_disconnect_reason == "closed:1000"

    asyncio.run(scenario())


def test_status_snapshot_counts_public_tools_and_client_operations() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    socket = FakeSocket()
    register(registry, socket)
    registry.set_public_tools_count(10)
    run(
        registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                tools=["client.status", "mcp.list", "mcp.command", "mcp.add", "mcp.modify", "mcp.delete", "mcp.enable", "mcp.disable"],
                client_version="0.2.0",
            ),
        )
    )

    snapshot = run(registry.status_snapshot())

    assert snapshot.public_tools == 10
    assert snapshot.client_operations == 8


def test_status_snapshot_reports_zero_client_operations_without_client() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    registry.set_public_tools_count(11)

    snapshot = run(registry.status_snapshot())

    assert snapshot.connected is False
    assert snapshot.public_tools == 11
    assert snapshot.client_operations == 0


def test_disconnect_reason_is_bounded() -> None:
    registry = RelayRegistry(client_id="one", client_token="client-token")
    socket = FakeSocket()
    register(registry, socket)
    run(
        registry.disconnect(
            socket, reason="x" * 500
        )
    )
    snapshot = run(registry.status_snapshot())
    assert snapshot.last_disconnect_reason is not None
    assert len(snapshot.last_disconnect_reason) <= 64


def test_progress_listener_receives_in_flight_progress_frames() -> None:
    """The registered listener is invoked for the request in flight only."""

    async def scenario() -> None:
        registry = RelayRegistry(client_id="one", client_token="client-token")
        received: list[tuple[str, int, str]] = []

        async def listener(request_id: str, progress: int, message: str) -> None:
            received.append((request_id, progress, message))

        registry.set_progress_listener(listener)
        socket = FakeSocket()
        await registry.register(
            socket,
            Register(version=1, type="register", client_id="one", relay_contract=RELAY_CONTRACT),
        )
        await registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                tools=["mcp.list"],
                client_version="0.2.0",
            ),
        )
        message = InvokeMessage(
            version=2, type="invoke", request_id="req-1", tool_name="mcp.list"
        )
        task = asyncio.ensure_future(
            registry.invoke("one", message, timeout_seconds=5)
        )
        while not socket.messages:
            await asyncio.sleep(0)
        await registry.handle_progress(
            Progress(
                version=2,
                type="progress",
                request_id="req-1",
                progress=40,
                message="halfway",
            )
        )
        await registry.handle_result(
            ClientResult(
                version=2,
                type="result",
                request_id="req-1",
                result=provider_result({"ok": True}),
            )
        )
        await task

        assert received == [("req-1", 40, "halfway")]

    run(scenario())


def test_progress_listener_failure_does_not_break_progress_accounting() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(client_id="one", client_token="client-token")

        async def broken(request_id: str, progress: int, message: str) -> None:
            raise RuntimeError("listener exploded")

        registry.set_progress_listener(broken)
        socket = FakeSocket()
        await registry.register(
            socket,
            Register(version=1, type="register", client_id="one", relay_contract=RELAY_CONTRACT),
        )
        await registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                relay_contract=RELAY_CONTRACT,
                tools=["mcp.list"],
                client_version="0.2.0",
            ),
        )
        message = InvokeMessage(
            version=2, type="invoke", request_id="req-1", tool_name="mcp.list"
        )
        task = asyncio.ensure_future(
            registry.invoke("one", message, timeout_seconds=5)
        )
        while not socket.messages:
            await asyncio.sleep(0)
        await registry.handle_progress(
            Progress(version=2, type="progress", request_id="req-1", progress=10)
        )
        # Accounting still worked despite the listener failure.
        assert registry.current_progress == 10
        await registry.handle_result(
            ClientResult(
                version=2,
                type="result",
                request_id="req-1",
                result=provider_result({"ok": True}),
            )
        )
        await task

    run(scenario())
