"""Runs whose trigger message is recalled while they work: they stop and send nothing.

A run watches its trigger message (``watch``). The group's recall notice (``recall``) marks the watch and cancels the
tasks it guards (the agent and input moderation); the run checks ``Watch.recalled`` again before it answers. Once the
answer is on its way, a recall changes nothing.
"""

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any


class Watch:
    def __init__(self) -> None:
        self.recalled = False
        self._tasks: set[asyncio.Task[Any]] = set()

    def guard(self, task: asyncio.Task[Any]) -> None:
        """Cancel ``task`` when the trigger is recalled (at once, if it already was)."""
        if self.recalled:
            task.cancel()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def mark(self) -> None:
        self.recalled = True
        for task in list(self._tasks):
            task.cancel()


_watches: dict[tuple[int, int], list[Watch]] = {}


@contextmanager
def watch(group_id: int | None, message_id: int) -> Generator[Watch]:
    """Watch a group message while the block runs (a private message is never recalled through a group notice)."""
    w = Watch()
    if group_id is None:
        yield w
        return
    key = (group_id, message_id)
    _watches.setdefault(key, []).append(w)
    try:
        yield w
    finally:
        watches = _watches[key]
        watches.remove(w)
        if not watches:
            del _watches[key]


def recall(group_id: int, message_id: int) -> bool:
    """The message was recalled: stop the runs it triggered. Returns whether there were any."""
    watches = _watches.get((group_id, message_id), [])
    for w in watches:
        w.mark()
    return bool(watches)
