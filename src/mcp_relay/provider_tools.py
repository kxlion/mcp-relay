"""Provider-neutral, closed descriptors and invocation metadata.

Third-party tools carry only their exact MCP identity: ``tool_name`` is the
name announced by the upstream server, with case and punctuation preserved.
No derived Relay public name exists on these models; the fixed Relay facade
names live in :mod:`mcp_relay.relay_tools`.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .json_bounds import (
    MAX_CATALOG_JSON_BYTES,
    MAX_CATALOG_JSON_NODES,
    JsonBoundsError,
    JsonObject,
    validate_json_bounds,
)

MAX_PROVIDER_NAME_LENGTH = 128
MAX_PROVIDER_DESCRIPTION_LENGTH = 2048
MAX_PROVIDER_TOOLS = 128

ProviderName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_PROVIDER_NAME_LENGTH,
        pattern=r"^[A-Za-z0-9._-]+$",
    ),
]
ProviderToolName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_PROVIDER_NAME_LENGTH,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]
ProviderDescription = Annotated[
    str,
    Field(min_length=1, max_length=MAX_PROVIDER_DESCRIPTION_LENGTH),
]


class _ProviderModel(BaseModel):
    """Closed, strict base for provider-neutral envelopes."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
        hide_input_in_errors=True,
    )


def _reject_ambiguous_name(value: object) -> object:
    """Refuse payloads that carry the wire ``name`` next to ``tool_name``."""
    if isinstance(value, dict) and "name" in value and "tool_name" in value:
        raise ValueError("name alias is ambiguous with tool_name")
    return value


class ProviderToolDescriptor(_ProviderModel):
    """A bounded provider capability description, not an executable callback."""

    provider_name: ProviderName
    tool_name: ProviderToolName = Field(
        validation_alias=AliasChoices("name", "tool_name"),
        serialization_alias="name",
    )
    description: ProviderDescription
    input_schema: JsonObject = Field(
        validation_alias=AliasChoices("inputSchema", "input_schema"),
        serialization_alias="inputSchema",
    )
    output_schema: JsonObject | None = Field(
        default=None,
        validation_alias=AliasChoices("outputSchema", "output_schema"),
        serialization_alias="outputSchema",
    )
    annotations: JsonObject = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _reject_ambiguous_tool_name(cls, value: object) -> object:
        return _reject_ambiguous_name(value)

    @field_validator("input_schema", "output_schema", mode="before")
    @classmethod
    def _bounded_schema(cls, value: object) -> object:
        """Enforce transport bounds only; the driver is the sole validator.

        The upstream JSON Schema is announced as-is. The relay refuses only
        bound violations (non-object, non-JSON, depth, size, node count) and
        never interprets or rejects schema assertions.
        """
        if value is None:
            return None
        try:
            return validate_json_bounds(value, require_object=True, label="schema")
        except JsonBoundsError as error:
            raise ValueError(f"schema exceeds transport bounds ({error})") from None

    @field_validator("annotations", mode="before")
    @classmethod
    def _bounded_annotations(cls, value: object) -> object:
        if value is None:
            raise ValueError("annotations must be a JSON object")
        return validate_json_bounds(
            value,
            require_object=True,
            label="annotations",
            reject_unsafe_metadata=True,
        )

    @model_validator(mode="after")
    def _bounded_descriptor(self) -> "ProviderToolDescriptor":
        validate_json_bounds(
            self.model_dump(mode="json", by_alias=True, exclude_none=True),
            require_object=True,
            label="descriptor",
        )
        return self

    @property
    def name(self) -> str:
        """The MCP tool's exact name, announced verbatim on the wire."""
        return self.tool_name


class ProviderToolInvocation(_ProviderModel):
    """A bounded provider invocation envelope with opaque JSON arguments."""

    provider_name: ProviderName
    tool_name: ProviderToolName = Field(
        validation_alias=AliasChoices("name", "tool_name"),
        serialization_alias="name",
    )
    arguments: JsonObject = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _reject_ambiguous_tool_name(cls, value: object) -> object:
        return _reject_ambiguous_name(value)

    @field_validator("arguments", mode="before")
    @classmethod
    def _bounded_arguments(cls, value: object) -> object:
        return validate_json_bounds(
            value,
            require_object=True,
            label="arguments",
            reject_unsafe_metadata=True,
        )

    @model_validator(mode="after")
    def _bounded_invocation(self) -> "ProviderToolInvocation":
        validate_json_bounds(
            self.model_dump(mode="json", by_alias=True),
            require_object=True,
            label="invocation",
        )
        return self

    @property
    def name(self) -> str:
        return self.tool_name


class ProviderToolCatalog(_ProviderModel):
    """A bounded closed collection of provider descriptors."""

    tools: list[ProviderToolDescriptor] = Field(
        default_factory=list,
        max_length=MAX_PROVIDER_TOOLS,
    )

    @model_validator(mode="after")
    def _reject_duplicate_names(self) -> "ProviderToolCatalog":
        internal_names: set[tuple[str, str]] = set()
        for tool in self.tools:
            internal_name = (tool.provider_name, tool.tool_name)
            if internal_name in internal_names:
                raise ValueError(f"duplicate internal tool name: {tool.tool_name}")
            internal_names.add(internal_name)
        return self

    @model_validator(mode="after")
    def _bounded_catalog(self) -> "ProviderToolCatalog":
        validate_json_bounds(
            self.model_dump(mode="json", by_alias=True, exclude_none=True),
            require_object=True,
            label="catalog",
            max_nodes=MAX_CATALOG_JSON_NODES,
            max_bytes=MAX_CATALOG_JSON_BYTES,
        )
        return self


__all__ = [
    "MAX_PROVIDER_DESCRIPTION_LENGTH",
    "MAX_PROVIDER_NAME_LENGTH",
    "MAX_PROVIDER_TOOLS",
    "ProviderDescription",
    "ProviderName",
    "ProviderToolCatalog",
    "ProviderToolDescriptor",
    "ProviderToolInvocation",
    "ProviderToolName",
]
