"""Read-only client for the official MCP Registry (server-side discovery).

The Relay Server performs registry search itself; the result is returned to
the agent without ever touching the agent channel or the local YAML. Bounds
mirror the closed-model contract used across the Relay surface: bounded
queries, bounded result counts, bounded field lengths, unexpected tool
arguments rejected, unknown response fields never forwarded.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Annotated, Any
from urllib.parse import quote

import httpx2
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from .diagnostics import info as _info_log

DEFAULT_REGISTRY_BASE_URL = "https://registry.modelcontextprotocol.io"
SEARCH_PATH = "/v0/servers"

MAX_QUERY_LENGTH = 200
MIN_LIMIT = 1
MAX_LIMIT = 50
MAX_CURSOR_LENGTH = 512
MAX_VERSION_LENGTH = 64
MAX_NAME_LENGTH = 255
MAX_TITLE_LENGTH = 128
MAX_DESCRIPTION_LENGTH = 512
MAX_PACKAGE_COUNT = 8
MAX_URL_LENGTH = 2048

DEFAULT_SEARCH_LIMIT = 10
#: Hard cap on one registry response; larger answers are treated as unusable.
MAX_PAGE_BYTES = 5 * 1024 * 1024

_REGISTRY_TIMEOUT_SECONDS = 5.0

_LAUNCHER_BY_REGISTRY_TYPE: dict[str, tuple[str, ...]] = {
    # declarative launchers only: the hub never installs anything itself
    "npm": ("npx", "-y"),
    "pypi": ("uvx",),
}

_INVALID_PAGE = "registry returned an invalid page"
_REDACTED = "registry_unreachable"


class RegistryUnreachableError(Exception):
    """The official registry could not be reached or answered unusable data."""

    code = "registry_unreachable"
    suggested_action = "retry_later"

    def __init__(self, detail: str) -> None:
        super().__init__(_REDACTED)
        self.detail = detail


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        strict=True,
        hide_input_in_errors=True,
    )


class RegistrySearchInput(_ClosedModel):
    """Closed input model for the public ``relay_registry_search`` tool.

    ``extra="forbid"`` makes every unexpected argument a tool error, matching
    the facade's closed-schema contract (``additionalProperties: false``).
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
    )

    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_LENGTH)]
    limit: Annotated[int, Field(ge=MIN_LIMIT, le=MAX_LIMIT)] = DEFAULT_SEARCH_LIMIT
    cursor: (
        Annotated[str, Field(min_length=1, max_length=MAX_CURSOR_LENGTH)] | None
    ) = None
    version: (
        Annotated[str, Field(min_length=1, max_length=MAX_VERSION_LENGTH)] | None
    ) = None
    updated_since: (
        Annotated[str, Field(min_length=1, max_length=MAX_VERSION_LENGTH)] | None
    ) = None
    include_deleted: bool = False

    @field_validator("updated_since")
    @classmethod
    def _check_updated_since(cls, value: str | None) -> str | None:
        if value is not None and not _is_http_date(value):
            raise ValueError("updated_since must be an RFC3339 datetime")
        return value


class RegistryPackage(_ClosedModel):
    # The live API publishes camelCase keys; the Relay surface stays snake_case.
    registry_type: Annotated[
        str, Field(min_length=1, max_length=32, validation_alias=AliasChoices(
            "registry_type", "registryType"
        ))
    ]
    identifier: Annotated[str, Field(min_length=1, max_length=214)]
    version: Annotated[str, Field(min_length=1, max_length=MAX_VERSION_LENGTH)] | None = None


class RegistryServerSummary(_ClosedModel):
    name: Annotated[str, Field(min_length=1, max_length=MAX_NAME_LENGTH)]
    title: Annotated[str, Field(min_length=1, max_length=MAX_TITLE_LENGTH)] | None = None
    description: Annotated[str, Field(min_length=1, max_length=MAX_DESCRIPTION_LENGTH)]
    version: Annotated[str, Field(min_length=1, max_length=MAX_VERSION_LENGTH)] | None = None
    repository_url: (
        Annotated[str, Field(min_length=9, max_length=MAX_URL_LENGTH)] | None
    ) = None
    packages: list[RegistryPackage] = Field(default_factory=list)


class RegistrySearchResult(_ClosedModel):
    results: list[RegistryServerSummary]
    next_cursor: (
        Annotated[str, Field(min_length=1, max_length=MAX_CURSOR_LENGTH)] | None
    ) = None


def launcher_for_registry_type(registry_type: str) -> tuple[str, ...] | None:
    """Map a registry package type to its declarative launcher, if any."""
    return _LAUNCHER_BY_REGISTRY_TYPE.get(registry_type)


def declarative_launcher(
    package: RegistryPackage | Mapping[str, Any],
) -> list[str] | None:
    """Build the argv of the declarative launcher for one registry package.

    Accepts a validated model or a raw registry mapping (camelCase keys are
    normalized). The launcher fetches the package at first spawn inside the
    alias cache directory; nothing is installed by the relay itself.
    """
    if not isinstance(package, RegistryPackage):
        package = RegistryPackage.model_validate(package)
    launcher = launcher_for_registry_type(package.registry_type)
    if launcher is None:
        return None
    if package.version is None:
        return [*launcher, package.identifier]
    return [
        *launcher,
        f"{package.identifier}@{package.version}",
    ]


def _is_http_date(value: str) -> bool:
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _repository_url(raw: dict[str, Any]) -> str | None:
    repository = raw.get("repository")
    if not isinstance(repository, dict):
        return None
    url = repository.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    return None


def _parse_server_entry(entry: Any) -> RegistryServerSummary | None:
    if not isinstance(entry, dict):
        return None
    server = entry.get("server")
    if not isinstance(server, dict):
        return None
    name = server.get("name")
    description = server.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        return None
    raw_packages = server.get("packages")
    if raw_packages is not None and not isinstance(raw_packages, list):
        return None
    if raw_packages is not None and len(raw_packages) > MAX_PACKAGE_COUNT:
        # An entry beyond the declared package bound is unreliable data: skip
        # the whole record instead of silently truncating its package list.
        return None
    title = server.get("title")
    version = server.get("version")
    try:
        return RegistryServerSummary(
            name=name,
            title=title if isinstance(title, str) else None,
            description=description,
            version=version if isinstance(version, str) else None,
            repository_url=_repository_url(server),
            packages=raw_packages if raw_packages is not None else [],
        )
    except ValidationError:
        # One malformed record must not fail the whole page.
        return None


def _bounded_page(
    entries: list[Any], limit: int
) -> tuple[list[RegistryServerSummary], bool]:
    summaries: list[RegistryServerSummary] = []
    for entry in entries:
        summary = _parse_server_entry(entry)
        if summary is not None:
            summaries.append(summary)
    truncated = False
    if len(summaries) > limit:
        summaries = summaries[:limit]
        truncated = True
    return summaries, truncated


_PAGE_ADAPTER = TypeAdapter(dict[str, Any])


def _declared_page_size(response: httpx2.Response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def _read_bounded(response: httpx2.Response) -> bytes:
    """Read one registry page without ever buffering beyond the size cap."""
    declared = _declared_page_size(response)
    if declared is not None and declared > MAX_PAGE_BYTES:
        raise RegistryUnreachableError("registry page exceeds the size bound")
    chunks: list[bytes] = []
    received = 0
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        if received > MAX_PAGE_BYTES:
            raise RegistryUnreachableError("registry page exceeds the size bound")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_page(payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_PAGE_BYTES:
        raise RegistryUnreachableError("registry page exceeds the size bound")
    try:
        page = _PAGE_ADAPTER.validate_json(payload)
    except (ValidationError, ValueError):
        raise RegistryUnreachableError(_INVALID_PAGE) from None
    servers = page.get("servers")
    if not isinstance(servers, list):
        raise RegistryUnreachableError(_INVALID_PAGE)
    return page


def _cursor_from(page: dict[str, Any]) -> str | None:
    metadata = page.get("metadata")
    if not isinstance(metadata, dict):
        return None
    cursor = metadata.get("nextCursor")
    return cursor if isinstance(cursor, str) and cursor else None


async def search_registry_servers(
    query: str,
    *,
    base_url: str = DEFAULT_REGISTRY_BASE_URL,
    timeout_seconds: float = _REGISTRY_TIMEOUT_SECONDS,
    limit: int | None = None,
    cursor: str | None = None,
    version: str | None = None,
    updated_since: str | None = None,
    include_deleted: bool = False,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> RegistrySearchResult:
    """Search the official registry; read-only, server-side, never relayed."""
    query_fields: dict[str, Any] = {
        "query": query,
        "cursor": cursor,
        "version": version,
        "updated_since": updated_since,
        "include_deleted": include_deleted,
    }
    if limit is not None:
        query_fields["limit"] = limit
    registry_query = RegistrySearchInput(**query_fields)

    params: dict[str, str] = {"search": registry_query.query}
    # The tool contract owns the default page size (fiche: 10), so the
    # registry's own default (30) is never exercised through this client.
    params["limit"] = str(registry_query.limit)
    if registry_query.cursor is not None:
        params["cursor"] = registry_query.cursor
    if registry_query.version is not None:
        params["version"] = registry_query.version
    if registry_query.updated_since is not None:
        params["updated_since"] = registry_query.updated_since
        # Verified live behaviour: incremental sync implies deleted records.
        params["include_deleted"] = "true"
    elif registry_query.include_deleted:
        params["include_deleted"] = "true"

    url = base_url.rstrip("/") + SEARCH_PATH
    try:
        async with httpx2.AsyncClient(
            transport=transport, timeout=timeout_seconds, follow_redirects=False
        ) as client:
            request = client.build_request("GET", url, params=params)
            response = await client.send(request, stream=True)
            _info_log(f"registry request: GET {request.url} -> {response.status_code}")
        try:
            if response.status_code < 200 or response.status_code >= 300:
                raise RegistryUnreachableError(f"status {response.status_code}")
            page_payload = await _read_bounded(response)
        finally:
            await response.aclose()
    except (httpx2.HTTPError, httpx2.StreamError, OSError) as exc:
        raise RegistryUnreachableError(type(exc).__name__) from None

    page = _validate_page(page_payload)
    summaries, truncated = _bounded_page(page["servers"], registry_query.limit)
    next_cursor = None if truncated else _cursor_from(page)
    return RegistrySearchResult(results=summaries, next_cursor=next_cursor)


async def lookup_registry_server(
    source: str,
    *,
    version: str | None = None,
    base_url: str = DEFAULT_REGISTRY_BASE_URL,
    timeout_seconds: float = _REGISTRY_TIMEOUT_SECONDS,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> RegistryServerSummary | None:
    """Resolve one exact registry source at spawn time; read-only, bounded.

    Returns ``None`` when the registry knows no such source. Reachability and
    unusable-data failures raise ``RegistryUnreachableError``. The page is
    consumed as a bounded stream with the same 5 MiB cap as search.
    """
    if not source or len(source) > MAX_NAME_LENGTH:
        raise RegistryUnreachableError("invalid source")
    selected = version if version else "latest"
    url = (
        base_url.rstrip("/")
        + f"/v0/servers/{quote(source, safe='')}/versions/{quote(selected, safe='')}"
    )
    try:
        async with httpx2.AsyncClient(
            transport=transport, timeout=timeout_seconds, follow_redirects=False
        ) as client:
            request = client.build_request("GET", url)
            response = await client.send(request, stream=True)
            _info_log(f"registry request: GET {request.url} -> {response.status_code}")
        try:
            if response.status_code == 404:
                return None
            if response.status_code < 200 or response.status_code >= 300:
                raise RegistryUnreachableError(f"status {response.status_code}")
            page_payload = await _read_bounded(response)
        finally:
            await response.aclose()
    except (httpx2.HTTPError, httpx2.StreamError, OSError) as exc:
        raise RegistryUnreachableError(type(exc).__name__) from None

    try:
        page = _PAGE_ADAPTER.validate_json(page_payload)
    except (ValidationError, ValueError):
        raise RegistryUnreachableError(_INVALID_PAGE) from None
    server = page.get("server")
    if not isinstance(server, dict):
        raise RegistryUnreachableError(_INVALID_PAGE)
    record = _parse_server_entry(page)
    if record is None:
        raise RegistryUnreachableError(_INVALID_PAGE)
    return record
