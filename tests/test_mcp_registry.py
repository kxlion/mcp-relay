"""Tests for the official MCP Registry client used by relay_registry_search.

The network is never touched: every test drives the client through an
httpx2 MockTransport fixture shaped like the verified live API.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError
from typing_extensions import AsyncIterator

from mcp_relay.mcp_registry import (
    DEFAULT_REGISTRY_BASE_URL,
    MAX_LIMIT,
    MAX_PACKAGE_COUNT,
    MAX_PAGE_BYTES,
    MAX_QUERY_LENGTH,
    RegistryUnreachableError,
    declarative_launcher,
    launcher_for_registry_type,
    lookup_registry_server,
    search_registry_servers,
)


def _server_entry(
    name: str = "io.example/author/server",
    version: str = "1.2.3",
    *,
    description: str = "A test server.",
    title: str | None = "Test Server",
    repository_url: str | None = "https://github.com/example/server",
    packages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    server: dict[str, Any] = {
        "name": name,
        "description": description,
        "version": version,
    }
    if title is not None:
        server["title"] = title
    if repository_url is not None:
        server["repository"] = {"url": repository_url, "source": "github"}
    if packages is not None:
        server["packages"] = packages
    return {
        "server": server,
        "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "status": "active",
                "isLatest": True,
            }
        },
    }


def _page(entries: list[dict[str, Any]], next_cursor: str | None = None) -> bytes:
    metadata = {"count": len(entries)}
    if next_cursor is not None:
        metadata["nextCursor"] = next_cursor
    return json.dumps({"servers": entries, "metadata": metadata}).encode()


def _transport(
    page: bytes | Exception,
    *,
    status_code: int = 200,
    seen: list[httpx2.Request] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        if isinstance(page, Exception):
            raise page
        return httpx2.Response(status_code, content=page, headers=headers)

    return httpx2.MockTransport(handler)


class _CountingStreamTransport(httpx2.AsyncBaseTransport):
    """Serve ``total_bytes`` as a chunked stream and record bytes pulled.

    ``pulled`` only grows while a consumer actually reads the stream, so a
    client that aborts mid-download (bounded streaming) stays far below
    ``total_bytes`` while a client that buffers everything reads it all.
    """

    def __init__(
        self, total_bytes: int, chunk_size: int = 65536, *, declared_length: int | None = None
    ) -> None:
        self.total_bytes = total_bytes
        self.chunk_size = chunk_size
        self.declared_length = declared_length
        self.pulled = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        transport = self
        full_chunk = b"x" * self.chunk_size

        async def body() -> AsyncIterator[bytes]:
            remaining = self.total_bytes
            while remaining > 0:
                size = min(self.chunk_size, remaining)
                transport.pulled += size
                remaining -= size
                yield full_chunk[:size]

        headers = (
            {"content-length": str(self.declared_length)}
            if self.declared_length is not None
            else None
        )
        return httpx2.Response(200, content=body(), headers=headers)


def test_launcher_mapping_covers_launchable_registry_types() -> None:
    assert launcher_for_registry_type("npm") == ("npx", "-y")
    assert launcher_for_registry_type("pypi") == ("uvx",)


@pytest.mark.parametrize("registry_type", ["docker", "oci", "binary", ""])
def test_launcher_mapping_rejects_unknown_registry_types(registry_type: str) -> None:
    assert launcher_for_registry_type(registry_type) is None


def test_declarative_launcher_builds_npm_command_with_version() -> None:
    package: dict[str, Any] = {
        "registryType": "npm",
        "identifier": "server-pkg",
        "version": "1.2.3",
    }
    launcher = declarative_launcher(package)  # type: ignore[arg-type]
    assert launcher == ["npx", "-y", "server-pkg@1.2.3"]


def test_declarative_launcher_builds_pypi_command_without_version() -> None:
    package: dict[str, Any] = {"registryType": "pypi", "identifier": "server-pkg"}
    launcher = declarative_launcher(package)  # type: ignore[arg-type]
    assert launcher == ["uvx", "server-pkg"]


def test_declarative_launcher_rejects_unlaunchable_registry_type() -> None:
    package: dict[str, Any] = {"registryType": "docker", "identifier": "image"}
    launcher = declarative_launcher(package)
    assert launcher is None


def test_search_queries_the_verified_endpoint_with_fiche_parameters() -> None:
    seen: list[httpx2.Request] = []
    transport = _transport(_page([_server_entry()]), seen=seen)

    result = search_registry_servers_sync_helper(
        transport, query="blender", limit=2, cursor="abc:1"
    )

    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/v0/servers"
    params = dict(request.url.params)
    assert params["search"] == "blender"
    assert params["limit"] == "2"
    assert params["cursor"] == "abc:1"
    assert "version" not in params
    assert "updated_since" not in params
    assert "include_deleted" not in params
    assert result.next_cursor is None
    assert len(result.results) == 1
    entry = result.results[0]
    assert entry.name == "io.example/author/server"
    assert entry.title == "Test Server"
    assert entry.description == "A test server."
    assert entry.version == "1.2.3"
    assert entry.repository_url == "https://github.com/example/server"
    assert entry.packages == []


def test_search_passes_version_updated_since_and_include_deleted() -> None:
    seen: list[httpx2.Request] = []
    transport = _transport(_page([]), seen=seen)

    search_registry_servers_sync_helper(
        transport,
        query="x",
        version="latest",
        updated_since="2026-08-01T00:00:00Z",
        include_deleted=False,
    )

    params = dict(seen[0].url.params)
    assert params["version"] == "latest"
    assert params["updated_since"] == "2026-08-01T00:00:00Z"
    # updated_since implies include_deleted=true on the live API.
    assert params["include_deleted"] == "true"


def test_search_updated_since_false_positive_guard_keeps_explicit_flag() -> None:
    seen: list[httpx2.Request] = []
    transport = _transport(_page([]), seen=seen)

    search_registry_servers_sync_helper(
        transport, query="x", include_deleted=True
    )

    params = dict(seen[0].url.params)
    assert params["include_deleted"] == "true"


def test_search_parses_packages_and_next_cursor() -> None:
    page = _page(
        [
            _server_entry(
                packages=[
                    {
                        "registryType": "npm",
                        "identifier": "server-pkg",
                        "version": "1.2.3",
                    },
                    {"registryType": "pypi", "identifier": "server-py"},
                ]
            )
        ],
        next_cursor="io.example/author/server:1.2.3",
    )
    transport = _transport(page)

    result = search_registry_servers_sync_helper(transport, query="server")

    assert result.next_cursor == "io.example/author/server:1.2.3"
    packages = result.results[0].packages
    assert packages is not None
    assert [p.registry_type for p in packages] == ["npm", "pypi"]
    assert packages[0].identifier == "server-pkg"
    assert packages[0].version == "1.2.3"
    assert packages[1].version is None


def test_search_sends_the_tool_default_limit() -> None:
    seen: list[httpx2.Request] = []
    transport = _transport(_page([]), seen=seen)

    search_registry_servers_sync_helper(transport, query="x")

    assert dict(seen[0].url.params)["limit"] == "10"


def test_search_skips_malformed_entries_without_failing_the_page() -> None:
    page = _page(
        [
            {"not": "a server record"},
            _server_entry(name="a" * 300),  # overlong name: skipped
            _server_entry(name="ok.example/kept"),
        ]
    )
    transport = _transport(page)

    result = search_registry_servers_sync_helper(transport, query="x")

    assert [s.name for s in result.results] == ["ok.example/kept"]


def test_search_truncates_oversized_pages_and_drops_the_cursor() -> None:
    entries = [_server_entry(name=f"io.example/i/{i}") for i in range(7)]
    page = json.dumps(
        {"servers": entries, "metadata": {"count": 7, "nextCursor": "c2"}}
    ).encode()
    transport = _transport(page)

    result = search_registry_servers_sync_helper(transport, query="x", limit=2)

    assert len(result.results) == 2
    assert result.next_cursor is None


def test_search_returns_none_cursor_when_metadata_has_none() -> None:
    transport = _transport(json.dumps({"servers": []}).encode())
    result = search_registry_servers_sync_helper(transport, query="x")
    assert result.results == []
    assert result.next_cursor is None


@pytest.mark.parametrize(
    "page",
    [
        httpx2.ConnectError("connection refused"),
        httpx2.ReadTimeout("timed out"),
        b"<html>not json</html>",
        b'{"servers": "not-a-list"}',
        b"[1, 2, 3]",
    ],
)
def test_search_maps_registry_failures_to_registry_unreachable(
    page: bytes | Exception,
) -> None:
    transport = _transport(page)
    with pytest.raises(RegistryUnreachableError) as excinfo:
        search_registry_servers_sync_helper(transport, query="x")
    assert excinfo.value.code == "registry_unreachable"


@pytest.mark.parametrize("status_code", [500, 503, 404, 400])
def test_search_maps_http_error_statuses_to_registry_unreachable(
    status_code: int,
) -> None:
    transport = _transport(b"{}", status_code=status_code)
    with pytest.raises(RegistryUnreachableError):
        search_registry_servers_sync_helper(transport, query="x")


def test_search_rejects_out_of_bounds_arguments() -> None:
    transport = _transport(_page([]))
    with pytest.raises(ValidationError):
        search_registry_servers_sync_helper(transport, query="q" * (MAX_QUERY_LENGTH + 1))
    with pytest.raises(ValidationError):
        search_registry_servers_sync_helper(transport, query="x", limit=MAX_LIMIT + 1)
    with pytest.raises(ValidationError):
        search_registry_servers_sync_helper(transport, query="")


def test_search_rejects_malformed_updated_since() -> None:
    transport = _transport(_page([]))
    with pytest.raises(ValidationError):
        search_registry_servers_sync_helper(transport, query="x", updated_since="yesterday")


def test_search_skips_entries_exceeding_the_package_cap() -> None:
    packages = [
        {"registryType": "npm", "identifier": f"pkg-{index}"}
        for index in range(MAX_PACKAGE_COUNT + 1)
    ]
    page = _page(
        [
            _server_entry(name="io.example/over", packages=packages),
            _server_entry(name="io.example/ok"),
        ]
    )
    transport = _transport(page)

    result = search_registry_servers_sync_helper(transport, query="x")

    # An entry beyond the declared package bound is skipped, not truncated.
    assert [entry.name for entry in result.results] == ["io.example/ok"]


def test_search_keeps_entries_at_the_package_cap() -> None:
    packages = [
        {"registryType": "npm", "identifier": f"pkg-{index}"}
        for index in range(MAX_PACKAGE_COUNT)
    ]
    page = _page([_server_entry(name="io.example/full", packages=packages)])
    transport = _transport(page)

    result = search_registry_servers_sync_helper(transport, query="x")

    assert len(result.results) == 1
    assert len(result.results[0].packages) == MAX_PACKAGE_COUNT


def test_search_aborts_oversized_streams_before_reading_them_fully() -> None:
    transport = _CountingStreamTransport(
        total_bytes=MAX_PAGE_BYTES + 2 * 1024 * 1024
    )

    with pytest.raises(RegistryUnreachableError):
        search_registry_servers_sync_helper(transport, query="x")

    # The size cap must interrupt the download itself: the client may never
    # pull the whole oversized body into memory before rejecting it.
    assert transport.pulled <= MAX_PAGE_BYTES + transport.chunk_size


def test_search_rejects_declared_oversized_pages_before_reading() -> None:
    transport = _CountingStreamTransport(
        total_bytes=MAX_PAGE_BYTES + 1024 * 1024,
        declared_length=MAX_PAGE_BYTES + 1,
    )

    with pytest.raises(RegistryUnreachableError):
        search_registry_servers_sync_helper(transport, query="x")

    # The declared size is enough to refuse the page without pulling a byte.
    assert transport.pulled == 0


def test_default_base_url_is_the_official_registry() -> None:
    assert DEFAULT_REGISTRY_BASE_URL == "https://registry.modelcontextprotocol.io"


def test_search_targets_the_configured_base_url() -> None:
    seen: list[httpx2.Request] = []
    transport = _transport(_page([]), seen=seen)
    search_registry_servers_sync_helper(
        transport, query="x", base_url="http://registry.test"
    )
    assert str(seen[0].url).startswith("http://registry.test/v0/servers")


def search_registry_servers_sync_helper(
    transport: httpx2.MockTransport | httpx2.AsyncBaseTransport,
    *,
    query: str,
    limit: int | None = None,
    cursor: str | None = None,
    version: str | None = None,
    updated_since: str | None = None,
    include_deleted: bool = False,
    base_url: str = DEFAULT_REGISTRY_BASE_URL,
) -> Any:
    """Run one async search call synchronously for test readability."""

    import asyncio

    async def scenario() -> Any:
        return await search_registry_servers(
            query,
            base_url=base_url,
            timeout_seconds=2.0,
            limit=limit,
            cursor=cursor,
            version=version,
            updated_since=updated_since,
            include_deleted=include_deleted,
            transport=transport,
        )

    return asyncio.run(scenario())


# --------------------------------------------------------------------------
# Exact-source lookup (client-side spawn-time resolution)
# --------------------------------------------------------------------------


def _server_record(name: str, *, version: str = "1.2.3") -> dict[str, Any]:
    return {
        "server": {
            "name": name,
            "description": "record",
            "version": version,
            "packages": [
                {"registryType": "npm", "identifier": "server-pkg", "version": version}
            ],
        }
    }


def test_lookup_hits_the_exact_version_endpoint() -> None:
    seen: list[httpx2.Request] = []
    transport = _transport(
        json.dumps(_server_record("io.example/author/server")).encode(),
        seen=seen,
    )
    result = asyncio.run(
        lookup_registry_server(
            "io.example/author/server",
            version="1.2.3",
            base_url="https://registry.example.test",
            transport=transport,
        )
    )
    assert result is not None
    assert result.name == "io.example/author/server"
    assert result.packages[0].identifier == "server-pkg"
    assert seen[0].url.path == "/v0/servers/io.example/author/server/versions/1.2.3"


def test_lookup_defaults_to_latest_version() -> None:
    seen: list[httpx2.Request] = []
    transport = _transport(
        json.dumps(_server_record("io.example/author/server")).encode(),
        seen=seen,
    )
    asyncio.run(
        lookup_registry_server(
            "io.example/author/server",
            base_url="https://registry.example.test",
            transport=transport,
        )
    )
    assert seen[0].url.path.endswith("/versions/latest")


def test_lookup_encodes_reverse_dns_source_safely() -> None:
    seen: list[httpx2.Request] = []
    transport = _transport(
        json.dumps(_server_record("io.example/author/server")).encode(),
        seen=seen,
    )
    asyncio.run(
        lookup_registry_server(
            "io.example/author/server",
            base_url="https://registry.example.test",
            transport=transport,
        )
    )
    assert " " not in seen[0].url.path


def test_lookup_returns_none_on_unknown_source() -> None:
    transport = _transport(b'{"error": "not found"}', status_code=404)
    result = asyncio.run(
        lookup_registry_server(
            "io.example/ghost/server",
            base_url="https://registry.example.test",
            transport=transport,
        )
    )
    assert result is None


@pytest.mark.parametrize("status_code", [500, 503])
def test_lookup_maps_http_failures_to_registry_unreachable(status_code: int) -> None:
    transport = _transport(b"boom", status_code=status_code)
    with pytest.raises(RegistryUnreachableError):
        asyncio.run(
            lookup_registry_server(
                "io.example/author/server",
                base_url="https://registry.example.test",
                transport=transport,
            )
        )


def test_lookup_maps_network_failures_to_registry_unreachable() -> None:
    transport = _transport(httpx2.ConnectError("offline"))
    with pytest.raises(RegistryUnreachableError):
        asyncio.run(
            lookup_registry_server(
                "io.example/author/server",
                base_url="https://registry.example.test",
                transport=transport,
            )
        )


def test_lookup_rejects_oversized_pages() -> None:
    transport = _CountingStreamTransport(6 * 1024 * 1024)
    with pytest.raises(RegistryUnreachableError):
        asyncio.run(
            lookup_registry_server(
                "io.example/author/server",
                base_url="https://registry.example.test",
                transport=transport,
            )
        )


def test_lookup_rejects_malformed_pages() -> None:
    transport = _transport(b"not json")
    with pytest.raises(RegistryUnreachableError):
        asyncio.run(
            lookup_registry_server(
                "io.example/author/server",
                base_url="https://registry.example.test",
                transport=transport,
            )
        )
