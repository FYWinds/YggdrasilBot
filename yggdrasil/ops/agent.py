"""The ops agent: a superuser's natural-language admin instruction, carried out with ops tools.

Entry points: ``@bot ops <instruction>`` (``ops <instruction>`` in private; trigger ``admin.ops``), and a private
「同意」/「拒绝」 that is not a review command (it answers a request notice).
"""

import secrets
import time
from datetime import datetime
from typing import Literal

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
from yggdrasil.llm.errors import describe
from yggdrasil.llm.registry import TaskNotConfigured, chat_model, record_usage
from yggdrasil.llm.streaming import drain_events
from yggdrasil.ops.tools import OpsDeps, QuotedMessage, ops_tools
from yggdrasil.people import People, resolve
from yggdrasil.prompts import render
from yggdrasil.qq.client import QQ
from yggdrasil.qq.model import Incoming, Message
from yggdrasil.runtime import snapshot
from yggdrasil.settings import InteractionSettings
from yggdrasil.transcript import store as transcripts
from yggdrasil.transcript.capture import TranscriptRecorder
from yggdrasil.transcript.notes import TraceNotes
from yggdrasil.transcript.schema import QQContextDisplay, QQMessage

Outcome = Literal["done", "reply", "failed"]


async def _quoted(incoming: Incoming, self_id: int, bot_name: str) -> QuotedMessage | None:
    r = incoming.reply
    if r is None:
        return None
    try:
        people = await resolve(incoming.group_id, normalize(r.segments).mentions, bot_name=bot_name, self_id=self_id)
    except Exception:
        LOGGER.exception("name lookup failed")
        people = People(bot_name, self_ids=frozenset({self_id}))
    return QuotedMessage(r.message_id, r.sender.user_id == self_id, normalize(r.segments, people.mention).text)


def _display(incoming: Incoming) -> QQContextDisplay:
    """The trigger and quoted messages, as the console shows the user turn."""

    def snapshot_of(m: Message) -> QQMessage:
        return QQMessage(
            qq_message_id=m.message_id,
            user_id=m.sender.user_id,
            sender_name=m.sender.name.strip(),
            sent_at=datetime.fromtimestamp(m.time).astimezone(),
            segments=m.segments,
        )

    quoted = snapshot_of(incoming.reply) if incoming.reply is not None else None
    blocks = ["被引用的消息", "指令"] if quoted else ["指令"]
    return QQContextDisplay(trigger=snapshot_of(incoming), quoted=quoted, blocks=blocks)


def _prompt(instruction: str, quoted: QuotedMessage | None, nonce: str) -> str:
    def block(tag: str, body: str) -> str:
        return f"<{tag}-{nonce}>\n{body.replace(nonce, '*' * len(nonce))}\n</{tag}-{nonce}>"

    parts = []
    if quoted is not None:
        parts.append(block("被引用的消息", ("（你自己发的）" if quoted.is_self else "") + quoted.text))
    parts.append(block("指令", instruction.strip()))
    return "\n\n".join(parts)


async def run_ops(qq: QQ, incoming: Incoming, instruction: str) -> Outcome:
    """Carry out one superuser instruction; anyone else is ignored."""
    snap = snapshot()
    group_id = incoming.group_id
    if incoming.user_id not in snap.superusers:
        return "failed"
    settings = snap.settings(group_id)
    try:
        resolved = chat_model("ops", default_limits=UsageLimits(request_limit=10, tool_calls_limit=12))
    except TaskNotConfigured:
        await _send(qq, incoming, "ops 任务还没有配置模型，先在控制台的模型路由里配置。")
        return "failed"

    started = time.perf_counter()
    ident = await identity(qq)
    deps = OpsDeps(qq, incoming.user_id, group_id, await _quoted(incoming, ident.self_id, settings.persona.name))
    deps.notes = TraceNotes(full=settings.privacy.capture_llm_content, bot=(ident.self_id, settings.persona.name))
    if deps.notes.full:
        deps.notes.context = _display(incoming)
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
            await react(qq, group_id, incoming.message_id, settings.interaction.received_emoji)
            agent: Agent[OpsDeps, OpsReply] = Agent(
                resolved.model,
                output_type=OpsReply,
                deps_type=OpsDeps,
                instructions=render(
                    "ops_system",
                    now=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %A"),
                    where=f"群「{group.name if group else ''}」（{group_id}）" if group_id is not None else "私聊",
                    in_group=group_id is not None,
                    protocol=qq.protocol,
                    nonce=nonce,
                ),
                tools=ops_tools(deps),
                name="ops",
                capabilities=[recorder],
                retries=2,
            )
            result = await agent.run(
                _prompt(instruction, deps.quoted, nonce),
                deps=deps,
                model_settings=resolved.settings,
                usage_limits=resolved.limits,
                usage=usage,
                event_stream_handler=drain_events,
            )
            reply = result.output
            outcome = await _deliver(qq, incoming, reply, deps, settings.interaction)
        except Exception as e:
            error = describe(e)[:1000]
            LOGGER.exception("ops run failed")
            span.record_exception(e)
            if group_id is not None:
                await react(qq, group_id, incoming.message_id, settings.interaction.failure_emoji)
            else:
                await _send(qq, incoming, f"出错了：{error[:300]}")
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
            user_id=incoming.user_id,
            handler="ops",
            trigger_qq_message_id=incoming.message_id,
            request_text=instruction[:2000],
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


async def _deliver(qq: QQ, incoming: Incoming, reply: OpsReply, deps: OpsDeps, cfg: InteractionSettings) -> Outcome:
    in_group = incoming.group_id is not None
    text = (reply.text or "").strip()
    if not in_group and not text:
        # No reactions in private chats: say what was done instead.
        text = "；".join(deps.actions) if deps.actions else "没有需要做的操作。"
    if text and (reply.mode == "reply" or not in_group):
        await _send(qq, incoming, text)
        return "reply"
    # done (or an empty reply): the reaction is the whole answer
    await react(qq, incoming.group_id, incoming.message_id, cfg.action_emoji if deps.actions else cfg.silent_emoji)
    return "done"


async def _send(qq: QQ, incoming: Incoming, text: str) -> None:
    ident = await identity(qq)
    group_id = incoming.group_id
    await send_text(
        qq,
        group_id=group_id,
        user_id=incoming.user_id,
        reply_to=incoming.message_id if group_id is not None else None,
        text=text.strip()[:1500],
        self_id=ident.self_id,
        nickname=ident.nickname,
    )
