"""Shared closed result models for every public Relay boundary."""

from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from . import json_bounds as _bounds_runtime

# NOTE: the three result/transport bounds (MAX_TOOL_RESULT_BYTES,
# MAX_RESULT_NODES, MAX_WS_MESSAGE_BYTES) are runtime-resolvable via the
# RELAY_MAX_* overrides; they must be read through the module attribute at
# validation time, never bound at import. Static unit bounds below may be
# imported directly.
from .json_bounds import (
    MAX_JSON_BYTES,
    MAX_JSON_COLLECTION_ITEMS,
    MAX_JSON_URI_LENGTH,
    JsonObject,
    validate_json_bounds,
    validate_resource_uri,
)


class Output(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
        hide_input_in_errors=True,
    )

class _ProviderContent(Output):
    """Shared bounded MCP metadata for provider content blocks."""

    annotations: JsonObject | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    meta: JsonObject | None = Field(
        default=None,
        validation_alias=AliasChoices("_meta", "meta"),
        serialization_alias="_meta",
        exclude_if=lambda value: value is None,
    )

    @field_validator("annotations", "meta", mode="before")
    @classmethod
    def _bounded_metadata(cls, value: object) -> object:
        if value is None:
            return None
        return validate_json_bounds(
            value,
            require_object=True,
            label="content metadata",
        )

    @model_validator(mode="after")
    def _bounded_content(self) -> "_ProviderContent":
        # One content block is bounded by the per-block size bound; a result
        # made of a single block therefore fills the aggregate result bound
        # exactly (chain invariant allows equality). Live reads: the bound is
        # runtime-resolvable via RELAY_MAX_TOOL_RESULT_BYTES.
        validate_json_bounds(
            self.model_dump(mode="json", by_alias=True, exclude_none=True),
            require_object=True,
            label="content",
            max_bytes=_bounds_runtime.MAX_TOOL_RESULT_BYTES,
            max_bytes_env="RELAY_MAX_TOOL_RESULT_BYTES",
        )
        return self


class ProviderTextContent(_ProviderContent):
    """MCP text content preserved without semantic interpretation."""

    type: Literal["text"]
    # Size is enforced by the per-block bound validator above (live read of
    # RELAY_MAX_TOOL_RESULT_BYTES) — a Field constraint here would freeze
    # the bound at import and break the override contract.
    text: str


class ProviderImageContent(_ProviderContent):
    """MCP image content with bounded, opaque encoded data."""

    type: Literal["image"]
    data: str = Field(min_length=1)
    mime_type: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("mimeType", "mime_type"),
        serialization_alias="mimeType",
    )


class ProviderAudioContent(_ProviderContent):
    """MCP audio content with bounded, opaque encoded data."""

    type: Literal["audio"]
    data: str = Field(min_length=1)
    mime_type: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("mimeType", "mime_type"),
        serialization_alias="mimeType",
    )


ProviderResourceUri = Annotated[
    str,
    Field(min_length=1, max_length=MAX_JSON_URI_LENGTH),
]


class ProviderEmbeddedResource(Output):
    """A resource body with a conservative network URI policy."""

    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}},
                    "not": {"required": ["blob"]},
                },
                {
                    "required": ["blob"],
                    "properties": {"blob": {"type": "string"}},
                    "not": {"required": ["text"]},
                },
            ]
        }
    )

    uri: ProviderResourceUri
    mime_type: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("mimeType", "mime_type"),
        serialization_alias="mimeType",
    )
    text: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    blob: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    meta: JsonObject | None = Field(
        default=None,
        validation_alias=AliasChoices("_meta", "meta"),
        serialization_alias="_meta",
        exclude_if=lambda value: value is None,
    )

    @field_validator("uri", mode="before")
    @classmethod
    def _safe_uri(cls, value: object) -> object:
        return validate_resource_uri(value)

    @field_validator("text", "blob", mode="before")
    @classmethod
    def _reject_explicit_null_body(cls, value: object) -> object:
        if value is None:
            raise ValueError("resource body must be a non-null string")
        return value

    @field_validator("meta", mode="before")
    @classmethod
    def _bounded_metadata(cls, value: object) -> object:
        if value is None:
            return None
        return validate_json_bounds(
            value,
            require_object=True,
            label="resource metadata",
        )

    @model_validator(mode="after")
    def _one_resource_body(self) -> "ProviderEmbeddedResource":
        if (self.text is None) == (self.blob is None):
            raise ValueError("resource must contain exactly one text or blob body")
        return self

    @model_validator(mode="after")
    def _bounded_resource(self) -> "ProviderEmbeddedResource":
        validate_json_bounds(
            self.model_dump(mode="json", by_alias=True, exclude_none=True),
            require_object=True,
            label="resource",
            max_bytes=_bounds_runtime.MAX_TOOL_RESULT_BYTES,
            max_bytes_env="RELAY_MAX_TOOL_RESULT_BYTES",
        )
        return self


class ProviderResourceContent(_ProviderContent):
    """MCP embedded resource content, retained as provider-supplied data."""

    type: Literal["resource"]
    resource: ProviderEmbeddedResource


class ProviderResourceLinkContent(_ProviderContent):
    """MCP resource-link content with no local-file or credential URI schemes."""

    type: Literal["resource_link"]
    uri: ProviderResourceUri
    name: str = Field(
        min_length=1,
        max_length=MAX_JSON_BYTES,
    )
    title: str | None = Field(default=None, max_length=MAX_JSON_BYTES)
    description: str | None = Field(default=None, max_length=MAX_JSON_BYTES)
    mime_type: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("mimeType", "mime_type"),
        serialization_alias="mimeType",
    )
    size: int | None = Field(default=None, ge=0, le=MAX_JSON_BYTES)
    icons: list[JsonObject] | None = Field(
        default=None,
        max_length=MAX_JSON_COLLECTION_ITEMS,
    )

    @field_validator("uri", mode="before")
    @classmethod
    def _safe_uri(cls, value: object) -> object:
        return validate_resource_uri(value)

    @field_validator("icons", mode="before")
    @classmethod
    def _bounded_icons(cls, value: object) -> object:
        if value is None:
            return None
        return validate_json_bounds(value, label="resource link icons")


ProviderContent = (
    ProviderTextContent
    | ProviderImageContent
    | ProviderAudioContent
    | ProviderResourceContent
    | ProviderResourceLinkContent
)


class ProviderToolResult(Output):
    """MCP-compatible bounded result; content is not mapped to semantic DTOs.

    Unknown top-level fields (e.g. the SDK's ``resultType``) are preserved
    pass-through, opaque and bounded: the relay stays neutral with respect to
    provider results. Bounds still apply — the ``_bounded_result`` validator
    covers captured extras through the full-dump check.
    """

    model_config = ConfigDict(extra="allow")

    content: list[ProviderContent] = Field(
        max_length=MAX_JSON_COLLECTION_ITEMS,
    )
    structured_content: JsonObject | None = Field(
        default=None,
        validation_alias=AliasChoices("structuredContent", "structured_content"),
        serialization_alias="structuredContent",
    )
    is_error: bool = Field(
        default=False,
        validation_alias=AliasChoices("isError", "is_error"),
        serialization_alias="isError",
    )
    meta: JsonObject | None = Field(
        default=None,
        validation_alias=AliasChoices("_meta", "meta"),
        serialization_alias="_meta",
        exclude_if=lambda value: value is None,
    )

    @field_validator("structured_content", mode="before")
    @classmethod
    def _bounded_structured_content(cls, value: object) -> object:
        if value is None:
            return None
        return validate_json_bounds(
            value,
            require_object=True,
            label="structuredContent",
            max_nodes=_bounds_runtime.MAX_RESULT_NODES,
            max_nodes_env="RELAY_MAX_RESULT_NODES",
            max_bytes=_bounds_runtime.MAX_TOOL_RESULT_BYTES,
            max_bytes_env="RELAY_MAX_TOOL_RESULT_BYTES",
        )

    @field_validator("meta", mode="before")
    @classmethod
    def _bounded_metadata(cls, value: object) -> object:
        if value is None:
            return None
        return validate_json_bounds(
            value,
            require_object=True,
            label="result metadata",
        )

    @model_validator(mode="after")
    def _bounded_result(self) -> "ProviderToolResult":
        # Aggregate result bound: sized for the heaviest known real consumer
        # (cua-driver full desktop inventory: screenshot + complete UIA tree).
        # Live reads: bounds are runtime-resolvable via RELAY_MAX_*.
        validate_json_bounds(
            self.model_dump(mode="json", by_alias=True, exclude_none=True),
            require_object=True,
            label="provider result",
            max_nodes=_bounds_runtime.MAX_RESULT_NODES,
            max_nodes_env="RELAY_MAX_RESULT_NODES",
            max_bytes=_bounds_runtime.MAX_TOOL_RESULT_BYTES,
            max_bytes_env="RELAY_MAX_TOOL_RESULT_BYTES",
        )
        return self
