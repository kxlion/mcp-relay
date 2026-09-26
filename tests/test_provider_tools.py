from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from mcp_relay.json_bounds import (
    MAX_JSON_BYTES,
    MAX_JSON_COLLECTION_ITEMS,
    MAX_JSON_DEPTH,
    MAX_TOOL_RESULT_BYTES,
    validate_json_bounds,
    validate_resource_uri,
)
from mcp_relay.output_models import (
    ProviderEmbeddedResource,
    ProviderImageContent,
    ProviderResourceContent,
    ProviderResourceLinkContent,
    ProviderTextContent,
    ProviderToolResult,
)
from mcp_relay.protocol import ClientResult
from mcp_relay.provider_tools import (
    ProviderToolCatalog,
    ProviderToolDescriptor,
    ProviderToolInvocation,
)


def _descriptor_payload() -> dict[str, object]:
    return {
        "provider_name": "example-provider",
        "tool_name": "read",
        "description": "Read one bounded example value.",
        "input_schema": {
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "title": "Read example"},
    }


def test_provider_descriptor_is_strict_and_emits_mcp_schema_names() -> None:
    descriptor = ProviderToolDescriptor.model_validate(_descriptor_payload())

    assert descriptor.provider_name == "example-provider"
    assert descriptor.tool_name == "read"
    assert descriptor.name == "read"
    assert descriptor.model_dump(by_alias=True)["inputSchema"]["type"] == "object"
    assert descriptor.model_dump(by_alias=True)["name"] == "read"

    with pytest.raises(ValidationError):
        ProviderToolDescriptor.model_validate(_descriptor_payload() | {"handler": "run"})


def test_provider_descriptor_accepts_standard_mcp_aliases_but_no_code_metadata() -> None:
    payload = _descriptor_payload() | {
        "name": "example.read",
        "inputSchema": _descriptor_payload()["input_schema"],
    }
    payload.pop("tool_name")
    payload.pop("input_schema")
    descriptor = ProviderToolDescriptor.model_validate(payload)
    assert descriptor.tool_name == "example.read"

    with pytest.raises(ValidationError):
        ProviderToolDescriptor.model_validate(
            _descriptor_payload() | {"annotations": {"module": "provider.module"}}
        )


def test_provider_descriptor_publishes_schema_property_names_as_is() -> None:
    """Schema property names are provider data, not relay metadata.

    A tool that legitimately owns a ``command`` or ``code`` property is
    announced verbatim; the driver remains the sole validator of its schema.
    """
    schema = {
        "type": "object",
        "properties": {"command": {"type": "string"}, "code": {}},
    }
    descriptor = ProviderToolDescriptor.model_validate(
        _descriptor_payload() | {"input_schema": schema}
    )
    assert descriptor.input_schema == schema


def test_provider_descriptor_announces_the_exact_mcp_name() -> None:
    """``name`` is the upstream MCP name verbatim, never normalized."""
    descriptor = ProviderToolDescriptor.model_validate(
        _descriptor_payload() | {"tool_name": "Provider_Read:v2"}
    )

    assert descriptor.provider_name == "example-provider"
    assert descriptor.tool_name == "Provider_Read:v2"
    assert descriptor.name == "Provider_Read:v2"
    assert descriptor.model_dump(by_alias=True)["name"] == "Provider_Read:v2"


def test_provider_descriptor_rejects_ambiguous_name_and_tool_name() -> None:
    payload = _descriptor_payload() | {"name": "example.read"}
    with pytest.raises(ValidationError, match="ambiguous"):
        ProviderToolDescriptor.model_validate(payload)


def test_provider_descriptor_rejects_removed_public_name_identity() -> None:
    """The derived public name identity is gone without a compat alias."""
    with pytest.raises(ValidationError):
        ProviderToolDescriptor.model_validate(
            _descriptor_payload() | {"public_name": "example.read"}
        )


def test_provider_descriptor_accepts_unambiguous_legacy_name_alias() -> None:
    payload = _descriptor_payload()
    payload.pop("tool_name")
    payload["name"] = "example.read"

    descriptor = ProviderToolDescriptor.model_validate(payload)

    assert descriptor.tool_name == "example.read"
    assert descriptor.name == "example.read"


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "execute",
        "handler",
        "module",
        "script",
        "shell",
        "command",
        "x-handler",
        "module.path",
        "command-line",
    ],
)
def test_provider_descriptor_rejects_executable_metadata_keys(forbidden_key: str) -> None:
    with pytest.raises(ValidationError):
        ProviderToolDescriptor.model_validate(
            _descriptor_payload() | {"annotations": {forbidden_key: "blocked"}}
        )


def test_provider_descriptor_bounds_schema() -> None:
    too_many_properties = {
        "type": "object",
        "properties": {
            f"field_{index}": {"type": "string"}
            for index in range(MAX_JSON_COLLECTION_ITEMS + 1)
        },
    }
    with pytest.raises(ValidationError):
        ProviderToolDescriptor.model_validate(
            _descriptor_payload() | {"input_schema": too_many_properties}
        )

    non_json_schema = _descriptor_payload() | {
        "input_schema": {"type": "object", "default": object()}
    }
    with pytest.raises(ValidationError):
        ProviderToolDescriptor.model_validate(non_json_schema)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "maxLength": "bad"},
        {"type": "object", "required": "bad", "additionalProperties": False},
        {
            "type": "object",
            "required": ["ok", 1],
            "additionalProperties": False,
        },
    ],
)
def test_provider_descriptor_publishes_keyword_values_as_is(
    schema: dict[str, object],
) -> None:
    """Keyword values the relay does not interpret are published verbatim.

    The driver remains the sole validator: the relay enforces transport
    bounds only and never judges the well-typedness of schema keywords.
    """
    descriptor = ProviderToolDescriptor.model_validate(
        _descriptor_payload() | {"input_schema": schema}
    )
    assert descriptor.input_schema == schema


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array", "items": {"type": "string"}},
        {"$ref": "#"},
        {"$ref": "https://schemas.example.test/tool.json"},
    ],
)
def test_provider_descriptor_publishes_open_and_reference_schemas_as_is(
    schema: dict[str, object],
) -> None:
    """Schemas with open collections or external references are announced.

    The relay refuses only transport-bound violations, never unsupported or
    unbounded schema assertions; the driver owns their semantics.
    """
    descriptor = ProviderToolDescriptor.model_validate(
        _descriptor_payload() | {"input_schema": schema}
    )
    assert descriptor.input_schema == schema


def test_provider_descriptor_keeps_open_objects_open_without_added_bounds() -> None:
    schema = {"type": "object", "properties": {"value": {"type": "string"}}}

    descriptor = ProviderToolDescriptor.model_validate(
        _descriptor_payload() | {"input_schema": schema}
    )

    # Pass-through: the upstream schema is published verbatim. The relay adds
    # no caps of its own and the driver remains the sole validator.
    assert descriptor.input_schema == schema


def test_provider_descriptor_publishes_unbounded_mapping_schemas_as_is() -> None:
    schema = {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }

    descriptor = ProviderToolDescriptor.model_validate(
        _descriptor_payload() | {"input_schema": schema}
    )

    assert descriptor.input_schema == schema


def test_provider_descriptor_refuses_schema_exceeding_transport_bounds() -> None:
    deep: dict[str, object] = {"type": "object"}
    node = deep
    for _ in range(MAX_JSON_DEPTH + 8):
        child: dict[str, object] = {"type": "object"}
        node["properties"] = {"nested": child}
        node = child
    with pytest.raises(ValidationError) as too_deep:
        ProviderToolDescriptor.model_validate(
            _descriptor_payload() | {"input_schema": deep}
        )
    assert "schema exceeds transport bounds" in str(too_deep.value)
    assert "too deeply nested" in str(too_deep.value)

    oversized = {
        "type": "object",
        "description": "x" * (MAX_JSON_BYTES + 1),
    }
    with pytest.raises(ValidationError) as too_large:
        ProviderToolDescriptor.model_validate(
            _descriptor_payload() | {"input_schema": oversized}
        )
    assert "schema exceeds transport bounds" in str(too_large.value)
    assert "exceeds maximum size" in str(too_large.value)

    with pytest.raises(ValidationError, match="schema exceeds transport bounds"):
        ProviderToolDescriptor.model_validate(
            _descriptor_payload() | {"input_schema": ["not", "an", "object"]}
        )


def test_provider_descriptor_accepts_bounded_local_definition_and_array_schema() -> None:
    schema = {
        "$defs": {
            "item": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            }
        },
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"$ref": "#/$defs/item"},
                "maxItems": 4,
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }

    descriptor = ProviderToolDescriptor.model_validate(
        _descriptor_payload() | {"input_schema": schema}
    )

    assert descriptor.input_schema == schema


def test_provider_invocation_arguments_use_the_shared_json_bounds() -> None:
    invocation = ProviderToolInvocation(
        provider_name="example-provider",
        tool_name="read",
        arguments={"item_id": "one", "options": {"verbose": True}},
    )
    assert invocation.provider_name == "example-provider"
    assert invocation.tool_name == "read"
    assert invocation.name == "read"
    assert invocation.model_dump(by_alias=True) == {
        "provider_name": "example-provider",
        "name": "read",
        "arguments": {"item_id": "one", "options": {"verbose": True}},
    }

    with pytest.raises(ValidationError):
        ProviderToolInvocation(
            provider_name="example-provider",
            tool_name="read",
            arguments={"not_json": float("nan")},
        )


def test_provider_invocation_enforces_aggregate_json_byte_bound() -> None:
    with pytest.raises(ValidationError, match="invocation JSON exceeds maximum size"):
        ProviderToolInvocation(
            provider_name="example-provider",
            tool_name="read",
            arguments={"value": "x" * 65_500},
        )


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "execute",
        "handler",
        "module",
        "script",
        "shell",
        "command",
        "x-handler",
        "module.path",
        "command-line",
    ],
)
def test_provider_invocation_rejects_executable_argument_keys(forbidden_key: str) -> None:
    with pytest.raises(ValidationError):
        ProviderToolInvocation(
            provider_name="example-provider",
            tool_name="read",
            arguments={forbidden_key: "blocked"},
        )


def test_provider_invocation_allows_terminal_command_id_as_provider_data() -> None:
    invocation = ProviderToolInvocation(
        provider_name="example-provider",
        tool_name="read",
        arguments={"command_id": "pwd"},
    )

    assert invocation.arguments == {"command_id": "pwd"}


def test_provider_catalog_rejects_duplicate_internal_tool_names() -> None:
    first = ProviderToolDescriptor.model_validate(_descriptor_payload())
    second = ProviderToolDescriptor.model_validate(_descriptor_payload())

    with pytest.raises(ValidationError, match="duplicate internal tool name"):
        ProviderToolCatalog(tools=[first, second])


def test_provider_catalog_uses_dedicated_collection_bounds() -> None:
    """A realistic 60-tool inventory (cua-driver: 5303 nodes / 145 KB) must pass.

    The catalog is a collection: per-descriptor bounds (4096 nodes / 64 KB)
    applied to the whole dump would refuse any large real-world provider
    regardless of each tool's individual validity. The catalog validates with
    its own wider bounds while each descriptor stays unit-bounded.
    """

    def make_descriptor(index: int) -> ProviderToolDescriptor:
        payload = _descriptor_payload() | {
            "tool_name": f"tool-{index}",
            "description": "x" * 1000,
            "input_schema": {
                "type": "object",
                "properties": {
                    f"property_{i}": {
                        "type": "string",
                        "description": "property description " * 2,
                    }
                    for i in range(40)
                },
            },
        }
        return ProviderToolDescriptor.model_validate(payload)

    tools = [make_descriptor(i) for i in range(50)]
    catalog = ProviderToolCatalog(tools=tools)
    assert len(catalog.tools) == 50


def test_provider_catalog_still_rejects_unbounded_inventories() -> None:
    """The dedicated bounds stay finite: 4x the unit bounds, then refusal."""

    def make_descriptor(index: int) -> ProviderToolDescriptor:
        payload = _descriptor_payload() | {
            "tool_name": f"tool-{index}",
            "description": "x" * 2000,
            "input_schema": {
                "type": "object",
                "properties": {
                    f"property_{i}": {
                        "type": "string",
                        "description": "property description " * 10,
                    }
                    for i in range(80)
                },
            },
        }
        return ProviderToolDescriptor.model_validate(payload)

    tools = [make_descriptor(i) for i in range(120)]
    with pytest.raises(ValidationError, match="nodes|size|members"):
        ProviderToolCatalog(tools=tools)


def test_provider_catalog_allows_same_tool_name_for_different_providers() -> None:
    first = ProviderToolDescriptor.model_validate(_descriptor_payload())
    second = ProviderToolDescriptor.model_validate(
        _descriptor_payload()
        | {
            "provider_name": "other-provider",
        }
    )

    catalog = ProviderToolCatalog(tools=[first, second])

    assert [(tool.provider_name, tool.tool_name) for tool in catalog.tools] == [
        ("example-provider", "read"),
        ("other-provider", "read"),
    ]


def test_provider_catalog_accepts_distinct_tool_names_for_one_provider() -> None:
    first = ProviderToolDescriptor.model_validate(_descriptor_payload())
    second = ProviderToolDescriptor.model_validate(
        _descriptor_payload() | {"tool_name": "write"}
    )

    catalog = ProviderToolCatalog(tools=[first, second])

    assert [(tool.provider_name, tool.tool_name) for tool in catalog.tools] == [
        ("example-provider", "read"),
        ("example-provider", "write"),
    ]


def test_provider_catalog_and_invocation_schemas_are_closed() -> None:
    descriptor = ProviderToolDescriptor.model_validate(_descriptor_payload())

    with pytest.raises(ValidationError):
        ProviderToolCatalog(tools=[descriptor], extra=True)
    with pytest.raises(ValidationError):
        ProviderToolInvocation(
            provider_name="example-provider",
            tool_name="read",
            arguments={},
            execute="blocked",
        )


def test_shared_json_bounds_reject_depth_nodes_and_non_json_values() -> None:
    deep: list[object] = []
    cursor: list[object] = deep
    for _ in range(MAX_JSON_DEPTH):
        child: list[object] = []
        cursor.append(child)
        cursor = child
    with pytest.raises(ValueError, match="deeply nested"):
        validate_json_bounds(deep)

    too_many_nodes: list[object] = [[]]
    for _ in range(12):
        too_many_nodes = [too_many_nodes, too_many_nodes.copy()]
    with pytest.raises(ValueError, match="too many nodes"):
        validate_json_bounds(too_many_nodes)

    with pytest.raises(ValueError, match="JSON values"):
        validate_json_bounds({"value": object()})

    with pytest.raises(ValueError, match="JSON values"):
        validate_json_bounds({"value": math.inf})


def test_provider_tool_result_preserves_bounded_mcp_content() -> None:
    result = ProviderToolResult(
        content=[
            {"type": "text", "text": "hello"},
            {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
        ],
        structuredContent={"value": {"ok": True}},
        isError=False,
    )

    assert result.model_dump(by_alias=True) == {
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
        ],
        "structuredContent": {"value": {"ok": True}},
        "isError": False,
    }
    assert isinstance(result.content[0], ProviderTextContent)
    assert isinstance(result.content[1], ProviderImageContent)


def test_provider_tool_result_requires_content_and_supports_audio_content() -> None:
    with pytest.raises(ValidationError):
        ProviderToolResult.model_validate({})

    result = ProviderToolResult.model_validate(
        {
            "content": [
                {
                    "type": "audio",
                    "data": "YXVkaW8=",
                    "mimeType": "audio/wav",
                }
            ]
        }
    )

    assert type(result.content[0]).__name__ == "ProviderAudioContent"


def test_provider_result_preserves_native_code_and_uri_data() -> None:
    result = ProviderToolResult(
        content=[{"type": "text", "text": "provider output"}],
        structuredContent={
            "code": "E_PROVIDER",
            "command_id": "pwd",
            "uri": "file:///provider/output",
        },
    )

    assert result.structured_content == {
        "code": "E_PROVIDER",
        "command_id": "pwd",
        "uri": "file:///provider/output",
    }
    assert result.model_dump(by_alias=True)["structuredContent"] == {
        "code": "E_PROVIDER",
        "command_id": "pwd",
        "uri": "file:///provider/output",
    }


def test_provider_tool_result_rejects_unknown_fields_and_untrusted_resources() -> None:
    with pytest.raises(ValidationError):
        ProviderToolResult.model_validate(
            {"content": [{"type": "text", "text": "ok", "code": "x"}]}
        )

    # Unknown TOP-LEVEL fields are preserved pass-through (bounded, opaque):
    # the relay stays neutral with respect to provider results (2026-09-06).
    passthrough = ProviderToolResult.model_validate(
        {"content": [{"type": "text", "text": "ok"}], "extra": True}
    )
    assert passthrough.model_dump(by_alias=True)["extra"] is True

    with pytest.raises(ValidationError):
        ProviderToolResult.model_validate(
            {
                "content": [
                    {
                        "type": "resource",
                        "resource": {"uri": "file:///etc/passwd", "text": "secret"},
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        ProviderToolResult.model_validate(
            {
                "content": [
                    {
                        "type": "resource",
                        "resource": {"uri": "https://user:password@example.test/x"},
                    }
                ]
            }
        )


def test_resource_uri_validation_errors_do_not_render_credentials() -> None:
    credential_uri = "https://user:super-secret@example.test/resource?token=top-secret"

    with pytest.raises(ValidationError) as caught:
        ProviderToolResult.model_validate(
            {
                "content": [
                    {
                        "type": "resource",
                        "resource": {"uri": credential_uri, "text": "secret"},
                    }
                ]
            }
        )

    rendered = str(caught.value)
    assert credential_uri not in rendered
    assert "super-secret" not in rendered
    assert "top-secret" not in rendered


def test_provider_resource_content_allows_bounded_public_https_uri() -> None:
    result = ProviderToolResult(
        content=[
            {
                "type": "resource",
                "resource": {
                    "uri": "https://example.test/resource.json",
                    "mimeType": "application/json",
                    "text": "{}",
                },
            }
        ]
    )
    assert isinstance(result.content[0], ProviderResourceContent)
    assert result.model_dump(by_alias=True)["content"][0]["resource"]["uri"].startswith(
        "https://"
    )


def _assert_no_unbounded_additional_properties(schema: object) -> None:
    if isinstance(schema, dict):
        additional = schema.get("additionalProperties")
        # ProviderToolResult deliberately allows unknown TOP-LEVEL fields
        # (bounded pass-through); every other schema — and anything nested —
        # stays closed.
        if additional is True:
            assert schema.get("title") == "ProviderToolResult"
        for value in schema.values():
            _assert_no_unbounded_additional_properties(value)
    elif isinstance(schema, list):
        for value in schema:
            _assert_no_unbounded_additional_properties(value)


def _schema_property_name(model: type[object], field: str) -> str:
    model_field = model.model_fields[field]  # type: ignore[attr-defined]
    return model_field.serialization_alias or model_field.alias or field


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (ProviderToolDescriptor, "input_schema"),
        (ProviderToolDescriptor, "output_schema"),
        (ProviderToolDescriptor, "annotations"),
        (ProviderToolInvocation, "arguments"),
        (ProviderToolResult, "structured_content"),
        (ClientResult, "result"),
    ],
)
def test_generated_json_value_schemas_are_recursive_and_bounded(
    model: type[object], field: str
) -> None:
    schema = model.model_json_schema()  # type: ignore[attr-defined]

    _assert_no_unbounded_additional_properties(schema)
    assert "$defs" in schema
    property_name = _schema_property_name(model, field)
    assert "$ref" in str(schema["properties"][property_name])


def test_provider_content_blocks_preserve_mcp_metadata_aliases() -> None:
    result = ProviderToolResult.model_validate(
        {
            "content": [
                {
                    "type": "text",
                    "text": "hello",
                    "annotations": {"title": "Greeting"},
                    "meta": {"vendor": {"trace": "text"}},
                },
                {
                    "type": "image",
                    "data": "aGVsbG8=",
                    "mimeType": "image/png",
                    "annotations": {"title": "Preview"},
                    "_meta": {"vendor": {"trace": "image"}},
                },
                {
                    "type": "resource",
                    "resource": {
                        "uri": "https://example.test/resource.json",
                        "text": "{}",
                    },
                    "annotations": {"title": "Resource"},
                    "meta": {"vendor": {"trace": "resource"}},
                },
                {
                    "type": "resource_link",
                    "uri": "https://example.test/resource.json",
                    "name": "resource.json",
                    "annotations": {"title": "Link"},
                    "_meta": {"vendor": {"trace": "link"}},
                },
            ]
        }
    )

    assert isinstance(result.content[0], ProviderTextContent)
    assert isinstance(result.content[1], ProviderImageContent)
    assert isinstance(result.content[2], ProviderResourceContent)
    assert isinstance(result.content[3], ProviderResourceLinkContent)
    serialized_content = result.model_dump(by_alias=True)["content"]
    for content in serialized_content:
        assert "annotations" in content
        assert "_meta" in content
        assert "meta" not in content
    assert serialized_content[0]["_meta"]["vendor"]["trace"] == "text"
    assert serialized_content[1]["_meta"]["vendor"]["trace"] == "image"


def test_provider_generated_schemas_publish_wire_aliases_and_required_fields() -> None:
    descriptor_validation = ProviderToolDescriptor.model_json_schema(
        mode="validation", by_alias=True
    )
    descriptor_serialization = ProviderToolDescriptor.model_json_schema(
        mode="serialization", by_alias=True
    )
    for schema in (descriptor_validation, descriptor_serialization):
        properties = schema["properties"]
        assert "name" in properties
        assert "inputSchema" in properties
        assert "outputSchema" in properties
        assert "public_name" not in properties
        assert "input_schema" not in properties
        assert "output_schema" not in properties
        assert "risk_class" not in properties

    result_validation = ProviderToolResult.model_json_schema(
        mode="validation", by_alias=True
    )
    result_serialization = ProviderToolResult.model_json_schema(
        mode="serialization", by_alias=True
    )
    for schema in (result_validation, result_serialization):
        assert "content" in schema["required"]
        properties = schema["properties"]
        assert "structuredContent" in properties
        assert "isError" in properties
        assert "_meta" in properties
        assert "structured_content" not in properties
        assert "is_error" not in properties
        assert "meta" not in properties

    image_schema = ProviderImageContent.model_json_schema(
        mode="validation", by_alias=True
    )
    assert "mimeType" in image_schema["properties"]
    assert "mime_type" not in image_schema["properties"]


def test_provider_embedded_resource_schema_requires_exactly_one_body() -> None:
    schema = ProviderEmbeddedResource.model_json_schema(
        mode="validation", by_alias=True
    )

    assert schema["oneOf"] == [
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


def test_provider_embedded_resource_default_serialization_omits_null_body() -> None:
    resource = ProviderEmbeddedResource(
        uri="https://example.test/resource",
        text="hello",
    )

    serialized = resource.model_dump(by_alias=True)

    assert serialized["text"] == "hello"
    assert "blob" not in serialized


def test_provider_embedded_resource_rejects_explicit_null_body() -> None:
    with pytest.raises(ValidationError):
        ProviderEmbeddedResource.model_validate(
            {
                "uri": "https://example.test/resource",
                "text": "hello",
                "blob": None,
            }
        )


def test_provider_content_metadata_stays_within_shared_json_bounds() -> None:
    with pytest.raises(ValidationError):
        ProviderTextContent.model_validate(
            {
                "type": "text",
                "text": "hello",
                "_meta": {"items": list(range(MAX_JSON_COLLECTION_ITEMS + 1))},
            }
        )


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            ProviderTextContent,
            {"type": "text", "text": "🚀" * (MAX_TOOL_RESULT_BYTES // 4)},
        ),
        (
            ProviderImageContent,
            {
                "type": "image",
                "data": "A" * MAX_TOOL_RESULT_BYTES,
                "mimeType": "image/png",
            },
        ),
        (
            ProviderEmbeddedResource,
            {
                "uri": "https://example.test/resource",
                "text": "🚀" * (MAX_TOOL_RESULT_BYTES // 4),
            },
        ),
    ],
)
def test_direct_provider_content_models_enforce_shared_json_byte_bounds(
    model: type[object], payload: dict[str, object]
) -> None:
    # Honest-refusal contract: the oversized-refusal message names the bound
    # variable and the measured payload size (2026-09-09), not a generic
    # "JSON exceeds maximum size".
    with pytest.raises(ValidationError, match="RELAY_MAX_TOOL_RESULT_BYTES"):
        model.model_validate(payload)  # type: ignore[attr-defined]
    with pytest.raises(ValidationError, match="payload: "):
        model.model_validate(payload)  # type: ignore[attr-defined]


def _large_bounded_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            f"field_{index}": {"type": "string", "description": "x" * 1400}
            for index in range(24)
        },
        "additionalProperties": False,
    }


def test_provider_descriptor_enforces_aggregate_json_byte_bound() -> None:
    payload = _descriptor_payload()
    payload["input_schema"] = _large_bounded_schema()
    payload["output_schema"] = _large_bounded_schema()
    payload["annotations"] = {"description": "x" * 30000}

    with pytest.raises(ValidationError, match="descriptor JSON exceeds maximum size"):
        ProviderToolDescriptor.model_validate(payload)


def test_provider_catalog_enforces_aggregate_json_byte_bound() -> None:
    tools = []
    for index in range(10):
        payload = _descriptor_payload() | {
            "input_schema": _large_bounded_schema(),
            "tool_name": f"read-{index}",
        }
        tools.append(ProviderToolDescriptor.model_validate(payload))

    with pytest.raises(ValidationError, match="catalog JSON exceeds maximum size"):
        ProviderToolCatalog(tools=tools)


def test_provider_resource_link_requires_mcp_name() -> None:
    with pytest.raises(ValidationError):
        ProviderToolResult.model_validate(
            {
                "content": [
                    {
                        "type": "resource_link",
                        "uri": "https://example.test/resource.json",
                    }
                ]
            }
        )


def test_provider_resource_link_preserves_bounded_official_fields_and_metadata() -> None:
    result = ProviderToolResult.model_validate(
        {
            "content": [
                {
                    "type": "resource_link",
                    "uri": "https://example.test/resource.json",
                    "name": "resource.json",
                    "title": "Example resource",
                    "description": "A provider resource.",
                    "mimeType": "application/json",
                    "size": 42,
                    "icons": [
                        {
                            "src": "https://example.test/icon.png",
                            "mimeType": "image/png",
                            "sizes": "32x32",
                        }
                    ],
                    "_meta": {"vendor": {"trace": "link"}},
                },
                {
                    "type": "resource",
                    "resource": {
                        "uri": "https://example.test/resource.json",
                        "text": "{}",
                        "_meta": {"vendor": {"trace": "embedded"}},
                    },
                },
            ],
            "_meta": {"vendor": {"trace": "result"}},
        }
    )

    serialized = result.model_dump(by_alias=True, exclude_none=True)
    assert serialized["_meta"] == {"vendor": {"trace": "result"}}
    assert serialized["content"][0] == {
        "type": "resource_link",
        "uri": "https://example.test/resource.json",
        "name": "resource.json",
        "title": "Example resource",
        "description": "A provider resource.",
        "mimeType": "application/json",
        "size": 42,
        "icons": [
            {
                "src": "https://example.test/icon.png",
                "mimeType": "image/png",
                "sizes": "32x32",
            }
        ],
        "_meta": {"vendor": {"trace": "link"}},
    }
    assert serialized["content"][1]["resource"]["_meta"] == {
        "vendor": {"trace": "embedded"}
    }


@pytest.mark.parametrize(
    "query",
    [
        "token",
        "token=",
        "access_token",
        "access-token=",
        "api-key=",
        "api_key",
        "authToken=",
        "password",
        "secret=",
        "signature=",
        "client_secret",
        "refresh_token",
        "authorization",
        "bearer",
        "private_key",
        "oauth_token",
        "jwt",
        "client-secret",
        "refresh-token",
        "Authorization",
        "client%5Fsecret",
    ],
)
def test_resource_uri_rejects_credential_like_query_keys_with_blank_values(
    query: str,
) -> None:
    with pytest.raises(ValueError, match="credential-like"):
        validate_resource_uri(f"https://example.test/resource?{query}")


def test_resource_uri_allows_ordinary_query_keys() -> None:
    uri = "https://example.test/resource?page=1&filter=&view=summary"

    assert validate_resource_uri(uri) == uri
