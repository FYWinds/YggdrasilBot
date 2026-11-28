"""One triggered run (chat / tldr): moderation, quick ack, agent, delivery, bookkeeping."""

import asyncio
import contextlib
import time
from dataclasses import replace
from datetime import timedelta
from typing import Literal
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic_ai import Agent, ModelRetry, RunContext, Tool
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, UserContent
from pydantic_ai.usage import RunUsage, UsageLimits

from yggdrasil.agent import bridge
from yggdrasil.agent.budget import ToolBudget
from yggdrasil.agent.context import build_context, build_followup_context
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.media import keep_context_images
from yggdrasil.agent.permissions import Role, may_recall, speaker_role
from yggdrasil.agent.schema import BotReply
from yggdrasil.agent.skills import skill_names, skills_for
from yggdrasil.agent.tools import ALL_TOOLS, action_tools, suite_enabled, tool_names, tools_for
from yggdrasil.agent.tools import sandbox as sandbox_tools
from yggdrasil.agent.tools.images import check_reply_images, reply_images
from yggdrasil.bot.state import identity, should_quote
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import INVOCATION_ACK_LATENCY, INVOCATION_DURATION, INVOCATIONS
from yggdrasil.core.telemetry import TRACER, current_trace_id
from yggdrasil.delivery.forward import send_forward, strip_markdown
from yggdrasil.delivery.send import MessageDropped, react, send_plain, send_text
from yggdrasil.llm.errors import describe, refusal
from yggdrasil.llm.registry import ResolvedModel, TaskNotConfigured, chat_model, record_usage
from yggdrasil.llm.streaming import RUN_STATS, RunStats, drain_events
from yggdrasil.moderation.guard import Verdict, moderate
from yggdrasil.pipeline import ack as ack_mod
from yggdrasil.pipeline import ratelimit, recalls, record, repeats
from yggdrasil.pipeline import session as sessions
from yggdrasil.pipeline.prompt import inline_images, instructions, now_text
from yggdrasil.qq.client import QQ
from yggdrasil.qq.model import Incoming
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


async def run(qq: QQ, incoming: Incoming, resolved: Resolved, handler: Handler, request_text: str) -> None:
    snap = snapshot()
    group_id = incoming.group_id
    settings = snap.settings(group_id)
    role = speaker_role(incoming, snap.superusers)
    superuser = role == "superuser"
    user_id, message_id = incoming.user_id, incoming.message_id
    repeat = None if superuser else repeats.key(incoming, resolved, handler, request_text)
    if repeat is not None and not await repeats.first(repeat, settings.ratelimit.repeat_window_s):
        LOGGER.info("repeated {} request ignored user={} group={}", handler, user_id, group_id)
        INVOCATIONS.add(1, {"handler": handler, "outcome": "repeat"})
        await react(qq, group_id, message_id, settings.interaction.silent_emoji)
        return
    answered = False
    try:
        if reason := await ratelimit.check(group_id, user_id, settings.ratelimit, superuser=superuser):
            LOGGER.info("rate limited ({}) user={} group={}", reason, user_id, group_id)
            INVOCATIONS.add(1, {"handler": handler, "outcome": "ratelimited"})
            await react(qq, group_id, message_id, settings.interaction.ratelimited_emoji)
            return
        with recalls.watch(group_id, message_id) as watch:
            async with ratelimit.slot(group_id, user_id, settings.ratelimit) as granted:
                if not granted:
                    LOGGER.info("request dropped: queue full or timed out user={} group={}", user_id, group_id)
                    INVOCATIONS.add(1, {"handler": handler, "outcome": "busy"})
                    await react(qq, group_id, message_id, settings.interaction.ratelimited_emoji)
                    return
                if watch.recalled:
                    LOGGER.info("{} request recalled while queued user={} group={}", handler, user_id, group_id)
                    INVOCATIONS.add(1, {"handler": handler, "outcome": "cancelled"})
                    return
                inv = Invocator(qq, incoming, resolved, handler, request_text, settings, group_id, role, watch=watch)
                status = await inv.run()
                answered = status not in ("failed", "cancelled")
    finally:
        if repeat is not None and not answered:
            # Nothing came of it (rate limited, queue full, failed, recalled): asking again must work.
            with contextlib.suppress(Exception):
                await repeats.forget(repeat)


def _cancelling() -> bool:
    """Whether the running task itself is being cancelled."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class Invocator:
    def __init__(
        self,
        qq: QQ,
        incoming: Incoming,
        resolved: Resolved,
        handler: Handler,
        request_text: str,
        settings: Settings,
        group_id: int | None,
        role: Role = "member",
        *,
        watch: recalls.Watch | None = None,
    ) -> None:
        self._setup(
            qq,
            handler,
            settings,
            group_id,
            user_id=incoming.user_id,
            trigger_message_id=incoming.message_id,
            role=role,
        )
        self.incoming, self.resolved, self.request_text = incoming, resolved, request_text
        self.rule_id = resolved.rule_id
        if watch is not None:
            self.watch = watch

    def _setup(
        self,
        qq: QQ,
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
        self.qq, self.handler, self.settings, self.group_id = qq, handler, settings, group_id
        self.user_id, self.trigger_message_id = user_id, trigger_message_id
        self.record_handler: str = record_handler or handler
        self.rule_id: int | None = None
        self.role: Role = role
        self.reply_qq_message_id: int | None = None
        self.started = time.perf_counter()
        self.meta = record.Meta()
        self.invocation_id: UUID | None = None
        self.ack_kind: str | None = None
        self._finished = False
        self.deps: Deps | None = None  # the agent run's, once it ran: delivery sends the pictures it holds
        self.sandbox: Session | None = None  # set when this run gets the workspace suite
        # How a chat/tldr run's agent was built and its prompt's block nonce: set, the delivered answer's run is kept
        # for replies to it (``pipeline.session``).
        self._keep: tuple[sessions.Setup, str] | None = None
        # The run's conversation for the console; every agent this invocator builds records into it.
        self.recorder = TranscriptRecorder()
        self.notes = TraceNotes(full=settings.privacy.capture_llm_content)
        # The console follows a running invocation: every recorded step asks for the transcript to be written.
        self._live = transcripts.LiveWriter(self._write_live)
        self.recorder.on_change = self._live.poke
        # The agent run under way (route label, usage so far, deps): what ``_tally`` needs for the row's numbers.
        self._spending: tuple[str, RunUsage, Deps] | None = None
        # Recalling the trigger message stops the run before it answers; a run without one is never recalled.
        self.watch = recalls.Watch()
        self._slow: asyncio.Task[None] | None = None  # adds slow_emoji unless the run has its answer by then

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
                self._slow = asyncio.create_task(self._mark_slow())
                status = await self._run()
            except Exception as e:
                error = describe(e)[:1000]
                LOGGER.exception("invocation {} failed", self.record_handler)
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, error[:200]))
                if not await self._notify_refusal(e):
                    await self._status(self.settings.interaction.failure_emoji)
            finally:
                self._stop_slow()
                await self._live.stop()
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
                    with contextlib.suppress(Exception):
                        await keep_context_images(self.qq, self.group_id, self.user_id, self.notes)
                    await transcripts.save_invocation(self.invocation_id, self.recorder, self.notes, self.group_id)
        return status

    async def _write_live(self) -> None:
        """The run so far, for the console: its transcript, and on its row what the agent run has spent."""
        if self.invocation_id is None:
            return
        await transcripts.save_invocation(self.invocation_id, self.recorder, self.notes, self.group_id, live=True)
        if self._spending is not None:
            meta = replace(self.meta, models=list(self.meta.models))
            self._tally(meta, *self._spending)
            await record.progress_row(self.invocation_id, meta)

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
        ident = await identity(self.qq)
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
        if session is not None and replied_bot_msg is not None and self.group_id is not None:
            # The answer's agent again (route, instructions, tools), so everything before the new turn is what the
            # provider already has cached.
            route, sandbox_on = session.setup.handler, session.setup.sandbox
            ctx = await build_followup_context(
                self.incoming,
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
                self.incoming,
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
            await self._status(self.settings.interaction.failure_emoji)
            return "failed"

        mod_text = f"{ctx.request_text or ctx.trigger.text}\n{ctx.target.text if ctx.target is not ctx.trigger else ''}"
        deps = Deps(
            self.qq,
            self.group_id,
            self.user_id,
            self.settings,
            ctx.handles,
            route,
            recall_target=recall_target,
            self_id=ident.self_id,
            bot_nickname=ident.nickname,
            trigger_message_id=self.trigger_message_id,
            invocation_id=str(self.invocation_id) if self.invocation_id is not None else None,
            request_text=ctx.request_text or ctx.trigger.text,
            notes=self.notes,
        )
        if self.notes.full:
            self.notes.context = ctx.display
        tools_cfg = self.settings.tools
        tool_list = session.setup.tools if session else tool_names(tools_cfg.enabled, self.handler)
        skill_list = session.setup.skills if session else skill_names(self.settings, self.handler, self.group_id)
        suite = sandbox_tools.Suite()
        if sandbox_on and self.group_id is not None:
            self.sandbox = deps.sandbox = manager().session(
                self.group_id,
                invocation_id=str(self.invocation_id) if self.invocation_id is not None else None,
                cfg=tools_cfg,
            )
            suite = sandbox_tools.suite(exclude=frozenset() if tools_cfg.sandbox_delegate else frozenset({"delegate"}))
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
        self._keep = (sessions.Setup(route, system, tool_list, sandbox_on, skill_list), ctx.nonce)
        agent = self._agent(
            resolved_model,
            tools=tools_for(tool_list) + action_tools() + suite.tools,
            capabilities=suite.capabilities + skills_for(skill_list),
            instructions=system,
        )
        prompt = await inline_images(
            ctx, self.qq, self.group_id, self.user_id, route, self.settings.context.inline_images, self.notes
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
        agent = Agent(
            resolved_model.model,
            output_type=BotReply,
            deps_type=Deps,
            instructions=instructions,
            tools=tools,
            # The recorder sees every call, the budget's skipped ones too; the skipped ones reach no other wrapper.
            capabilities=[self.recorder, self._budget(), *capabilities],
            name=self.handler,
            retries=2,
        )
        agent.output_validator(check_reply_images)
        agent.output_validator(self._check_plain)
        return agent

    def _check_plain(self, ctx: RunContext[Deps], reply: BotReply) -> BotReply:
        """mode=plain is for short chat answers only (not tldr, not a job report)."""
        if reply.mode != "plain":
            return reply
        if self.record_handler != "chat":
            raise ModelRetry("这里不能用 mode=plain，用 forward")
        if problem := reply.plain_problem(len(ctx.deps.images)):
            raise ModelRetry(problem)
        return reply

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
        if self.watch.recalled:
            return await self._recalled()
        budget = self._budget()
        run_usage = RunUsage()  # filled in place, so a failed or cancelled run still reports what it spent
        run_stats = RunStats()
        stats_token = RUN_STATS.set(run_stats)  # the task copies the context, sub-agents included
        self._spending = (resolved_model.label, run_usage, deps)
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
        self.watch.guard(agent_task)
        self.watch.guard(input_task)
        blocked: Verdict | None = None
        try:
            v_in = await input_task
            await self._save_verdict(v_in)
            if v_in.action != "pass" and self.settings.moderation.input_action_on_hit == "drop":
                blocked = v_in
            else:
                result = await agent_task
        except asyncio.CancelledError:
            # The recall cancelled what this run waits for; a cancel of the run itself (shutdown) goes on up.
            if not self.watch.recalled or _cancelling():
                raise
        finally:
            for t in (agent_task, input_task):
                if not t.done():
                    t.cancel()
            self._stop_slow()  # the answer is in: moderation and sending are quick
            self._spending = None
            self._account(resolved_model.label, run_usage, deps, run_stats)
            # No sh / py runs any more: the code it started can't call the bot from here on.
            await bridge.close_run(deps)
        if self.watch.recalled:
            return await self._recalled()

        if blocked is not None:
            await self._status(self.settings.interaction.review_emoji)
            if self.settings.moderation.notify_admins_on_input_block:
                from yggdrasil.moderation.review import notify_admins

                cats = ",".join(blocked.categories)
                await notify_admins(
                    self.qq, f"输入被拦截（{blocked.verdict} {blocked.score:.2f} {cats}）\n{mod_text[:500]}"
                )
            await self._finish("blocked_input", None, input_verdict=blocked.as_dict())
            return "blocked_input"

        reply = result.output
        self.deps = deps

        if reply.mode == "done" and deps.actions:
            await self._status(self.settings.interaction.action_emoji)
            await self._finish("done", None, reply=reply.model_dump())
            return "done"
        if reply.mode not in ("forward", "plain"):  # silent, or "done" without having done anything
            return await self._stay_silent(reply)

        v_out = await moderate("output", mod_text, reply.full_text(), group_id=self.group_id)
        await self._save_verdict(v_out)
        checked = [f"输入 {v_in.verdict}"] if v_in.verdict != "Skipped" else []
        if v_out.verdict != "Skipped":
            checked.append(f"输出 {v_out.verdict}({v_out.score:.2f})")
        self.meta.moderation = " · ".join(checked)
        verdicts = {"input_verdict": v_in.as_dict(), "output_verdict": v_out.as_dict(), "reply": reply.model_dump()}
        if self.watch.recalled:  # while output moderation ran; once the answer is going out, it goes out
            return await self._recalled(**verdicts)

        if v_out.action == "pass":
            await self._keep_session(result.all_messages(), deps.handles)
            try:
                await self.deliver(reply)
            except MessageDropped:
                # QQ swallowed it (content filter, most likely); the group got a short notice instead.
                await self._status(self.settings.interaction.review_emoji)
                await self._finish("dropped", None, **verdicts)
                return "dropped"
            await self._finish("ok", None, reply_qq_message_id=self.reply_qq_message_id, **verdicts)
            return "ok"
        if v_out.action == "review" and self.group_id is not None:
            from yggdrasil.moderation.review import create_review

            await create_review(
                self.qq,
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
        ident = await identity(self.qq)
        quote = allow_quote and should_quote(
            self.group_id, self.trigger_message_id, self.settings.interaction.quote_reply
        )
        await send_text(
            self.qq,
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
        """Add a status reaction to the trigger message (QQ has none in private chats). Reactions are never
        removed: frequent add/remove pairs look automated to QQ's risk control."""
        if not emoji or self.group_id is None:
            return False
        await react(self.qq, self.group_id, self.trigger_message_id, emoji)
        return True

    async def _notify_refusal(self, exc: Exception) -> bool:
        """The provider's content filter cut the answer off: say so in the chat with the provider's reason, where a
        bare failure reaction reads as the bot breaking. False when there is nothing to say or it didn't go out."""
        told = refusal(exc)
        if told is None or not self.settings.interaction.refusal_notice:
            return False
        text = "模型拒绝了这个请求" + (f"（{told.category}）" if told.category else "")
        if told.explanation:
            text += f"：{told.explanation[:500]}"
        try:
            await self._send_text(text)
        except Exception:
            LOGGER.exception("refusal notice failed")
            return False
        return True

    async def _mark_slow(self) -> None:
        """``slow_emoji`` once the run has gone ``slow_after_s`` without its answer, so the requester knows it is
        still working (long sandbox runs, slow pages)."""
        await asyncio.sleep(self.settings.interaction.slow_after_s)
        with contextlib.suppress(Exception):
            await self._status(self.settings.interaction.slow_emoji)

    def _stop_slow(self) -> None:
        if self._slow is not None:
            self._slow.cancel()
            self._slow = None

    def _budget(self) -> ToolBudget:
        # Workspace sessions (write, run, read the error, fix) need far more steps than a plain reply.
        cfg = self.settings.tools
        if self.sandbox is not None:
            return ToolBudget(requests=cfg.sandbox_request_limit, tool_calls=cfg.sandbox_tool_calls_limit)
        return ToolBudget(requests=cfg.request_limit, tool_calls=cfg.tool_calls_limit)

    @staticmethod
    def _tally(meta: record.Meta, model_label: str, usage: RunUsage, deps: Deps) -> None:
        """Add what an agent run has spent so far to ``meta``."""
        meta.models.append(model_label)
        meta.tokens_in += usage.input_tokens
        meta.tokens_out += usage.output_tokens
        meta.tokens_cached += usage.cache_read_tokens
        for label, tin, tout in deps.vision_tokens:
            meta.models.append(label)
            meta.tokens_in += tin
            meta.tokens_out += tout
        meta.tools = dict(deps.tool_counts)
        meta.tool_seconds = dict(deps.tool_seconds)

    def _account(self, model_label: str, usage: RunUsage, deps: Deps, stats: RunStats) -> None:
        """Add what the agent run spent to the invocation (also for failed or cancelled runs)."""
        self._tally(self.meta, model_label, usage, deps)
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

    async def _recalled(self, **extra: object) -> str:
        """The trigger message was recalled before the answer went out: stop, send nothing (not even a reaction)."""
        LOGGER.info("{} trigger {} recalled; run stopped", self.handler, self.trigger_message_id)
        await self._finish("cancelled", "原消息已撤回", **extra)
        return "cancelled"

    def _replied_bot_message(self, self_id: int) -> int | None:
        """QQ id of the bot's own group message this chat request replies to (recall candidate)."""
        r = self.incoming.reply
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
        if (
            any(name not in ALL_TOOLS for name in setup.tools)
            or (setup.sandbox and not self._suite_enabled(setup.handler))
            or not set(setup.skills) <= set(skill_names(self.settings, self.handler, self.group_id))
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
        ident = await identity(self.qq)
        deps = self.deps
        images = await reply_images(deps, reply) if deps is not None else {}
        self.meta.total_s = self.elapsed()
        if reply.mode == "plain":
            text = strip_markdown(reply.full_text())
            pictures = [images[k][0] for s in reply.sections for k in s.images if k in images]
            pictures += [p for p, _ in (deps.images if deps is not None else ())]
            if not text and not pictures:  # an image-only answer whose pictures were all refused or lost
                LOGGER.warning("plain answer has nothing left to send")
                return
            quote = should_quote(self.group_id, self.trigger_message_id, cfg.quote_reply)
            self.reply_qq_message_id = await send_plain(
                self.qq,
                group_id=self.group_id,
                user_id=self.user_id,
                reply_to=self.trigger_message_id if quote else None,
                text=text,
                images=pictures,
                self_id=ident.self_id,
                nickname=ident.nickname,
            )
            return
        meta_text = self.meta.render(list(cfg.metadata_fields)) if cfg.metadata_node else None
        self.reply_qq_message_id = await send_forward(
            self.qq,
            group_id=self.group_id,
            user_id=self.user_id,
            reply=reply,
            meta_text=meta_text,
            cfg=cfg,
            self_id=ident.self_id,
            nickname=ident.nickname,
            images=images,
            attached=deps.images if deps is not None else (),
        )
