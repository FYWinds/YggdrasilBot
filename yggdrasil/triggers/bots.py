"""Other bots in a group never start a run.

Their messages are stored like anyone's, but bots answering bots can loop, and the bots in these groups imitate
members (replying "tldr" to a message) without anyone having asked for anything. A sender counts as a bot when

- it is listed in ``interaction.ignore_users``;
- its group card or nickname carries the ``[bot]`` tag (``name[bot]``, as on GitHub) the bot accounts here use.
  Looser hints ("bot" anywhere in a name, "测试中") turn up in people's names too;
- QQ classifies it as an official bot (``is_robot`` in its member info: Tencent's robot UIN ranges). Message events
  don't say so, so it is looked up once per sender, and only for a message that matched a trigger.

The caller exempts superusers.
"""

import math
import time

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from yggdrasil.core.log import LOGGER
from yggdrasil.napcat.api import NapCatAPI
from yggdrasil.settings import Settings

_TAG = "[bot]"
_LOOKUP_TIMEOUT_S = 3.0
_RETRY_AFTER_S = 600.0
# user id → (official QQ bot, monotonic time to ask again). An account never changes kind, so answers are kept;
# a failed lookup counts as "a person" for a while instead of delaying each of the sender's requests.
_official: dict[int, tuple[bool, float]] = {}


async def sent_by_bot(bot: Bot, event: GroupMessageEvent, settings: Settings) -> bool:
    """Whether the sender of ``event`` is another bot; ``bot`` is our own connection (for the member lookup)."""
    if event.user_id in settings.interaction.ignore_users:
        return True
    sender = event.sender
    if any(_TAG in (name or "").lower() for name in (sender.card, sender.nickname)):
        return True
    return await _official_bot(bot, event.group_id, event.user_id)


async def _official_bot(bot: Bot, group_id: int, user_id: int) -> bool:
    known = _official.get(user_id)
    if known is not None and time.monotonic() < known[1]:
        return known[0]
    try:
        info = await NapCatAPI(bot).get_group_member_info(
            group_id=group_id, user_id=user_id, reply_timeout=_LOOKUP_TIMEOUT_S
        )
    except Exception as e:
        LOGGER.warning("member info of {} in group {} unavailable, treated as a person: {!r}", user_id, group_id, e)
        _official[user_id] = (False, time.monotonic() + _RETRY_AFTER_S)
        return False
    _official[user_id] = (info.is_robot, math.inf)
    return info.is_robot
