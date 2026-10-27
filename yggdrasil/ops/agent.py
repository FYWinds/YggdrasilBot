"""The ops agent: a superuser's natural-language admin instruction, carried out with ops tools.

Entry points: any private message from a superuser (trigger ``admin.ops``), and a superuser
addressing the bot in a group (``pipeline.invoke.run`` asks here first; ``handoff`` sends it on to
the normal chat run).
"""

import secrets
import time
from datetime import datetime
from typing import Literal

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from opentelemetry import trace
from pydantic_ai import Agent
from pydantic_ai.usage import RunUsage, UsageLimits

from yggdrasil.agent.schema import OpsReply
from yggdrasil.bot.state import identity
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import INVOCATION_DURATION, INVOCATIONS
from yggdrasil.core.telemetry import TRACER, current_trace_id
from yggdrasil.db.models import Invocation
from yggdrasil.db.session import get_session
from yggdrasil.delivery.send import react, send_text
from yggdrasil.ingest.normalize import normalize
from yggdrasil.llm.registry import TaskNotConfigured, chat_model, record_usage
from yggdrasil.llm.streaming import drain_events
from yggdrasil.napcat.api import NapCatAPI
from yggdrasil.ops.tools import OpsDeps, QuotedMessage, ops_tools
from yggdrasil.people import People, resolve
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot
from yggdrasil.settings import InteractionSettings
from yggdrasil.transcript import store as transcripts
from yggdrasil.transcript.capture import TranscriptRecorder
from yggdrasil.transcript.notes import TraceNotes
from yggdrasil.transcript.schema import QQContextDisplay, QQMessage

Outcome = Literal["done", "reply", "handoff", "failed"]


async def _quoted(event: MessageEvent, self_id: int, bot_name: str) -> QuotedMessage | None:
    r = event.reply
    if r is None:
        return None
    segs = [{"type": s.type, "data": dict(s.data)} for s in r.message]
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    try:
        people = await resolve(group_id, normalize(segs).mentions, bot_name=bot_name, self_id=self_id)
    except Exception:
        LOGGER.exception("name lookup failed")
        people = People(bot_name, self_ids=frozenset({self_id}))
    return QuotedMessage(r.message_id, r.sender.user_id == self_id, normalize(segs, people.mention).text)


def _display(event: MessageEvent) -> QQContextDisplay:
    """The trigger and quoted messages, as the console shows the user turn."""

    def segments(message: object) -> list[dict[str, object]]:
        return [{"type": s.type, "data": dict(s.data)} for s in message]  # type: ignore[attr-defined]

    sender = event.sender
    trigger = QQMessage(
        qq_message_id=event.message_id,
        user_id=event.user_id,
        sender_name=(sender.card or sender.nickname or "").strip(),
        sent_at=datetime.fromtimestamp(event.time).astimezone(),
        segments=segments(event.original_message),
    )
    quoted = None
    if (r := event.reply) is not None:
        quoted = QQMessage(
            qq_message_id=r.message_id,
            user_id=r.sender.user_id or 0,
            sender_name=(r.sender.card or r.sender.nickname or "").strip(),
            sent_at=datetime.fromtimestamp(r.time).astimezone(),
            segments=segments(r.message),
        )
    blocks = ["被引用的消息", "指令"] if quoted else ["指令"]
    return QQContextDisplay(trigger=trigger, quoted=quoted, blocks=blocks)


def _prompt(event: MessageEvent, quoted: QuotedMessage | None, nonce: str) -> str:
    def block(tag: str, body: str) -> str:
        return f"<{tag}-{nonce}>\n{body.replace(nonce, '*' * len(nonce))}\n</{tag}-{nonce}>"

    parts = []
    if quoted is not None:
        parts.append(block("被引用的消息", ("（你自己发的）" if quoted.is_self else "") + quoted.text))
    parts.append(block("指令", event.get_plaintext().strip()))
    return "\n\n".join(parts)


async def run_ops(bot: Bot, event: MessageEvent) -> Outcome:
    """Carry out one superuser instruction; anyone else is handed back untouched."""
    snap = snapshot()
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    if event.user_id not in snap.superusers:
        return "handoff"
    settings = snap.settings(group_id)
    api = NapCatAPI(bot)
    try:
        resolved = chat_model("ops", default_limits=UsageLimits(request_limit=10, tool_calls_limit=12))
    except TaskNotConfigured:
        if group_id is not None:
            return "handoff"
        await _send(api, bot, event, "ops 任务还没有配置模型，先在控制台的模型路由里配置。")
        return "failed"

    started = time.perf_counter()
    ident = await identity(bot)
    deps = OpsDeps(bot, api, event.user_id, group_id, await _quoted(event, ident.self_id, settings.persona.name))
    deps.notes = TraceNotes(full=settings.privacy.capture_llm_content, bot=(ident.self_id, settings.persona.name))
    if deps.notes.full:
        deps.notes.context = _display(event)
    recorder = TranscriptRecorder()
    nonce = secrets.token_hex(3)
    group = snap.groups.get(group_id) if group_id is not None else None
    outcome: Outcome = "failed"
    reply: OpsReply | None = None
    error: str | None = None
    usage = RunUsage()  # filled in place, so a failed run still reports what it spent
    with TRACER.start_as_current_span("ygg.invocation ops", kind=trace.SpanKind.SERVER) as span:
        trace_id = current_trace_id()
        try:
            if group_id is not None:  # QQ has no reactions in private chats
                await react(api, event.message_id, settings.interaction.received_emoji)
            agent: Agent[OpsDeps, OpsReply] = Agent(
                resolved.model,
                output_type=OpsReply,
                deps_type=OpsDeps,
                instructions=render(
                    "ops_system",
                    now=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %A"),
                    where=f"群「{group.name if group else ''}」（{group_id}）" if group_id is not None else "私聊",
                    in_group=group_id is not None,
                    nonce=nonce,
                ),
                tools=ops_tools(deps),
                name="ops",
                capabilities=[recorder],
                retries=2,
            )
            result = await agent.run(
                _prompt(event, deps.quoted, nonce),
                deps=deps,
                model_settings=resolved.settings,
                usage_limits=resolved.limits,
                usage=usage,
                event_stream_handler=drain_events,
            )
            reply = result.output
            outcome = await _deliver(api, bot, event, reply, deps, settings.interaction)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:1000]
            LOGGER.exception("ops run failed")
            span.record_exception(e)
            if group_id is not None:
                await react(api, event.message_id, settings.interaction.failure_emoji)
            else:
                await _send(api, bot, event, f"出错了：{error[:300]}")
        span.set_attribute("ygg.status", outcome)
    record_usage(
        "ops",
        resolved.label,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached=usage.cache_read_tokens,
    )
    elapsed = time.perf_counter() - started
    INVOCATIONS.add(1, {"handler": "ops", "outcome": outcome})
    INVOCATION_DURATION.record(elapsed, {"handler": "ops"})
    async with get_session() as s:
        row = Invocation(
            group_id=group_id,
            user_id=event.user_id,
            handler="ops",
            trigger_qq_message_id=event.message_id,
            request_text=event.get_plaintext()[:2000],
            status=outcome,
            models=[resolved.label],
            tool_calls=dict(deps.tool_counts),
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
            tokens_cached=usage.cache_read_tokens,
            duration_ms=int(elapsed * 1000),
            reply=reply.model_dump() if reply else None,
            trace_id=trace_id,
            error=error,
        )
        s.add(row)
        await s.flush()
        invocation_id = row.id
    if recorder.history or recorder.tools:
        await transcripts.save_invocation(invocation_id, recorder, deps.notes, group_id)
    return outcome


async def _deliver(
    api: NapCatAPI, bot: Bot, event: MessageEvent, reply: OpsReply, deps: OpsDeps, cfg: InteractionSettings
) -> Outcome:
    in_group = isinstance(event, GroupMessageEvent)
    if reply.mode == "handoff" and in_group and not deps.actions:
        return "handoff"
    text = (reply.text or "").strip()
    if not in_group and not text:
        # No reactions in private chats: say what was done instead.
        text = "；".join(deps.actions) if deps.actions else "没有需要做的操作。"
    if text and (reply.mode == "reply" or not in_group):
        await _send(api, bot, event, text)
        return "reply"
    # done (or a handoff/empty reply after acting): the reaction is the whole answer
    await react(api, event.message_id, cfg.action_emoji if deps.actions else cfg.silent_emoji)
    return "done"


async def _send(api: NapCatAPI, bot: Bot, event: MessageEvent, text: str) -> None:
    ident = await identity(bot)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    await send_text(
        api,
        group_id=group_id,
        user_id=event.user_id,
        reply_to=event.message_id if group_id is not None else None,
        text=text.strip()[:1500],
        self_id=ident.self_id,
        nickname=ident.nickname,
    )
