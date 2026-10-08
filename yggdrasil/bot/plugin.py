"""NoneBot plugin: ingest every whitelisted group message, route triggers, run handlers.

Routing happens once per event in an ``event_preprocessor`` against the DB-driven
trigger index; the resolved command is rewritten into an internal ``ygg:<handler>``
header so Alconna only parses arguments (see ``TriggerExt``).
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from arclet.alconna import AllParam
from nonebot import get_driver, on_notice, on_type
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupRecallNoticeEvent,
    HeartbeatMetaEvent,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.message import event_preprocessor
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from nonebot.typing import T_State
from nonebot_plugin_alconna import Alconna, Args, Arparma, Option, Subcommand, UniMessage, on_alconna
from nonebot_plugin_alconna.extension import Extension

from yggdrasil.bot import state as bot_state
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import TRIGGERS_MATCHED
from yggdrasil.ingest.writer import WRITER, IncomingMessage
from yggdrasil.pipeline import ratelimit
from yggdrasil.runtime import snapshot
from yggdrasil.triggers.index import MatchInput, Resolved, current

__plugin_meta__ = PluginMetadata(
    name="yggdrasil",
    description="Group-chat TL;DR, replies and background memory",
    usage="see dashboard",
    type="application",
    supported_adapters={"~onebot.v11"},
)

STATE_KEY = "ygg.cmd"
_background: set[asyncio.Task] = set()


def _segments(message: Any) -> list[dict[str, Any]]:
    return [{"type": seg.type, "data": dict(seg.data)} for seg in message]


def _spawn(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# --- routing -----------------------------------------------------------------


@event_preprocessor
async def _route(bot: Bot, event: Event, state: T_State) -> None:
    if isinstance(event, HeartbeatMetaEvent):
        bot_state.heartbeat(bot.self_id)
        return
    if not isinstance(event, MessageEvent):
        return
    snap = snapshot()
    if isinstance(event, GroupMessageEvent):
        if not snap.group_enabled(event.group_id):
            return
        sender = event.sender
        WRITER.submit(
            IncomingMessage(
                group_id=event.group_id,
                user_id=event.user_id,
                qq_message_id=event.message_id,
                message_seq=getattr(event, "message_seq", None) or getattr(event, "real_seq", None),
                sent_at=datetime.fromtimestamp(event.time, UTC),
                sender_name=(sender.card or sender.nickname or "").strip(),
                segments=_segments(event.original_message),
                is_self=str(event.user_id) == bot.self_id,
            )
        )
        if str(event.user_id) == bot.self_id:
            return
        bot_state.last_group_message[event.group_id] = event.message_id
        scope, group_id = "group", event.group_id
        role = event.sender.role or "member"
    elif isinstance(event, PrivateMessageEvent):
        scope, group_id, role = "private", None, "member"
    else:
        return
    if event.user_id in snap.superusers:
        role = "superuser"
    replied_to_bot = event.reply is not None and str(event.reply.sender.user_id) == bot.self_id
    resolved = current().match(
        MatchInput(
            scope=scope,
            group_id=group_id,
            text=event.get_plaintext(),
            to_me=event.to_me,
            replied_to_bot=replied_to_bot,
            role=role,
        )
    )
    if resolved is None:
        return
    if not await ratelimit.cooldown(resolved.rule_id, event.user_id, resolved.cooldown_s):
        return
    TRIGGERS_MATCHED.add(1, {"handler": resolved.handler, "scope": scope})
    state[STATE_KEY] = resolved


class TriggerExt(Extension):
    """Feed Alconna the routed command (``ygg:<handler> <args>``) instead of the raw message."""

    @property
    def priority(self) -> int:
        return 10

    @property
    def id(self) -> str:
        return "yggdrasil.trigger"

    async def message_provider(self, event, state, bot, use_origin: bool = False) -> UniMessage | None:
        resolved: Resolved | None = state.get(STATE_KEY)
        if resolved is None:
            return None
        return UniMessage(f"ygg:{resolved.handler} {resolved.args}".strip())


def _routed(handler: str) -> Rule:
    async def check(state: T_State) -> bool:
        resolved = state.get(STATE_KEY)
        return resolved is not None and resolved.handler == handler

    return Rule(check)


def _matcher(alc: Alconna, handler: str):
    return on_alconna(
        alc,
        rule=_routed(handler),
        extensions=[TriggerExt],
        skip_for_unmatch=True,
        auto_send_output=False,
        use_cmd_start=False,
        priority=10,
        block=True,
    )


def _rest(result: Arparma) -> str:
    rest = result.all_matched_args.get("rest")
    if rest is None:
        return ""
    if isinstance(rest, list | tuple):
        return " ".join(str(x) for x in rest)
    return str(rest)


# --- handlers ----------------------------------------------------------------

tldr_cmd = _matcher(Alconna("ygg:tldr", Args["rest?", AllParam]), "tldr")
chat_cmd = _matcher(Alconna("ygg:chat", Args["rest?", AllParam]), "chat")
optout_cmd = _matcher(Alconna("ygg:privacy.optout"), "privacy.optout")
optin_cmd = _matcher(Alconna("ygg:privacy.optin"), "privacy.optin")
forget_cmd = _matcher(Alconna("ygg:privacy.forget"), "privacy.forget")
review_cmd = _matcher(
    Alconna(
        "ygg:admin.review",
        Subcommand("approve", Args["id", str]),
        Subcommand("reject", Args["id", str]),
        Subcommand("edit", Args["id", str], Args["text", AllParam]),
    ),
    "admin.review",
)
status_cmd = _matcher(Alconna("ygg:admin.status", Option("-v")), "admin.status")


@tldr_cmd.handle()
async def _tldr(bot: Bot, event: MessageEvent, state: T_State, result: Arparma) -> None:
    from yggdrasil.pipeline.invoke import run

    _spawn(run(bot, event, state[STATE_KEY], "tldr", _rest(result)))


@chat_cmd.handle()
async def _chat(bot: Bot, event: MessageEvent, state: T_State, result: Arparma) -> None:
    from yggdrasil.pipeline.invoke import run

    _spawn(run(bot, event, state[STATE_KEY], "chat", _rest(result)))


async def _privacy_reply(bot: Bot, event: GroupMessageEvent, text: str) -> None:
    from yggdrasil.delivery.send import send_text
    from yggdrasil.napcat.api import NapCatAPI

    ident = await bot_state.identity(bot)
    await send_text(
        NapCatAPI(bot),
        group_id=event.group_id,
        user_id=event.user_id,
        reply_to=event.message_id
        if bot_state.should_quote(
            event.group_id, event.message_id, snapshot().settings(event.group_id).interaction.quote_reply
        )
        else None,
        text=text,
        self_id=ident.self_id,
        nickname=ident.nickname,
    )


@optout_cmd.handle()
async def _optout(bot: Bot, event: GroupMessageEvent) -> None:
    from yggdrasil.memory.privacy import set_opt_out

    await set_opt_out(event.group_id, event.user_id, True)
    await _privacy_reply(bot, event, "好，之后不再整理你的画像（聊天记录仍按群规则保存）。想恢复就说「记得我」。")


@optin_cmd.handle()
async def _optin(bot: Bot, event: GroupMessageEvent) -> None:
    from yggdrasil.memory.privacy import set_opt_out

    await set_opt_out(event.group_id, event.user_id, False)
    await _privacy_reply(bot, event, "好，重新开始记住你。")


@forget_cmd.handle()
async def _forget(bot: Bot, event: GroupMessageEvent) -> None:
    from yggdrasil.memory.privacy import forget_user, set_opt_out

    await forget_user(event.group_id, event.user_id)
    await set_opt_out(event.group_id, event.user_id, True)
    await _privacy_reply(bot, event, "已删除你的画像，并且之后不再整理。")


@review_cmd.handle()
async def _review(event: PrivateMessageEvent, result: Arparma) -> None:
    from yggdrasil.moderation.review import ReviewError, decide, parse_review_id

    action = next((s for s in ("approve", "reject", "edit") if result.find(s)), None)
    if action is None:
        await review_cmd.finish("用法：通过 R12 / 拒绝 R12 / 改 R12 新内容")
    raw_id = result.query[str](f"{action}.id") or ""
    text = result.query[list](f"{action}.text")
    if isinstance(text, list | tuple):
        text = " ".join(str(x) for x in text)
    try:
        review_id = parse_review_id(raw_id)
        item = await decide(review_id, action, f"qq:{event.user_id}", str(text) if text else None)
    except ReviewError as e:
        await review_cmd.finish(str(e))
    await review_cmd.finish(f"R{item.id} → {item.status}")


@status_cmd.handle()
async def _status(bot: Bot) -> None:
    from yggdrasil.moderation.review import pending_count

    snap = snapshot()
    enabled = [g for g in snap.groups.values() if g.enabled]
    await status_cmd.finish(
        f"config v{snap.version}\n群：{', '.join(f'{g.name}({g.group_id})' for g in enabled) or '无'}\n"
        f"路由：{', '.join(sorted(snap.routes)) or '无'}\n待审核：{pending_count}"
    )


# --- notices & lifecycle -----------------------------------------------------

recall = on_notice(priority=1, block=False)


@recall.handle()
async def _recall(event: GroupRecallNoticeEvent) -> None:
    if snapshot().group_enabled(event.group_id):
        _spawn(WRITER.mark_recalled(event.group_id, event.message_id))


heartbeat = on_type(HeartbeatMetaEvent, priority=1, block=False)

driver = get_driver()


@driver.on_bot_connect
async def _connected(bot: Bot) -> None:
    bot_state.mark_connected(bot)
    ident = await bot_state.identity(bot)
    LOGGER.info("bot connected: {} ({})", ident.nickname, ident.self_id)


@driver.on_bot_disconnect
async def _disconnected(bot: Bot) -> None:
    bot_state.mark_disconnected(bot)
    LOGGER.warning("bot disconnected: {}", bot.self_id)
