"""Internal typing shared by local Relay capabilities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import uuid4

from pydantic import ValidationError

from ..json_bounds import JsonBoundsError, JsonValue
from ..output_models import ProviderToolResult
from ..protocol import InvokeMessage, ToolName
from ..provider_tools import ProviderToolDescriptor
from ..providers.base import (
    ProviderResultTooLargeError,
    UnknownProviderToolError,
    _bound_detail,
)


class LocalCapability(Protocol):
    tools: frozenset[ToolName]

    async def start(self) -> None: ...

    async def list_tools(self) -> Sequence[ProviderToolDescriptor]: ...

    async def invoke(self, message: InvokeMessage) -> dict[str, object]: ...

    async def wait_unavailable(self) -> None: ...

    async def aclose(self) -> None: ...


class CapabilityProviderClient:
    """Expose one in-process local capability behind the provider client boundary."""

    def __init__(
        self,
        capability: LocalCapability,
        descriptors: Sequence[ProviderToolDescriptor] = (),
    ) -> None:
        self._capability = capability
        self._descriptors = tuple(descriptors)
        self._tool_names = {
            descriptor.tool_name: descriptor for descriptor in self._descriptors
        }
        if not self._tool_names:
            self._tool_names = {tool: None for tool in capability.tools}

    @property
    def wire_names(self) -> tuple[str, ...]:
        return tuple(
            (
                f"{descriptor.provider_name}.{descriptor.tool_name}"
                if descriptor is not None
                else tool_name
            )
            for tool_name, descriptor in self._tool_names.items()
        )

    async def list_tools(self) -> Sequence[ProviderToolDescriptor]:
        return self._descriptors

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> ProviderToolResult:
        return await self.call_message(
            tool_name,
            arguments,
            request_id=f"provider-{uuid4().hex}",
        )

    async def call_message(
        self,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
        *,
        request_id: str,
    ) -> ProviderToolResult:
        descriptor = self._tool_names.get(tool_name)
        if tool_name not in self._tool_names:
            raise UnknownProviderToolError("unknown provider tool")
        wire_name = (
            f"{descriptor.provider_name}.{descriptor.tool_name}"
            if descriptor is not None
            else tool_name
        )
        result = await self._capability.invoke(
            InvokeMessage(
                version=2,
                type="invoke",
                request_id=request_id,
                tool_name=wire_name,
                arguments=dict(arguments),
            )
        )
        if isinstance(result, ProviderToolResult):
            return result
        try:
            return ProviderToolResult(content=[], structuredContent=result)
        except ValidationError as error:
            # Size refusals must surface as the honest closed error so the
            # client answers result_too_large instead of client_error. Size
            # refusals surface either as JsonBoundsError wrapping (explicit
            # validators) or as pydantic *_too_long types (Field bounds).
            for entry in error.errors():
                wrapped = entry.get("ctx", {}).get("error")
                if isinstance(wrapped, JsonBoundsError) or (
                    isinstance(wrapped, ValueError)
                    and isinstance(wrapped.__cause__, JsonBoundsError)
                ):
                    detail = ""
                    if isinstance(wrapped, JsonBoundsError):
                        detail = _bound_detail(wrapped)
                    elif (
                        isinstance(wrapped, ValueError)
                        and isinstance(wrapped.__cause__, JsonBoundsError)
                    ):
                        detail = _bound_detail(wrapped.__cause__)
                    raise ProviderResultTooLargeError(detail) from None
                if entry.get("type") in {
                    "string_too_long",
                    "too_long",
                    "list_too_long",
                    "set_too_long",
                }:
                    raise ProviderResultTooLargeError() from None
            raise

    async def close(self) -> None:
        # RelayClient owns lifecycle for the underlying LocalCapability.
        return None
