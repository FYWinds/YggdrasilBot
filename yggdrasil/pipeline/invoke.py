"""One triggered run (chat / tldr): moderation, quick ack, agent, delivery, bookkeeping."""

import asyncio
import contextlib
import time
from datetime import timedelta
from typing import Literal
from uuid import UUID

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic_ai import Agent, Tool
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, UserContent
from pydantic_ai.usage import RunUsage, UsageLimits

from yggdrasil.agent.budget import ToolBudget
from yggdrasil.agent.context import build_context, build_followup_context
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.permissions import Role, may_recall, speaker_role
from yggdrasil.agent.schema import BotReply
from yggdrasil.agent.tools import ALL_TOOLS, action_tools, suite_enabled, tool_names, tools_for
from yggdrasil.agent.tools import sandbox as sandbox_tools
from yggdrasil.bot.state import identity, should_quote
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import INVOCATION_ACK_LATENCY, INVOCATION_DURATION, INVOCATIONS
from yggdrasil.core.telemetry import TRACER, current_trace_id
from yggdrasil.delivery.forward import send_forward
from yggdrasil.delivery.send import MessageDropped, react, send_text
from yggdrasil.llm.registry import ResolvedModel, TaskNotConfigured, chat_model, record_usage
from yggdrasil.llm.streaming import RUN_STATS, RunStats, drain_events
from yggdrasil.moderation.guard import Verdict, moderate
from yggdrasil.napcat.api import NapCatAPI
from yggdrasil.pipeline import ack as ack_mod
from yggdrasil.pipeline import ratelimit, record, repeats
from yggdrasil.pipeline import session as sessions
from yggdrasil.pipeline.prompt import inline_images, instructions, now_text
from yggdrasil.runtime import snapshot
from yggdrasil.sandbox import Session, manager
from yggdrasil.settings import Settings
from yggdrasil.transcript import store as transcripts
from yggdrasil.transcript.capture import TranscriptRecorder
from yggdrasil.transcript.notes import TraceNotes
from yggdrasil.triggers.index import Resolved

Handler = Literal["chat", "tldr"]
# How long before the answer's conversation was kept its newest seen chat message may have been sent.
_SEEN_SLACK = timedelta(hours=1)


async def run(bot: Bot, event: MessageEvent, resolved: Resolved, handler: Handler, request_text: str) -> None:
    snap = snapshot()
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    settings = snap.settings(group_id)
    role = speaker_role(event, snap.superusers)
    superuser = role == "superuser"
    api = NapCatAPI(bot)
    repeat = None if superuser else repeats.key(event, resolved, handler, request_text)
    if repeat is not None and not await repeats.first(repeat, settings.ratelimit.repeat_window_s):
        LOGGER.info("repeated {} request ignored user={} group={}", handler, event.user_id, group_id)
        INVOCATIONS.add(1, {"handler": handler, "outcome": "repeat"})
        if settings.interaction.silent_emoji:
            await react(api, event.message_id, settings.interaction.silent_emoji)
        return
    answered = False
    try:
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
            if handler == "chat" and superuser and group_id is not None:
                # A superuser talking to the bot may be giving an admin instruction: the ops agent
                # (fast model) acts on it, or hands it back here when it's an ordinary question.
                from yggdrasil.ops.agent import run_ops

                if await run_ops(bot, event) != "handoff":
                    return
            status = await Invocator(bot, api, event, resolved, handler, request_text, settings, group_id, role).run()
            answered = status != "failed"
    finally:
        if repeat is not None and not answered:
            # Nothing came of it (rate limited, queue full, failed): asking again must work.
            with contextlib.suppress(Exception):
                await repeats.forget(repeat)


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
        role: Role = "member",
    ) -> None:
        self._setup(
            bot, api, handler, settings, group_id, user_id=event.user_id, trigger_message_id=event.message_id, role=role
        )
        self.event, self.resolved, self.request_text = event, resolved, request_text
        self.rule_id = resolved.rule_id

    def _setup(
        self,
        bot: Bot,
        api: NapCatAPI,
        handler: Handler,
        settings: Settings,
        group_id: int | None,
        *,
        user_id: int,
        trigger_message_id: int,
        role: Role,
        record_handler: str | None = None,
    ) -> None:
        """State shared by every kind of run; ``record_handler`` names it in records and metrics when it differs
        from the route ``handler`` (a job report runs on the chat route)."""
        self.bot, self.api, self.handler, self.settings, self.group_id = bot, api, handler, settings, group_id
        self.user_id, self.trigger_message_id = user_id, trigger_message_id
        self.record_handler: str = record_handler or handler
        self.rule_id: int | None = None
        self.role: Role = role
        self.reply_qq_message_id: int | None = None
        self.started = time.perf_counter()
        self.meta = record.Meta()
        self.invocation_id: UUID | None = None
        self.ack_kind: str | None = None
        self._slow_marked = False
        self._finished = False
        self.images: list[tuple[bytes, str]] = []
        self.sandbox: Session | None = None  # set when this run gets the workspace suite
        # How a chat/tldr run's agent was built and its prompt's block nonce: set, the delivered answer's run is kept
        # for replies to it (``pipeline.session``).
        self._keep: tuple[sessions.Setup, str] | None = None
        # The run's conversation for the console; every agent this invocator builds records into it.
        self.recorder = TranscriptRecorder()
        self.notes = TraceNotes(full=settings.privacy.capture_llm_content)

    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    async def run(self) -> str:
        """Run, record and return the status (``failed`` when it raised)."""
        attrs = {
            "ygg.handler": self.record_handler,
            "ygg.group_id": self.group_id or 0,
            "ygg.rule_id": self.rule_id or 0,
        }
        with TRACER.start_as_current_span(
            f"ygg.invocation {self.record_handler}", kind=trace.SpanKind.SERVER, attributes=attrs
        ) as span:
            self.meta.trace_id = current_trace_id()
            status, error = "failed", None
            try:
                if await self._status(self.settings.interaction.received_emoji):
                    self._mark_ack("react")
                status = await self._run()
            except Exception as e:
                error = f"{type(e).__name__}: {e}"[:1000]
                LOGGER.exception("invocation {} failed", self.record_handler)
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, error[:200]))
                await self._status(self.settings.interaction.failure_emoji)
            finally:
                span.set_attribute("ygg.status", status)
                self.meta.total_s = self.elapsed()
                INVOCATIONS.add(1, {"handler": self.record_handler, "outcome": status})
                INVOCATION_DURATION.record(self.meta.total_s, {"handler": self.record_handler})
                if not self._finished:
                    await self._finish(status, error)
                if self.sandbox is not None:
                    # Kills leftover processes, renews the claim, releases the workspace; never raises.
                    with contextlib.suppress(Exception):
                        await self.sandbox.close()
                if self.invocation_id is not None and (self.recorder.history or self.recorder.tools):
                    await transcripts.save_invocation(self.invocation_id, self.recorder, self.notes, self.group_id)
        return status

    # --- bookkeeping -------------------------------------------------------
    async def _start_row(self, request_text: str) -> None:
        self.invocation_id = await record.start_row(
            group_id=self.group_id,
            user_id=self.user_id,
            handler=self.record_handler,
            trigger_message_id=self.trigger_message_id,
            request_text=request_text,
            trace_id=self.meta.trace_id,
        )

    async def _save_verdict(self, v: Verdict) -> None:
        if v.verdict == "Skipped" or self.invocation_id is None:
            return
        await record.save_verdict(self.invocation_id, v)

    async def _finish(self, status: str, error: str | None, **extra: object) -> None:
        self._finished = True
        self.meta.total_s = self.elapsed()
        await ratelimit.add_tokens(self.group_id, self._billable_tokens())
        if self.invocation_id is None:
            return
        await record.finish_row(self.invocation_id, status, error, self.meta, self.ack_kind, **extra)

    def _billable_tokens(self) -> int:
        """Tokens this run adds to the group's daily budget."""
        return self.meta.tokens_in + self.meta.tokens_out

    # --- main flow ---------------------------------------------------------
    async def _run(self) -> str:
        ident = await identity(self.bot)
        persona = self.settings.persona.name
        self.notes.bot = (ident.self_id, persona)
        replied_bot_msg = self._replied_bot_message(ident.self_id)
        recall_target = (
            replied_bot_msg
            if replied_bot_msg is not None
            and self.group_id is not None
            and await may_recall(self.role, self.user_id, self.group_id, replied_bot_msg)
            else None
        )
        # Replying to one of the bot's messages: the speaker may or may not have it recalled.
        recall = None if replied_bot_msg is None else recall_target is not None
        session = await self._session(replied_bot_msg)
        if session is not None and replied_bot_msg is not None and isinstance(self.event, GroupMessageEvent):
            # The answer's agent again (route, instructions, tools), so everything before the new turn is what the
            # provider already has cached.
            route, sandbox_on = session.setup.handler, session.setup.sandbox
            ctx = await build_followup_context(
                self.event,
                self.settings,
                self_id=ident.self_id,
                bot_name=persona,
                request_text=self.request_text,
                handles=session.handles,
                nonce=session.nonce,
                answer_qq_id=replied_bot_msg,
                seen_message_id=session.seen_message_id,
                # The id does the cut; the time only keeps the query on recent partitions.
                since=session.created_at - (_SEEN_SLACK if session.seen_message_id is not None else timedelta()),
                now=now_text(),
                recall=bool(recall),
                workspace=sandbox_on,
            )
            self.recorder.prior = session.messages
            self.recorder.continued_from = str(session.invocation_id)
        else:
            session = None
            route, sandbox_on = self.handler, self._suite_enabled(self.handler)
            ctx = await build_context(
                self.event,
                self.resolved,
                self.settings,
                self_id=ident.self_id,
                bot_name=persona,
                request_text=self.request_text,
                workspace=sandbox_on,
                recall=recall,
            )
        await self._start_row(self.request_text or ctx.trigger.text)
        try:
            resolved_model = chat_model(route)
        except TaskNotConfigured:
            LOGGER.warning("task {} has no model route; configure it in the dashboard", route)
            await react(self.api, self.trigger_message_id, self.settings.interaction.failure_emoji)
            return "failed"

        mod_text = f"{ctx.request_text or ctx.trigger.text}\n{ctx.target.text if ctx.target is not ctx.trigger else ''}"
        deps = Deps(
            self.api,
            self.group_id,
            self.user_id,
            self.settings,
            ctx.handles,
            route,
            recall_target=recall_target,
            self_id=ident.self_id,
            bot_nickname=ident.nickname,
            on_slow_tool=self._mark_slow,
            trigger_message_id=self.trigger_message_id,
            invocation_id=str(self.invocation_id) if self.invocation_id is not None else None,
            request_text=ctx.request_text or ctx.trigger.text,
            notes=self.notes,
        )
        if self.notes.full:
            self.notes.context = ctx.display
        tools_cfg = self.settings.tools
        tool_list = session.setup.tools if session else tool_names(tools_cfg.enabled, self.handler)
        suite = sandbox_tools.Suite()
        if sandbox_on and self.group_id is not None:
            self.sandbox = deps.sandbox = manager().session(
                self.group_id,
                invocation_id=str(self.invocation_id) if self.invocation_id is not None else None,
                cfg=tools_cfg,
            )
            suite = sandbox_tools.suite()
        system = (
            session.setup.instructions
            if session
            else instructions(
                self.handler,
                self.settings,
                self.group_id,
                tools=tool_list + suite.names,
                sandbox=tools_cfg if sandbox_on else None,
                report=False,
                nonce=ctx.nonce,
            )
        )
        self._keep = (sessions.Setup(route, system, tool_list, sandbox_on), ctx.nonce)
        agent = self._agent(
            resolved_model,
            tools=tools_for(tool_list) + action_tools() + suite.tools,
            capabilities=suite.capabilities,
            instructions=system,
        )
        prompt = await inline_images(
            ctx, self.api, self.group_id, route, self.settings.context.inline_images, self.notes
        )
        return await self._execute(
            agent, prompt, deps, resolved_model, mod_text, history=session.messages if session else None
        )

    def _suite_enabled(self, handler: str) -> bool:
        return suite_enabled(
            self.settings,
            handler,
            self.group_id,
            global_settings=snapshot().settings(),
            configured=manager().configured,
        )

    def _agent(
        self,
        resolved_model: ResolvedModel,
        *,
        tools: list[Tool[Deps]],
        capabilities: list[AbstractCapability[Deps]],
        instructions: str,
    ) -> Agent[Deps, BotReply]:
        return Agent(
            resolved_model.model,
            output_type=BotReply,
            deps_type=Deps,
            instructions=instructions,
            tools=tools,
            capabilities=[*capabilities, self._budget(), self.recorder],
            name=self.handler,
            retries=2,
        )

    async def _execute(
        self,
        agent: Agent[Deps, BotReply],
        prompt: str | list[UserContent],
        deps: Deps,
        resolved_model: ResolvedModel,
        mod_text: str,
        *,
        history: list[ModelMessage] | None = None,
    ) -> str:
        """Run the agent (after ``history``, an earlier answer's conversation) next to input moderation, then
        moderate, deliver and record the reply."""
        budget = self._budget()
        run_usage = RunUsage()  # filled in place, so a failed or cancelled run still reports what it spent
        run_stats = RunStats()
        stats_token = RUN_STATS.set(run_stats)  # the task copies the context, sub-agents included
        agent_task = asyncio.create_task(
            agent.run(
                prompt,
                message_history=history,
                deps=deps,
                model_settings=resolved_model.settings,
                usage_limits=UsageLimits(request_limit=budget.requests),
                usage=run_usage,
                event_stream_handler=drain_events,
                workspace=self.sandbox.backend if self.sandbox is not None else None,
            )
        )
        RUN_STATS.reset(stats_token)
        input_task = asyncio.create_task(moderate("input", mod_text, group_id=self.group_id))
        blocked: Verdict | None = None
        try:
            v_in = await input_task
            await self._save_verdict(v_in)
            if v_in.action != "pass" and self.settings.moderation.input_action_on_hit == "drop":
                blocked = v_in
            else:
                result = await agent_task
        finally:
            for t in (agent_task, input_task):
                if not t.done():
                    t.cancel()
            self._account(resolved_model.label, run_usage, deps, run_stats)

        if blocked is not None:
            await self._status(self.settings.interaction.review_emoji)
            if self.settings.moderation.notify_admins_on_input_block:
                from yggdrasil.moderation.review import notify_admins

                cats = ",".join(blocked.categories)
                await notify_admins(
                    self.bot, f"输入被拦截（{blocked.verdict} {blocked.score:.2f} {cats}）\n{mod_text[:500]}"
                )
            await self._finish("blocked_input", None, input_verdict=blocked.as_dict())
            return "blocked_input"

        reply = result.output
        self.images = deps.images

        if reply.mode == "done" and deps.actions:
            await self._status(self.settings.interaction.action_emoji)
            await self._finish("done", None, reply=reply.model_dump())
            return "done"
        if reply.mode != "forward":  # silent, or "done" without having done anything
            return await self._stay_silent(reply)

        v_out = await moderate("output", mod_text, reply.full_text(), group_id=self.group_id)
        await self._save_verdict(v_out)
        checked = [f"输入 {v_in.verdict}"] if v_in.verdict != "Skipped" else []
        if v_out.verdict != "Skipped":
            checked.append(f"输出 {v_out.verdict}({v_out.score:.2f})")
        self.meta.moderation = " · ".join(checked)
        verdicts = {"input_verdict": v_in.as_dict(), "output_verdict": v_out.as_dict(), "reply": reply.model_dump()}

        if v_out.action == "pass":
            await self._keep_session(result.all_messages(), deps.handles)
            try:
                await self.deliver(reply)
            except MessageDropped:
                # QQ swallowed it (content filter, most likely); the group got a short notice instead.
                await self._status(self.settings.interaction.review_emoji)
                await self._finish("dropped", None, **verdicts)
                return "dropped"
            await self._status(self.settings.interaction.done_emoji)
            await self._finish("ok", None, reply_qq_message_id=self.reply_qq_message_id, **verdicts)
            return "ok"
        if v_out.action == "review" and self.group_id is not None:
            from yggdrasil.moderation.review import create_review

            await create_review(
                self.bot,
                invocation_id=self.invocation_id,
                group_id=self.group_id,
                user_id=self.user_id,
                trigger_qq_message_id=self.trigger_message_id,
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
            self.group_id, self.trigger_message_id, self.settings.interaction.quote_reply
        )
        await send_text(
            self.api,
            group_id=self.group_id,
            user_id=self.user_id,
            reply_to=self.trigger_message_id if quote else None,
            text=text,
            self_id=ident.self_id,
            nickname=ident.nickname,
        )

    def _mark_ack(self, kind: str) -> None:
        """Record the first visible response's latency."""
        self.ack_kind = kind
        if self.meta.ack_s is None:
            self.meta.ack_s = self.elapsed()
            INVOCATION_ACK_LATENCY.record(self.meta.ack_s, {"handler": self.record_handler, "kind": kind})

    async def _status(self, emoji: str) -> bool:
        """Add a status reaction to the trigger message. Reactions are never removed: frequent
        add/remove pairs look automated to QQ's risk control."""
        if not emoji:
            return False
        await react(self.api, self.trigger_message_id, emoji)
        return True

    async def _mark_slow(self) -> None:
        if not self._slow_marked:
            self._slow_marked = True
            await self._status(self.settings.interaction.slow_tool_emoji)

    def _budget(self) -> ToolBudget:
        # Workspace sessions (write, run, read the error, fix) need far more steps than a plain reply.
        cfg = self.settings.tools
        if self.sandbox is not None:
            return ToolBudget(requests=cfg.sandbox_request_limit, tool_calls=cfg.sandbox_tool_calls_limit)
        return ToolBudget(requests=cfg.request_limit, tool_calls=cfg.tool_calls_limit)

    def _account(self, model_label: str, usage: RunUsage, deps: Deps, stats: RunStats) -> None:
        """Add what the agent run spent to the invocation (also for failed or cancelled runs)."""
        self.meta.models.append(model_label)
        self.meta.tokens_in += usage.input_tokens
        self.meta.tokens_out += usage.output_tokens
        self.meta.tokens_cached += usage.cache_read_tokens
        for label, tin, tout in deps.vision_tokens:
            self.meta.models.append(label)
            self.meta.tokens_in += tin
            self.meta.tokens_out += tout
        self.meta.tools = dict(deps.tool_counts)
        self.meta.tool_seconds = dict(deps.tool_seconds)
        self.meta.slow_tools = frozenset(self.settings.tools.slow_tools)
        self.meta.model_names = stats.models
        self.meta.ttft_s = stats.ttft
        self.meta.tokens_per_s = stats.tokens_per_s
        record_usage(
            self.handler,
            model_label,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached=usage.cache_read_tokens,
        )

    async def _stay_silent(self, reply: BotReply) -> str:
        """The model chose not to answer: send nothing, only mark the trigger with silent_emoji."""
        reason = (reply.silent_reason or "").strip()[:200]
        LOGGER.info("{} stays silent: {}", self.handler, reason or "(no reason)")
        trace.get_current_span().set_attribute("ygg.silent_reason", reason)
        await self._status(self.settings.interaction.silent_emoji)
        await self._finish("silent", None, reply=reply.model_dump())
        return "silent"

    def _replied_bot_message(self, self_id: int) -> int | None:
        """QQ id of the bot's own group message this chat request replies to (recall candidate)."""
        r = self.event.reply
        if self.handler != "chat" or self.group_id is None or r is None or r.sender.user_id != self_id:
            return None
        return r.message_id

    async def _session(self, answer: int | None) -> sessions.Session | None:
        """The kept run of the bot answer ``answer`` this chat request replies to, when it is recent enough and its
        agent can still be rebuilt as it was."""
        minutes = self.settings.context.continue_minutes
        if answer is None or self.group_id is None or minutes <= 0 or not self.settings.privacy.capture_llm_content:
            return None
        try:
            found = await sessions.load(self.group_id, answer, timedelta(minutes=minutes))
        except Exception:
            LOGGER.exception("session lookup failed")
            return None
        if found is None:
            return None
        setup = found.setup
        if any(name not in ALL_TOOLS for name in setup.tools) or (
            setup.sandbox and not self._suite_enabled(setup.handler)
        ):
            LOGGER.info("invocation {} used tools this run can't have; starting over", found.invocation_id)
            return None
        LOGGER.info("continuing the conversation of invocation {}", found.invocation_id)
        return found

    async def _keep_session(self, messages: list[ModelMessage], handles: Handles) -> None:
        """Keep this answer's run for replies to it (``pipeline.session``)."""
        if (
            self._keep is None
            or self.invocation_id is None
            or self.group_id is None
            or not self.settings.privacy.capture_llm_content
        ):
            return
        from yggdrasil.memory.search import find_by_qq_id

        try:
            seen = await find_by_qq_id(self.group_id, self.trigger_message_id)
        except Exception:
            LOGGER.exception("trigger lookup failed")
            seen = None
        setup, nonce = self._keep
        await sessions.save(self.invocation_id, setup, messages, handles, nonce, seen.id if seen else None)

    async def deliver(self, reply: BotReply) -> None:
        cfg = self.settings.interaction
        ident = await identity(self.bot)
        self.meta.total_s = self.elapsed()
        meta_text = self.meta.render(list(cfg.metadata_fields)) if cfg.metadata_node else None
        self.reply_qq_message_id = await send_forward(
            self.api,
            group_id=self.group_id,
            user_id=self.user_id,
            reply=reply,
            meta_text=meta_text,
            cfg=cfg,
            self_id=ident.self_id,
            nickname=ident.nickname,
            images=self.images,
        )
