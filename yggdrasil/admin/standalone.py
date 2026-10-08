"""Dashboard without the bot, for frontend development and smoke tests::

uv run uvicorn yggdrasil.admin.standalone:app --port 8192
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from yggdrasil import runtime
from yggdrasil.admin import mount
from yggdrasil.core.log import setup_logging
from yggdrasil.db.session import dispose_engine


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    await runtime.load()
    listener = asyncio.create_task(runtime.listen_forever())
    try:
        yield
    finally:
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    mount(app)
    return app


app = create_app()
