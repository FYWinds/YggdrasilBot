"""One triggered run (chat / tldr): moderation, quick ack, agent, delivery, bookkeeping."""

import asyncio
import contextlib
import time
from datetime import datetime
from typing import Literal
from uuid import UUID

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits
from sqlalchemy import update

from yggdrasil.agent.context import BuiltContext, build_context
from yggdrasil.agent.deps import Deps
from yggdrasil.agent.schema import AckDecision, BotReply, Section
from yggdrasil.agent.tools import tools_for
from yggdrasil.bot.state import identity, should_quote
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import INVOCATION_ACK_LATENCY, INVOCATION_DURATION, INVOCATIONS
from yggdrasil.core.telemetry import TRACER, current_trace_id
from yggdrasil.db.models import Invocation, ModerationResult
from yggdrasil.db.session import get_session
from yggdrasil.delivery.send import Meta, react, send_forward, send_sticker, send_text, strip_markdown
from yggdrasil.llm.registry import TaskNotConfigured, chat_model, record_usage
from yggdrasil.moderation.guard import Verdict, moderate
from yggdrasil.napcat.api import NapCatAPI
from yggdrasil.pipeline import ack as ack_mod
from yggdrasil.pipeline import ratelimit
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot
from yggdrasil.settings import Settings
from yggdrasil.triggers.index import Resolved

Handler = Literal["chat", "tldr"]


async def run(bot: Bot, event: MessageEvent, resolved: Resolved, handler: Handler, request_text: str) -> None:
    snap = snapshot()
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    settings = snap.settings(group_id)
    superuser = event.user_id in snap.superusers
    api = NapCatAPI(bot)
    if reason := await ratelimit.check(group_id, event.user_id, settings.ratelimit, superuser=superuser):
        LOGGER.info("rate limited ({}) user={} group={}", reason, event.user_id, group_id)
        INVOCATIONS.add(1, {"handler": handler, "outcome": "ratelimited"})
        if settings.interaction.ratelimited_emoji:
            await react(api, event.message_id, settings.interaction.ratelimited_emoji)
        return
    async with ratelimit.slot(group_id, event.user_id, settings.ratelimit) as granted:
        if not granted:
            LOGGER.info("request dropped: queue full or timed out user={} group={}", event.user_id, group_id)
            INVOCATIONS.add(1, {"handler": handler, "outcome": "busy"})
            if settings.interaction.ratelimited_emoji:
                await react(api, event.message_id, settings.interaction.ratelimited_emoji)
            return
        await Invocator(bot, api, event, resolved, handler, request_text, settings, group_id).run()


class Invocator:
    def __init__(
        self,
        bot: Bot,
        api: NapCatAPI,
        event: MessageEvent,
        resolved: Resolved,
        handler: Handler,
        request_text: str,
        settings: Settings,
        group_id: int | None,
    ) -> None:
        self.bot, self.api, self.event, self.resolved = bot, api, event, resolved
        self.handler, self.request_text, self.settings, self.group_id = handler, request_text, settings, group_id
        self.started = time.perf_counter()
        self.meta = Meta()
        self.invocation_id: UUID | None = None
        self.ack_kind: str | None = None
        self._slow_marked = False
        self._finished = False
        self.images: list[tuple[bytes, str]] = []

    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    async def run(self) -> None:
        attrs = {"ygg.handler": self.handler, "ygg.group_id": self.group_id or 0, "ygg.rule_id": self.resolved.rule_id}
        with TRACER.start_as_current_span(
            f"ygg.invocation {self.handler}", kind=trace.SpanKind.SERVER, attributes=attrs
        ) as span:
            self.meta.trace_id = current_trace_id()
            status, error = "failed", None
            try:
                if await self._status(self.settings.interaction.received_emoji):
                    self._mark_ack("react")
                status = await self._run()
            except Exception as e:
                error = f"{type(e).__name__}: {e}"[:1000]
                LOGGER.exception("invocation {} failed", self.handler)
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, error[:200]))
                await self._status(self.settings.interaction.failure_emoji)
            finally:
                span.set_attribute("ygg.status", status)
                self.meta.total_s = self.elapsed()
                INVOCATIONS.add(1, {"handler": self.handler, "outcome": status})
                INVOCATION_DURATION.record(self.meta.total_s, {"handler": self.handler})
                if not self._finished:
                    await self._finish(status, error)

    # --- bookkeeping -------------------------------------------------------
    async def _start_row(self, ctx: BuiltContext) -> None:
        async with get_session() as session:
            row = Invocation(
                group_id=self.group_id,
                user_id=self.event.user_id,
                handler=self.handler,
                trigger_qq_message_id=self.event.message_id,
                request_text=(self.request_text or ctx.trigger.text)[:2000],
                status="running",
                trace_id=self.meta.trace_id,
            )
            session.add(row)
            await session.flush()
            self.invocation_id = row.id

    async def _save_verdict(self, v: Verdict) -> None:
        if v.verdict == "Skipped" or self.invocation_id is None:
            return
        async with get_session() as session:
            session.add(
                ModerationResult(
                    invocation_id=self.invocation_id,
                    stage=v.stage,
                    verdict=v.verdict,
                    score=v.score,
                    categories=v.categories,
                    action=v.action,
                    model=v.model,
                    latency_ms=v.latency_ms,
                )
            )

    async def _finish(self, status: str, error: str | None, **extra: object) -> None:
        self._finished = True
        self.meta.total_s = self.elapsed()
        await ratelimit.add_tokens(self.group_id, self.meta.tokens_in + self.meta.tokens_out)
        if self.invocation_id is None:
            return
        with contextlib.suppress(Exception):
            async with get_session() as session:
                await session.execute(
                    update(Invocation)
                    .where(Invocation.id == self.invocation_id)
                    .values(
                        status=status,
                        error=error,
                        models=list(dict.fromkeys(self.meta.models)),
                        tokens_in=self.meta.tokens_in,
                        tokens_out=self.meta.tokens_out,
                        tokens_cached=self.meta.tokens_cached,
                        tool_calls=self.meta.tools,
                        ack_kind=self.ack_kind,
                        ack_latency_ms=int(self.meta.ack_s * 1000) if self.meta.ack_s is not None else None,
                        duration_ms=int(self.meta.total_s * 1000),
                        **extra,
                    )
                )

    # --- main flow ---------------------------------------------------------
    async def _run(self) -> str:
        ident = await identity(self.bot)
        persona = self.settings.persona.name
        ctx = await build_context(
            self.event,
            self.resolved,
            self.settings,
            self_id=ident.self_id,
            bot_name=persona,
            request_text=self.request_text,
        )
        await self._start_row(ctx)
        try:
            resolved_model = chat_model(self.handler)
        except TaskNotConfigured:
            LOGGER.warning("task {} has no model route; configure it in the dashboard", self.handler)
            await react(self.api, self.event.message_id, self.settings.interaction.failure_emoji)
            return "failed"

        mod_text = f"{ctx.request_text or ctx.trigger.text}\n{ctx.target.text if ctx.target is not ctx.trigger else ''}"
        deps = Deps(
            self.api, self.group_id, self.event.user_id, self.settings, ctx.handles, on_slow_tool=self._mark_slow
        )
        tools_cfg = self.settings.tools
        group_name = ""
        if self.group_id is not None:
            info = snapshot().groups.get(self.group_id)
            group_name = info.name if info else ""
        agent: Agent[Deps, BotReply] = Agent(
            resolved_model.model,
            output_type=BotReply,
            deps_type=Deps,
            instructions=render(
                f"{self.handler}_system",
                persona=self.settings.persona,
                group_name=group_name or "本群",
                now=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %A"),
                tools=tools_cfg.enabled,
                style=render("style_rules"),
                max_messages=self.settings.interaction.messages_max_count,
                max_chars=self.settings.interaction.message_max_chars,
            ),
            tools=tools_for(tools_cfg.enabled),
            name=self.handler,
            retries=2,
        )
        agent_task = asyncio.create_task(
            agent.run(
                ctx.prompt,
                deps=deps,
                model_settings=resolved_model.settings,
                usage_limits=UsageLimits(
                    request_limit=tools_cfg.request_limit, tool_calls_limit=tools_cfg.tool_calls_limit
                ),
            )
        )
        input_task = asyncio.create_task(moderate("input", mod_text, group_id=self.group_id))
        recent_tail = ctx.prompt[-1500:]
        ack_task: asyncio.Task[AckDecision] | None = None
        if self.handler != "tldr":
            ack_task = asyncio.create_task(
                ack_mod.draft_ack(ctx.request_text or ctx.trigger.text, recent_tail, self.settings)
            )
        try:
            v_in = await input_task
            await self._save_verdict(v_in)
            policy = self.settings.moderation
            if v_in.action != "pass" and policy.input_action_on_hit == "drop":
                agent_task.cancel()
                if ack_task is not None:
                    ack_task.cancel()
                await self._status(self.settings.interaction.review_emoji)
                if policy.notify_admins_on_input_block:
                    from yggdrasil.moderation.review import notify_admins

                    await notify_admins(
                        self.bot,
                        f"输入被拦截（{v_in.verdict} {v_in.score:.2f} {','.join(v_in.categories)}）\n{mod_text[:500]}",
                    )
                await self._finish("blocked_input", None, input_verdict=v_in.as_dict())
                return "blocked_input"

            if ack_task is not None:
                await self._quick_ack(ctx, agent_task, ack_task)
            result = await agent_task
        finally:
            for t in (agent_task, ack_task, input_task):
                if t is not None and not t.done():
                    t.cancel()

        reply = result.output
        usage = result.usage
        self.meta.models.append(resolved_model.label)
        self.meta.tokens_in += usage.input_tokens or 0
        self.meta.tokens_out += usage.output_tokens or 0
        self.meta.tokens_cached += usage.cache_read_tokens or 0
        for label, tin, tout in deps.vision_tokens:
            self.meta.models.append(label)
            self.meta.tokens_in += tin
            self.meta.tokens_out += tout
        self.meta.tools = dict(deps.tool_counts)
        self.images = deps.images
        record_usage(
            self.handler,
            resolved_model.label,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cached=usage.cache_read_tokens or 0,
        )

        if reply.mode == "silent":
            return await self._stay_silent(reply)

        v_out = await moderate("output", mod_text, reply.full_text(), group_id=self.group_id)
        await self._save_verdict(v_out)
        self.meta.moderation = f"输入 {v_in.verdict} · 输出 {v_out.verdict}({v_out.score:.2f})"
        verdicts = {"input_verdict": v_in.as_dict(), "output_verdict": v_out.as_dict(), "reply": reply.model_dump()}

        if v_out.action == "pass":
            await self.deliver(reply)
            await self._status(self.settings.interaction.done_emoji)
            await self._finish("ok", None, **verdicts)
            return "ok"
        if v_out.action == "review" and self.group_id is not None:
            from yggdrasil.moderation.review import create_review

            await create_review(
                self.bot,
                invocation_id=self.invocation_id,
                group_id=self.group_id,
                user_id=self.event.user_id,
                trigger_qq_message_id=self.event.message_id,
                request_text=mod_text,
                reply=reply,
                verdict=v_out,
                expire_minutes=self.settings.moderation.review_expire_minutes,
            )
            if self.settings.moderation.notify_group_on_review:
                with contextlib.suppress(Exception):
                    notice = await ack_mod.review_notice(mod_text, self.settings)
                    if notice:
                        await self._send_text(notice)
            await self._status(self.settings.interaction.review_emoji)
            await self._finish("review", None, **verdicts)
            return "review"
        await self._status(self.settings.interaction.review_emoji)
        await self._finish("rejected", None, **verdicts)
        return "rejected"

    async def _send_text(self, text: str, *, allow_quote: bool = True) -> None:
        ident = await identity(self.bot)
        quote = allow_quote and should_quote(
            self.group_id, self.event.message_id, self.settings.interaction.quote_reply
        )
        await send_text(
            self.api,
            group_id=self.group_id,
            user_id=self.event.user_id,
            reply_to=self.event.message_id if quote else None,
            text=text,
            self_id=ident.self_id,
            nickname=ident.nickname,
        )

    async def _send_messages(self, messages: list[str]) -> None:
        """Send short messages one by one like a person typing; only the first may quote."""
        interval = self.settings.interaction.message_interval_s
        for i, text in enumerate(messages):
            if i and interval:
                await asyncio.sleep(interval)
            await self._send_text(text, allow_quote=i == 0)

    async def _quick_ack(self, ctx: BuiltContext, agent_task: asyncio.Task, ack_task: asyncio.Task) -> None:
        cfg = self.settings.interaction
        remaining = cfg.ack_delay_s - self.elapsed()
        if remaining > 0:
            await asyncio.wait({agent_task}, timeout=remaining)
        if agent_task.done():
            ack_task.cancel()
            return
        try:
            decision: AckDecision = await asyncio.wait_for(ack_task, timeout=5)
        except Exception as e:
            LOGGER.debug("quick ack draft failed: {}", e)
            return
        ident = await identity(self.bot)
        if decision.kind == "sticker" and decision.sticker_id is not None and self.group_id is not None:
            if await send_sticker(
                self.api,
                group_id=self.group_id,
                sticker_id=decision.sticker_id,
                self_id=ident.self_id,
                nickname=ident.nickname,
            ):
                self._mark_ack("sticker")
            return
        if decision.kind == "text" and decision.text:
            v = await moderate("ack", ctx.request_text or ctx.trigger.text, decision.text, group_id=self.group_id)
            await self._save_verdict(v)
            if v.action == "pass":
                await self._send_text(decision.text.strip()[:40])
                self._mark_ack("text")

    def _mark_ack(self, kind: str) -> None:
        """Record the first visible response's latency; ack_kind keeps the latest form."""
        self.ack_kind = kind
        if self.meta.ack_s is None:
            self.meta.ack_s = self.elapsed()
            INVOCATION_ACK_LATENCY.record(self.meta.ack_s, {"handler": self.handler, "kind": kind})

    async def _status(self, emoji: str) -> bool:
        """Add a status reaction to the trigger message. Reactions are never removed: frequent
        add/remove pairs look automated to QQ's risk control."""
        if not emoji:
            return False
        await react(self.api, self.event.message_id, emoji)
        return True

    async def _mark_slow(self) -> None:
        if not self._slow_marked:
            self._slow_marked = True
            await self._status(self.settings.interaction.slow_tool_emoji)

    async def _stay_silent(self, reply: BotReply) -> str:
        """The model chose not to answer: send nothing, only mark the trigger with silent_emoji."""
        reason = (reply.silent_reason or "").strip()[:200]
        LOGGER.info("{} stays silent: {}", self.handler, reason or "(no reason)")
        trace.get_current_span().set_attribute("ygg.silent_reason", reason)
        await self._status(self.settings.interaction.silent_emoji)
        await self._finish("silent", None, reply=reply.model_dump())
        return "silent"

    async def deliver(self, reply: BotReply) -> None:
        cfg = self.settings.interaction
        ident = await identity(self.bot)
        messages = [m for m in (strip_markdown(m) for m in reply.messages) if m]
        fits = len(messages) <= cfg.messages_max_count and all(len(m) <= cfg.message_max_chars for m in messages)
        if reply.mode == "messages" and not reply.sections:
            # tldr always answers in a forward (it was already acknowledged with a reaction).
            if messages and fits and not self.images and self.handler != "tldr":
                await self._send_messages(messages)
                return
            # Too long for chat-style delivery: fall back to a forward with the same text.
            reply = reply.model_copy(update={"sections": [Section(content="\n".join(messages))], "messages": []})
            messages = []
        if self.handler == "tldr":
            messages = []  # no lead line before a tldr forward
        lead, rest = (messages[0], messages[1:]) if messages else (None, [])
        if rest:
            reply = reply.model_copy(update={"sections": [Section(content="\n".join(rest)), *reply.sections]})
        if lead:
            await self._send_text(lead)
        self.meta.total_s = self.elapsed()
        meta_text = self.meta.render(list(cfg.metadata_fields)) if cfg.metadata_node else None
        await send_forward(
            self.api,
            group_id=self.group_id,
            user_id=self.event.user_id,
            reply=reply,
            meta_text=meta_text,
            cfg=cfg,
            self_id=ident.self_id,
            nickname=ident.nickname,
            images=self.images,
        )
