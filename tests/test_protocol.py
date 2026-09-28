from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from mcp_relay.json_bounds import MAX_TOOL_RESULT_BYTES
from mcp_relay.protocol import (
    RELAY_CONTRACT,
    Capabilities,
    Catalog,
    ClientError,
    ClientResult,
    InvokeMessage,
    Progress,
    Register,
    Registered,
    parse_client_message,
    parse_server_message,
)


def test_v2_generic_invoke_and_provider_result_are_the_only_application_frames() -> None:
    invoke = parse_server_message(
        {
            "version": 2,
            "type": "invoke",
            "request_id": "r-1",
            "tool_name": "cua.get_accessibility_tree",
            "arguments": {},
        }
    )
    assert isinstance(invoke, InvokeMessage)
    assert invoke.tool_name == "cua.get_accessibility_tree"
    assert invoke.arguments == {}

    result = parse_client_message(
        {
            "version": 2,
            "type": "result",
            "request_id": "r-1",
            "result": {
                "content": [
                    {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}
                ],
                "structuredContent": {"ok": True},
                "isError": False,
            },
        }
    )
    assert isinstance(result, ClientResult)
    assert result.result.structured_content == {"ok": True}


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "type": "invoke", "request_id": "r", "tool": "sample.ping"},
        {"version": 2, "type": "invoke", "request_id": "r", "arguments": {}},
        {"version": 2, "type": "invoke", "request_id": "r", "tool_name": "bad/name", "arguments": {}},
        {"version": 2, "type": "invoke", "request_id": "r", "tool_name": "x.y", "arguments": []},
        {"version": 2, "type": "invoke", "request_id": "r", "tool_name": "x.y", "arguments": {}, "handler": "x"},
    ],
)
def test_v2_generic_invoke_rejects_legacy_malformed_and_executable_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_server_message(payload)


def test_parses_strict_versioned_token_free_register() -> None:
    message = parse_client_message(
        {
            "version": 1,
            "type": "register",
            "client_id": "client-a",
            "relay_contract": RELAY_CONTRACT,
        }
    )

    assert isinstance(message, Register)
    assert message.model_dump(mode="json") == {
        "version": 1,
        "type": "register",
        "client_id": "client-a",
        "relay_contract": RELAY_CONTRACT,
    }
    assert "token" not in repr(message)
    assert "secret" not in str(message)


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2, "type": "register", "client_id": "client-a", "token": "x"},
        {"version": 1, "type": "unknown"},
        {
            "version": 1,
            "type": "register",
            "client_id": "client-a",
            "token": "x",
            "extra": 1,
        },
    ],
)
def test_rejects_bad_version_type_and_extra_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        parse_client_message(payload)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"type": []}, id="array-type"),
        pytest.param({"type": {}}, id="object-type"),
        pytest.param({"type": None}, id="null-type"),
        pytest.param([], id="array-root"),
    ],
)
def test_client_parser_rejects_non_string_type_and_non_object_root(payload: object) -> None:
    with pytest.raises(ValueError, match="^invalid client message$"):
        parse_client_message(payload)


def test_generic_invoke_preserves_provider_owned_arguments() -> None:
    invoke = InvokeMessage(
        version=2,
        type="invoke",
        request_id="req-1",
        tool_name="sample.exec",
        arguments={"command_id": "pwd"},
    )
    assert invoke.tool_name == "sample.exec"
    assert invoke.arguments == {"command_id": "pwd"}

    unvalidated = InvokeMessage(
        version=2,
        type="invoke",
        request_id="req-2",
        tool_name="sample.exec",
        arguments={"command_id": "provider-owned-command"},
    )
    assert unvalidated.arguments == {"command_id": "provider-owned-command"}


def test_invoke_message_is_an_explicit_closed_union() -> None:
    assert InvokeMessage.model_fields.keys() == {
        "version", "type", "request_id", "tool_name", "arguments"
    }


@pytest.mark.parametrize(
    "payload",
    [
        {
            "version": 2,
            "type": "invoke",
            "request_id": "ping-1",
            "tool_name": "sample.ping",
            "arguments": {},
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "exec-1",
            "tool_name": "sample.exec",
            "arguments": {"command_id": "pwd"},
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "cua-browser-1",
            "tool_name": "cua.browser_click",
            "arguments": {"locator": {"role": "button", "name": "Submit"}},
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "cua-1",
            "tool_name": "cua.type_text",
            "arguments": {"element_token": "provider-owned-token", "text": "hello"},
        },
    ],
)
def test_generic_invoke_parsing_accepts_provider_tool_arguments(
    payload: dict[str, object],
) -> None:
    message = parse_server_message(payload)
    assert isinstance(message, InvokeMessage)
    assert message.tool_name


@pytest.mark.parametrize(
    "payload",
    [
        {
            "version": 1,
            "type": "invoke",
            "request_id": "legacy-1",
            "tool": "sample.ping",
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "missing-name",
            "arguments": {},
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "legacy-field",
            "tool": "sample.ping",
            "arguments": {},
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "array-args",
            "tool_name": "sample.ping",
            "arguments": [],
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "handler-field",
            "tool_name": "sample.ping",
            "arguments": {},
            "handler": "not-accepted",
        },
        {
            "version": 2,
            "type": "invoke",
            "request_id": "bad-name",
            "tool_name": "bad/name",
            "arguments": {},
        },
    ],
)
def test_generic_invoke_rejects_legacy_and_malformed_frames(
    payload: dict[str, object],
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_server_message(payload)


def test_generic_invoke_keeps_provider_arguments_bounded_but_opaque() -> None:
    message = InvokeMessage(
        version=2,
        type="invoke",
        request_id="opaque-1",
        tool_name="cua.provider_tool",
        arguments={
            "provider_owned": {"value": "kept"},
            "items": [1, True, None],
        },
    )
    assert message.arguments == {
        "provider_owned": {"value": "kept"},
        "items": [1, True, None],
    }

    with pytest.raises(ValidationError):
        InvokeMessage(
            version=2,
            type="invoke",
            request_id="opaque-2",
            tool_name="cua.provider_tool",
            arguments=[],  # type: ignore[arg-type]
        )

def test_register_frame_has_no_credential_field_or_secret_repr() -> None:
    message = Register(
        version=1,
        type="register",
        client_id="client-a",
        relay_contract=RELAY_CONTRACT,
    )
    assert message.model_dump(mode="json") == {
        "version": 1,
        "type": "register",
        "client_id": "client-a",
        "relay_contract": RELAY_CONTRACT,
    }
    assert "secret" not in repr(message)
    assert "token" not in json.dumps(message.model_dump(mode="json"))


def test_client_result_carries_bounded_provider_result() -> None:
    result = ClientResult(
        version=2,
        type="result",
        request_id="request",
        result={
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": {"command_id": "pwd"},
        },
    )
    assert result.result.structured_content == {"command_id": "pwd"}


def test_protocol_rejects_register_frame_credentials_and_bounds_client_payloads() -> None:
    with pytest.raises(ValidationError):
        Register(
            version=1,
            type="register",
            client_id="client-a",
            token="secret",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        ClientResult(
            version=2,
            type="result",
            request_id="request",
            result={"content": [{"type": "text", "text": "x" * (MAX_TOOL_RESULT_BYTES + 1)}]},
        )
    deeply_nested: dict[str, object] = {}
    cursor = deeply_nested
    for _ in range(17):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    with pytest.raises(ValidationError):
        ClientResult(
            version=2,
            type="result",
            request_id="request",
            result={"content": [], "structuredContent": deeply_nested},
        )
    with pytest.raises(ValidationError):
        ClientError(
            version=2,
            type="error",
            request_id="request",
            error={
                "code": "failed",
                "message": "x" * 513,
                "execution_state": "not_started",
            },
        )
    with pytest.raises(ValidationError):
        Progress(
            version=2,
            type="progress",
            request_id="request",
            progress=1,
            message="x" * 513,
        )


def test_registered_announces_bounded_server_package_version() -> None:
    registered = parse_server_message(
        {
            "version": 1,
            "type": "registered",
            "client_id": "d",
            "server_version": "0.1.0",
            "relay_contract": RELAY_CONTRACT,
        }
    )
    assert isinstance(registered, Registered)
    assert registered.server_version == "0.1.0"
    # The package version is metadata only: the wire protocol version of the
    # handshake frame stays at 1 and is a separate concept.
    assert registered.version == 1


@pytest.mark.parametrize(
    ("server_version", "reason"),
    [
        ("", "empty value"),
        ("/etc/passwd", "filesystem path"),
        ("0.1.0 token=secret", "embedded credential"),
        ("0.1.0\nmalicious", "log injection"),
        ("0" * 65, "over-length value"),
        ("0.1.0 $HOME", "shell expansion"),
        ("../secret", "path traversal"),
    ],
)
def test_registered_rejects_sensitive_or_unbounded_server_versions(
    server_version: str, reason: str
) -> None:
    with pytest.raises(ValidationError):
        parse_server_message(
            {
                "version": 1,
                "type": "registered",
                "client_id": "d",
                "server_version": server_version,
                "relay_contract": RELAY_CONTRACT,
            }
        )


def test_registered_server_version_must_be_a_string() -> None:
    with pytest.raises(ValidationError):
        parse_server_message(
            {
                "version": 1,
                "type": "registered",
                "client_id": "d",
                "server_version": 12,
                "relay_contract": RELAY_CONTRACT,
            }
        )


def test_capabilities_frame_carries_bounded_client_version() -> None:
    capabilities = Capabilities(
        version=1,
        type="capabilities",
        admin=False,
        relay_contract=RELAY_CONTRACT,
        client_version="0.2.0",
    )
    assert capabilities.client_version == "0.2.0"
    # Unresolvable metadata is announced as "unknown", never omitted.
    fallback = Capabilities(
        version=1,
        type="capabilities",
        admin=False,
        relay_contract=RELAY_CONTRACT,
        client_version="unknown",
    )
    assert fallback.client_version == "unknown"


@pytest.mark.parametrize(
    ("version_value", "reason"),
    [
        ("", "empty value"),
        ("/opt/cua-driver/bin/driver", "filesystem path"),
        ("0.1.0 token=secret", "embedded credential"),
        ("0.1.0\nmalicious", "log injection"),
        ("0" * 65, "over-length value"),
        ("0.1.0 $HOME", "shell expansion"),
        ("../secret", "path traversal"),
    ],
)
def test_capabilities_version_metadata_is_bounded_and_version_safe(
    version_value: str, reason: str
) -> None:
    with pytest.raises(ValidationError):
        Capabilities(
            version=1,
            type="capabilities",
            admin=False,
            relay_contract=RELAY_CONTRACT,
            client_version=version_value,
        )


def test_handshake_frames_carry_the_current_relay_contract() -> None:
    register = Register(
        version=1,
        type="register",
        client_id="client-a",
        relay_contract=RELAY_CONTRACT,
    )
    assert register.relay_contract == RELAY_CONTRACT
    registered = parse_server_message(
        {
            "version": 1,
            "type": "registered",
            "client_id": "client-a",
            "server_version": "0.1.0",
            "relay_contract": RELAY_CONTRACT,
        }
    )
    assert isinstance(registered, Registered)
    assert registered.relay_contract == RELAY_CONTRACT
    capabilities = Capabilities(
        version=1,
        type="capabilities",
        admin=False,
        relay_contract=RELAY_CONTRACT,
        client_version="0.2.0",
    )
    assert capabilities.relay_contract == RELAY_CONTRACT
    # The relay contract is its own mandatory wire field, separate from the
    # protocol ``version`` and from the package ``client_version``/``server_version``.
    assert capabilities.version == 1


@pytest.mark.parametrize(
    "frame",
    ["register", "registered", "capabilities"],
)
@pytest.mark.parametrize(
    ("relay_contract", "reason"),
    [
        (None, "absent field"),
        ("2", "non-strict integer"),
        (True, "boolean masquerading as integer"),
        (-1, "negative contract"),
    ],
)
def test_frames_reject_missing_or_malformed_relay_contract(
    frame: str, relay_contract: object, reason: str
) -> None:
    if frame == "register":
        payload: dict[str, object] = {
            "version": 1,
            "type": "register",
            "client_id": "client-a",
        }
        if relay_contract is not None:
            payload["relay_contract"] = relay_contract
        with pytest.raises(ValidationError):
            parse_client_message(payload)
    elif frame == "registered":
        payload = {
            "version": 1,
            "type": "registered",
            "client_id": "client-a",
            "server_version": "0.1.0",
        }
        if relay_contract is not None:
            payload["relay_contract"] = relay_contract
        with pytest.raises(ValidationError):
            parse_server_message(payload)
    else:
        payload = {
            "version": 1,
            "type": "capabilities",
            "admin": False,
            "client_version": "0.2.0",
        }
        if relay_contract is not None:
            payload["relay_contract"] = relay_contract
        with pytest.raises(ValidationError):
            parse_client_message(payload)


def test_error_detail_carries_bounded_execution_state() -> None:
    not_started = ClientError(
        version=2,
        type="error",
        request_id="request",
        error={
            "code": "tool_unknown",
            "message": "no such tool",
            "execution_state": "not_started",
        },
    )
    assert not_started.error.execution_state == "not_started"
    unknown = ClientError(
        version=2,
        type="error",
        request_id="request",
        error={
            "code": "timeout",
            "message": "deadline expired",
            "execution_state": "unknown",
        },
    )
    assert unknown.error.execution_state == "unknown"


def test_error_detail_requires_execution_state() -> None:
    with pytest.raises(ValidationError):
        ClientError(
            version=2,
            type="error",
            request_id="request",
            error={"code": "failed", "message": "boom"},
        )


@pytest.mark.parametrize(
    "execution_state",
    [None, "", "maybe", "NOT_STARTED", "not-started", True, 1],
)
def test_error_detail_rejects_unknown_execution_states(
    execution_state: object,
) -> None:
    with pytest.raises(ValidationError):
        ClientError(
            version=2,
            type="error",
            request_id="request",
            error={
                "code": "failed",
                "message": "boom",
                "execution_state": execution_state,  # type: ignore[dict-item]
            },
        )


def _catalog_tool(name: str = "fs__read", **overrides: object) -> dict[str, object]:
    return {
        "name": name,
        "alias": "fs",
        "tool": "read",
        "input_schema": {"type": "object"},
        **overrides,
    }


def test_catalog_frame_is_an_application_frame() -> None:
    message = parse_client_message(
        {"version": 2, "type": "catalog", "tools": [_catalog_tool()]}
    )
    assert isinstance(message, Catalog)
    assert message.tools[0].tool == "read"
    with pytest.raises(ValueError):
        parse_client_message({"version": 1, "type": "catalog", "tools": []})


@pytest.mark.parametrize(
    "tools",
    [
        [_catalog_tool(), _catalog_tool()],
        [_catalog_tool("fs.read")],
        [_catalog_tool("x" * 65)],
        [_catalog_tool(alias="FS")],
        [_catalog_tool(input_schema=[])],
        [_catalog_tool(handler="x")],
    ],
)
def test_catalog_frame_rejects_ambiguous_or_unportable_entries(tools: list[object]) -> None:
    with pytest.raises(ValidationError):
        parse_client_message({"version": 2, "type": "catalog", "tools": tools})
