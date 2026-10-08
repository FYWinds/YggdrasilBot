import asyncio
import re
import warnings

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import yggdrasil.db.models  # noqa: F401  (register tables)
from yggdrasil.config import DB_CONFIG
from yggdrasil.db.base import Base

config = context.config
config.set_main_option("sqlalchemy.url", DB_CONFIG.dsn.replace("%", "%%"))
target_metadata = Base.metadata
warnings.filterwarnings("ignore", message="Did not recognize type 'geometry'")

_PARTITION = re.compile(r"^messages_\d{4}_\d{2}$")


def include_object(obj, name, type_, reflected, compare_to):
    # Never drop objects we don't own (the ParadeDB image ships PostGIS/tiger tables).
    if reflected and compare_to is None:
        return False
    # Monthly partitions are managed by yggdrasil.db.partitions, BM25 indexes by raw SQL.
    if type_ == "table" and name and _PARTITION.match(name):
        return False
    if type_ == "index" and name and (name.endswith("_bm25") or _PARTITION.match(name.rsplit("_", 1)[0])):
        return False
    return True


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
