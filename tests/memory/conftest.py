from collections.abc import Iterator
from typing import Any

import pytest

from yggdrasil import runtime


@pytest.fixture
def group_settings() -> Iterator[Any]:
    """Install a snapshot with per-group settings documents: ``group_settings({gid: {...}})``."""
    previous = runtime._snapshot

    def install(docs: dict[int, dict[str, Any]]) -> None:
        runtime._snapshot = runtime.Snapshot(
            groups={gid: runtime.GroupInfo(gid, "test", True, doc) for gid, doc in docs.items()}
        )

    yield install
    runtime._snapshot = previous
