from datetime import UTC, datetime, timedelta
from itertools import count

from yggdrasil.memory.search import MessageView

T0 = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)  # 12:00 Asia/Shanghai
_ids = count(1)


def mv(
    minute: float, text: str = "消息", *, user: int = 10001, name: str = "甲", mid: int | None = None
) -> MessageView:
    i = mid if mid is not None else next(_ids)
    return MessageView(i, i, T0 + timedelta(minutes=minute), user, name, text, None, False)
