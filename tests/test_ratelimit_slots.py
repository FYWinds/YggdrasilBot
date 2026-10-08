import asyncio

from yggdrasil.pipeline.ratelimit import slot
from yggdrasil.settings import RateLimitSettings


async def test_same_user_requests_queue_instead_of_dropping():
    cfg = RateLimitSettings(max_concurrent_per_user=1, max_queued_per_user=2, queue_timeout_s=5)
    order: list[str] = []
    release_first = asyncio.Event()

    async def run(name: str, hold: asyncio.Event | None = None) -> bool:
        async with slot(1, 42, cfg) as granted:
            if granted:
                order.append(f"start {name}")
                if hold:
                    await hold.wait()
                order.append(f"end {name}")
            return granted

    first = asyncio.create_task(run("a", release_first))
    await asyncio.sleep(0)
    second = asyncio.create_task(run("b"))
    await asyncio.sleep(0.05)
    assert order == ["start a"]  # b is waiting, not dropped
    release_first.set()
    assert await first
    assert await second
    assert order == ["start a", "end a", "start b", "end b"]


async def test_queue_limit_and_timeout_reject():
    cfg = RateLimitSettings(max_concurrent_per_user=1, max_queued_per_user=1, queue_timeout_s=0.1)
    hold = asyncio.Event()

    async def holder() -> None:
        async with slot(2, 7, cfg):
            await hold.wait()

    task = asyncio.create_task(holder())
    await asyncio.sleep(0)

    async def try_slot() -> bool:
        async with slot(2, 7, cfg) as granted:
            return granted

    waiter = asyncio.create_task(try_slot())  # queues (1 allowed), then times out
    await asyncio.sleep(0)
    assert await try_slot() is False  # queue already full
    assert await waiter is False  # timed out after 0.1s
    hold.set()
    await task
    assert await try_slot() is True  # slots were released
