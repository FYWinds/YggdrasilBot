"""Process-wide bot state: identity cache, connection and heartbeat tracking."""

import time
from dataclasses import dataclass

import nonebot
from nonebot.adapters.onebot.v11 import Bot

from yggdrasil.napcat.api import NapCatAPI


@dataclass(slots=True)
class Identity:
    self_id: int
    nickname: str


_identities: dict[str, Identity] = {}
connected: dict[str, float] = {}
last_heartbeat: dict[str, float] = {}
# Latest message id from someone other than the bot, per group (for quote-when-busy).
last_group_message: dict[int, int] = {}


def should_quote(group_id: int | None, trigger_message_id: int, mode: str) -> bool:
    if group_id is None or mode == "never":
        return False
    if mode == "always":
        return True
    return last_group_message.get(group_id, trigger_message_id) != trigger_message_id


async def identity(bot: Bot) -> Identity:
    ident = _identities.get(bot.self_id)
    if ident is None:
        try:
            info = await NapCatAPI(bot).get_login_info()
            ident = Identity(info.user_id, info.nickname)
        except Exception:
            ident = Identity(int(bot.self_id), "Bot")
        _identities[bot.self_id] = ident
    return ident


def mark_connected(bot: Bot) -> None:
    connected[bot.self_id] = time.time()
    last_heartbeat[bot.self_id] = time.time()


def mark_disconnected(bot: Bot) -> None:
    connected.pop(bot.self_id, None)
    _identities.pop(bot.self_id, None)


def heartbeat(self_id: str) -> None:
    last_heartbeat[self_id] = time.time()


def any_bot() -> Bot | None:
    for bot in nonebot.get_bots().values():
        if isinstance(bot, Bot):
            return bot
    return None
