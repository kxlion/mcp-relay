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
MAX_ERROR_MESSAGE_LENGTH = 512
MAX_PROGRESS_MESSAGE_LENGTH = 512
MAX_CATALOG_TOOLS = 4096
MAX_PUBLIC_TOOL_NAME_LENGTH = 64
RELAY_CONTRACT: Final = 2
# Strict integer, never a bool or numeric string, never absent. Its value is
# compared explicitly so a mismatch closes as ``protocol_incompatible``.
RelayContract = Annotated[int, Field(strict=True, ge=0)]

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
# Public tool names use the subset every major MCP host accepts.
PublicToolName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_PUBLIC_TOOL_NAME_LENGTH,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]
AliasName = Annotated[str, Field(min_length=1, max_length=16, pattern=r"^[a-z]+$")]

#: Operations the Server may invoke on the Client.
OP_CLIENT_STATUS = "client.status"
OP_MCP_COMMAND = "mcp.command"
OP_MCP_ADD = "mcp.add"
OP_MCP_MODIFY = "mcp.modify"
OP_MCP_DELETE = "mcp.delete"
OP_MCP_ENABLE = "mcp.enable"
OP_MCP_DISABLE = "mcp.disable"
ADMIN_OPERATIONS = frozenset(
    {OP_MCP_ADD, OP_MCP_MODIFY, OP_MCP_DELETE, OP_MCP_ENABLE, OP_MCP_DISABLE}
)

# Imported after the bounded frame primitives to keep the provider result model
# independent from the protocol module's application-frame definitions.
from .output_models import ProviderToolResult  # noqa: E402


class Message(BaseModel):
    """Base class which rejects unknown wire fields."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    version: Literal[1]
    type: str


class Register(Message):
    type: Literal["register"]
    client_id: ClientId
    # Server-Client relay contract, separate from the frame ``version`` and
    # from package versions. Any other value is incompatible, never negotiated.
    relay_contract: RelayContract


class Capabilities(Message):
    """Client metadata sent once after registration.

    ``admin`` is the local administration switch, read once at Client start;
    the Server only uses it to decide whether to list the admin tools.
    """

    type: Literal["capabilities"]
    relay_contract: RelayContract
    client_version: VersionLabel
    admin: bool


class ApplicationMessage(BaseModel):
    """Closed v2 frame used only after the Client is authenticated."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    version: Literal[2]
    type: str


class Heartbeat(ApplicationMessage):
    type: Literal["heartbeat"]


class CatalogTool(BaseModel):
    """One third-party tool as published by the Server's MCP facade."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    name: PublicToolName
    alias: AliasName
    tool: ToolName
    description: Annotated[str, Field(max_length=2048)] = ""
    input_schema: JsonObject
    output_schema: JsonObject | None = None
    annotations: JsonObject | None = None


class Catalog(ApplicationMessage):
    """The Client's complete publishable tool catalog, replacing the last one."""

    type: Literal["catalog"]
    tools: list[CatalogTool] = Field(max_length=MAX_CATALOG_TOOLS)

    @model_validator(mode="after")
    def _unique_names(self) -> Catalog:
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("duplicate public tool name")
        return self


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


ClientMessage = (
    Register | Capabilities | Heartbeat | Catalog | ClientResult | ClientError | Progress
)
ServerMessage = Registered | InvokeMessage | Cancel

_client_adapter = TypeAdapter(Annotated[ClientMessage, Field(discriminator="type")])


def parse_client_message(value: object) -> ClientMessage:
    """Parse one decoded JSON object from a client."""
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ValueError("invalid client message")
    if value["type"] in {"catalog", "result", "error", "progress"}:
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
