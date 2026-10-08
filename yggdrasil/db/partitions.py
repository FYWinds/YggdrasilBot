"""Monthly partitions for ``messages``. Idempotent; used by the migration and a daily job."""

from datetime import UTC, date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

PARENT = "messages"


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def partition_name(month: date) -> str:
    return f"{PARENT}_{month:%Y_%m}"


def partition_ddl(month: date) -> str:
    start, end = _month_start(month), _add_months(month, 1)
    return (
        f"CREATE TABLE IF NOT EXISTS {partition_name(start)} PARTITION OF {PARENT} "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )


def months_to_ensure(today: date, back: int, ahead: int) -> list[date]:
    base = _month_start(today)
    return [_add_months(base, i) for i in range(-back, ahead + 1)]


async def ensure_partitions(conn: AsyncConnection, *, back: int = 0, ahead: int = 3) -> list[str]:
    """Create missing monthly partitions; returns the names that now exist for the window."""
    names = []
    for month in months_to_ensure(datetime.now(UTC).date(), back, ahead):
        await conn.execute(text(partition_ddl(month)))
        names.append(partition_name(month))
    return names


_known: set[str] = set()


async def ensure_month(conn: AsyncConnection, when: datetime) -> None:
    """Make sure the partition for ``when`` exists (backfill can reach months before the baseline)."""
    month = _month_start(when.astimezone(UTC).date())
    name = partition_name(month)
    if name in _known:
        return
    await conn.execute(text(partition_ddl(month)))
    _known.add(name)


async def months_of_runway(conn: AsyncConnection) -> int:
    """Number of whole future months already partitioned (for alerting)."""
    rows = await conn.execute(
        text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = :parent"
        ),
        {"parent": PARENT},
    )
    existing = {r[0] for r in rows}
    current = _month_start(datetime.now(UTC).date())
    n = 0
    while partition_name(_add_months(current, n + 1)) in existing:
        n += 1
    return n
