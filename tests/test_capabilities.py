"""Generic adapter tests: no native System/Terminal implementation is required."""
from __future__ import annotations

import asyncio

import pytest

from mcp_relay.capabilities.base import CapabilityProviderClient, LocalCapability
from mcp_relay.protocol import InvokeMessage
from mcp_relay.provider_tools import ProviderToolDescriptor
from mcp_relay.providers.base import UnknownProviderToolError


class SyntheticCapability:
    tools = frozenset({"sample.echo"})

    def __init__(self):
        self.messages = []
        self.closed = 0

    async def list_tools(self):
        return [ProviderToolDescriptor(
            provider_name="sample", tool_name="echo",
            description="Synthetic echo", input_schema={"type": "object", "additionalProperties": False},
        )]

    async def start(self):
        pass

    async def wait_unavailable(self):
        await asyncio.Event().wait()

    async def invoke(self, message: InvokeMessage):
        self.messages.append(message)
        return {"echo": True}

    async def aclose(self):
        self.closed += 1


def test_local_capability_protocol_is_generic_and_typed():
    capability: LocalCapability = SyntheticCapability()
    assert capability.tools == frozenset({"sample.echo"})


def test_adapter_preserves_qualified_tool_arguments_and_request_id():
    async def scenario():
        capability = SyntheticCapability()
        client = CapabilityProviderClient(capability, await capability.list_tools())
        result = await client.call_message("echo", {}, request_id="correlation")
        assert result.structured_content == {"echo": True}
        assert capability.messages == [InvokeMessage(
            version=2, type="invoke", request_id="correlation", tool_name="sample.echo", arguments={},
        )]
        assert client.wire_names == ("sample.echo",)
    asyncio.run(scenario())


def test_adapter_rejects_unknown_tools_without_invoking():
    async def scenario():
        capability = SyntheticCapability()
        client = CapabilityProviderClient(capability, await capability.list_tools())
        with pytest.raises(UnknownProviderToolError):
            await client.call_tool("unannounced", {})
        assert not capability.messages
    asyncio.run(scenario())


def test_adapter_does_not_close_capability_owned_by_client():
    async def scenario():
        capability = SyntheticCapability()
        client = CapabilityProviderClient(capability, await capability.list_tools())
        await client.close()
        assert capability.closed == 0
    asyncio.run(scenario())
