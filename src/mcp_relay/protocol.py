"""Strict identity frames and provider-neutral v2 application messages."""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .json_bounds import (
    MAX_REQUEST_ID_LENGTH,
    JsonObject,
    validate_json_bounds,
)

MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 256
# Credential ASCII must also be usable verbatim as an HTTP Bearer credential.
TOKEN_PATTERN = r"^[\x21-\x7e]+$"
MAX_CAPABILITIES = 128
MAX_ERROR_MESSAGE_LENGTH = 512
MAX_PROGRESS_MESSAGE_LENGTH = 512
RELAY_CONTRACT: Final = 1
# Strict wire type for the relay contract field: exactly the integer 1, never
# a bool or numeric string, never absent.
RelayContract = Annotated[
    int, Field(strict=True, ge=RELAY_CONTRACT, le=RELAY_CONTRACT)
]

RequestId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_REQUEST_ID_LENGTH,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]
ClientId = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
]
ToolName = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
]
# Version labels are package versions, never free-form text: bounded to 64
# version-safe characters so they can never carry credentials, filesystem
# paths, or configuration. Shared by the handshake metadata fields.
VERSION_LABEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$"
VersionLabel = Annotated[
    str, Field(min_length=1, max_length=64, pattern=VERSION_LABEL_PATTERN)
]

# Imported after the bounded frame primitives to keep the provider result model
# independent from the protocol module's application-frame definitions.
from .output_models import ProviderToolResult  # noqa: E402
from .provider_tools import ProviderToolDescriptor  # noqa: E402,F401


class Message(BaseModel):
    """Base class which rejects unknown wire fields."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    version: Literal[1]
    type: str


class Register(Message):
    type: Literal["register"]
    client_id: ClientId
    # Explicit Server-Client relay contract, separate from the protocol
    # ``version`` field's role (handshake v1, application frames v2) and from
    # the package version metadata. Exactly 1 is accepted; an absent or other
    # value is a protocol incompatibility, never negotiable.
    relay_contract: RelayContract


class Capabilities(Message):
    """Capability announcement sent after authentication.

    ``tools`` carries exactly the fixed Relay wire operations shared by
    Server and Client code — never third-party descriptors or schemas, and
    independent of the Client's admin setting. ``client_version`` is the
    Client's installed package version, bounded by :data:`VersionLabel`.
    Unresolvable metadata is announced as ``unknown`` instead of invented.
    """

    type: Literal["capabilities"]
    tools: list[ToolName] = Field(min_length=0, max_length=MAX_CAPABILITIES)
    relay_contract: RelayContract
    client_version: VersionLabel


class ApplicationMessage(BaseModel):
    """Closed v2 frame used only after the Client is authenticated."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    version: Literal[2]
    type: str


class Heartbeat(ApplicationMessage):
    type: Literal["heartbeat"]


class ClientResult(ApplicationMessage):
    type: Literal["result"]
    request_id: RequestId
    result: ProviderToolResult


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.-]+$")]
    message: Annotated[str, Field(min_length=1, max_length=MAX_ERROR_MESSAGE_LENGTH)]
    # Bounded execution state for relayed failures: ``not_started`` asserts the
    # MCP target was never sent the business operation; ``unknown`` means the
    # response was lost after dispatch, so the operation may have happened.
    execution_state: Literal["not_started", "unknown"]


class ClientError(ApplicationMessage):
    type: Literal["error"]
    request_id: RequestId
    error: ErrorDetail

    @model_validator(mode="before")
    @classmethod
    def _coerce_error_detail(cls, value: object) -> object:
        if isinstance(value, dict) and isinstance(value.get("error"), dict):
            return {**value, "error": ErrorDetail.model_validate(value["error"])}
        return value


class Progress(ApplicationMessage):
    type: Literal["progress"]
    request_id: RequestId
    progress: Annotated[int, Field(ge=0, le=100)]
    message: Annotated[str, Field(max_length=MAX_PROGRESS_MESSAGE_LENGTH)] = ""


class Registered(Message):
    """Registration acknowledgement for the version 1 handshake.

    ``server_version`` is the Relay Server's *package* version (the installed
    ``mcp-relay`` distribution version). It is deliberately separate from
    the wire ``version`` field inherited from ``Message``, which is the
    *protocol* version of this frame and stays at 1. Unresolvable metadata
    is announced as ``unknown``. The field is bounded and restricted to
    version characters so it can never carry credentials, filesystem
    paths, or free-form configuration.
    """

    type: Literal["registered"]
    client_id: ClientId
    server_version: VersionLabel
    relay_contract: RelayContract


class InvokeMessage(ApplicationMessage):
    """Provider-neutral invocation with bounded opaque JSON arguments."""

    type: Literal["invoke"]
    request_id: RequestId
    tool_name: ToolName
    arguments: JsonObject = Field(default_factory=dict)

    @field_validator("arguments", mode="before")
    @classmethod
    def bounded_arguments(cls, value: object) -> object:
        return validate_json_bounds(value, require_object=True, label="arguments")


class Cancel(ApplicationMessage):
    type: Literal["cancel"]
    request_id: RequestId
    reason: Annotated[str, Field(min_length=1, max_length=256)]


ClientMessage = Register | Capabilities | Heartbeat | ClientResult | ClientError | Progress
ServerMessage = Registered | InvokeMessage | Cancel

_client_adapter = TypeAdapter(Annotated[ClientMessage, Field(discriminator="type")])


def parse_client_message(value: object) -> ClientMessage:
    """Parse one decoded JSON object from a client."""
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ValueError("invalid client message")
    if value["type"] in {"result", "error", "progress"}:
        if value.get("version") != 2:
            raise ValueError("invalid application message version")
    return _client_adapter.validate_python(value)


def parse_server_message(value: object) -> ServerMessage:
    """Parse one decoded JSON object from the relay server."""
    if not isinstance(value, dict):
        raise ValueError("server message must be an object")
    message_type = value.get("type")
    if message_type == "registered":
        return Registered.model_validate(value)
    if message_type == "cancel":
        return Cancel.model_validate(value)
    if message_type == "invoke":
        return InvokeMessage.model_validate(value)
    raise ValueError("unknown server message")
