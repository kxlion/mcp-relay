"""One ASGI app serving both Relay listeners, for in-process tests.

Production serves the MCP and WebSocket apps on separate sockets; tests that
drive both through a single TestClient or uvicorn server use this composite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mcp_relay.server import RelaySettings, _create_listener_apps


def create_app(settings: RelaySettings) -> FastAPI:
    mcp_app, ws_app = _create_listener_apps(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Starlette mounts never propagate lifespan events.
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.registry = mcp_app.state.registry
    app.state.settings = settings
    app.state.mcp = mcp_app.state.mcp
    app.state.mcp_app = mcp_app
    app.state.ws_app = ws_app
    # /ws must win over the catch-all MCP mount.
    for route in ws_app.router.routes:
        if getattr(route, "path", None) == "/ws":
            app.router.routes.append(route)
            break
    app.mount("/", mcp_app)
    return app
