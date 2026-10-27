"""Admin dashboard: JSON API under ``/api`` plus the built React SPA at ``/``."""

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute

from yggdrasil import __version__
from yggdrasil.admin import (
    admins,
    dashboard,
    events_api,
    groups,
    invocations,
    memory,
    prompts_api,
    providers,
    reviews,
    routes,
    sandbox_api,
    search_providers,
    settings_api,
    stickers,
    transcripts,
    triggers,
)
from yggdrasil.admin.auth import require_admin
from yggdrasil.config import APP_CONFIG
from yggdrasil.core.log import LOGGER

_ROUTERS = (
    dashboard.router,
    providers.router,
    search_providers.router,
    routes.router,
    settings_api.router,
    groups.router,
    triggers.router,
    prompts_api.router,
    admins.router,
    reviews.router,
    events_api.router,
    memory.router,
    invocations.router,
    stickers.router,
    sandbox_api.router,
    transcripts.router,
)
# Paths the SPA fallback must never shadow.
_RESERVED = ("api", "onebot")


def _operation_id(route: APIRoute) -> str:
    # Endpoint function names are unique; the generated TS client turns them into listProviders etc.
    return route.name


def build_api() -> FastAPI:
    """The admin API as a standalone ASGI app (also used to dump the OpenAPI document)."""
    api = FastAPI(
        title="Yggdrasil Admin",
        version=__version__,
        dependencies=[Depends(require_admin)],
        generate_unique_id_function=_operation_id,
        docs_url="/docs",
        redoc_url=None,
        servers=[{"url": "/api"}],
        openapi_url="/openapi.json",
    )
    for router in _ROUTERS:
        api.include_router(router)
    names = [r.name for r in api.routes if isinstance(r, APIRoute)]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise RuntimeError(f"duplicate admin operation ids: {duplicates}")
    return api


def _mount_spa(app: FastAPI, dist: Path) -> None:
    root = dist.resolve()
    index = root / "index.html"
    if not index.is_file():
        LOGGER.warning("dashboard build not found at {}; serving the API only", root)
        return

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        head = path.split("/", 1)[0]
        if head in _RESERVED:
            raise HTTPException(404)
        if path:
            file = (root / path).resolve()
            if file.is_relative_to(root) and file.is_file():
                cache = "public, max-age=31536000, immutable" if head == "assets" else "no-cache"
                return FileResponse(file, headers={"Cache-Control": cache})
            if head == "assets":  # a stale hashed chunk must not turn into index.html
                raise HTTPException(404)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def mount(app: FastAPI) -> None:
    """Attach the admin API at ``/api`` and the SPA at ``/``.

    Call after the OneBot adapter registered its routes: the SPA catch-all is a GET
    route added last, so it never shadows earlier routes or websockets.
    """
    app.mount("/api", build_api())
    _mount_spa(app, Path(APP_CONFIG.web_dist))
