from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from yggdrasil.config import DB_CONFIG
from yggdrasil.core.telemetry import instrument_engine

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(
            DB_CONFIG.dsn,
            pool_size=DB_CONFIG.pool_size,
            max_overflow=DB_CONFIG.pool_size,
            pool_pre_ping=True,
            pool_recycle=1800,
            # Errors would otherwise echo bound parameters (chat text, names) into logs and traces.
            hide_parameters=True,
        )
        instrument_engine(_engine)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def engine() -> AsyncEngine:
    return init_engine()


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Session that commits on success and rolls back on error."""
    init_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine, _sessionmaker = None, None
