from collections.abc import Callable
from typing import Any

import pytest

from yggdrasil.runtime import GroupInfo, Snapshot


@pytest.fixture
def group_settings(snapshot: Callable[..., Snapshot]) -> Callable[[dict[int, dict[str, Any]]], None]:
    """Install per-group settings documents: ``group_settings({gid: {...}})``."""

    def install(docs: dict[int, dict[str, Any]]) -> None:
        snapshot(groups={gid: GroupInfo(gid, "test", True, doc) for gid, doc in docs.items()})

    return install
