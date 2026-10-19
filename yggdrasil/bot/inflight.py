"""Work started for incoming OneBot events (invocations, ops runs, event handling).

A rolling update waits for it (``drain``) before the old pod drops the OneBot connection, so replies
in progress still go out over that connection.
"""

import asyncio
import time
from collections.abc import Coroutine
from typing import Any

_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> None:
    track(coro)


def track[T](coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    """Run ``coro`` as a task that ``drain`` waits for; returns the task."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def drain(max_wait: float) -> int:
    """Wait until nothing is in flight, or ``max_wait`` seconds pass; return how many tasks are left.

    Work spawned while waiting is waited for too: the old pod keeps the connection until it moves.
    """
    deadline = time.monotonic() + max_wait
    while _tasks and (left := deadline - time.monotonic()) > 0:
        await asyncio.wait(set(_tasks), timeout=left)
    return len(_tasks)
