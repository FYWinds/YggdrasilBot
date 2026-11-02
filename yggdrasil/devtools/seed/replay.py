"""Plays a scripted run through a real ``Agent`` on a ``FunctionModel`` and writes its invocation and transcript."""

import asyncio
import json
import random
import secrets
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from pydantic import BaseModel
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, ModelResponsePart, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import update

from yggdrasil.agent.context import build_context
from yggdrasil.agent.schema import BotReply, OpsReply
from yggdrasil.db.models import Invocation, Message, ModerationResult, Transcript
from yggdrasil.db.session import get_session
from yggdrasil.devtools.seed.chat import Chat, bot_message, sender_label, text_segment
from yggdrasil.devtools.seed.data import FAST, GROUPS, MEMBERS, MODEL, NOW, PERSONA, PRIVATE_TEXT, SELF, TRACE_PREFIX
from yggdrasil.devtools.seed.scenarios import SAFE, MsgId, Run, Turn, verdict_record
from yggdrasil.devtools.seed.tools import CHAT_TOOLS, OPS_TOOLS, SeedDeps
from yggdrasil.memory import search
from yggdrasil.memory.search import MessageView
from yggdrasil.people import self_label
from yggdrasil.prompts import render
from yggdrasil.sandbox.job_model import Job
from yggdrasil.settings import Settings
from yggdrasil.transcript import store
from yggdrasil.transcript.capture import TranscriptRecorder
from yggdrasil.transcript.notes import TraceNotes
from yggdrasil.transcript.schema import QQContextDisplay, QQMessage
from yggdrasil.triggers.index import Resolved


def _resolve_args(args: dict[str, Any], keys: dict[str, Message]) -> dict[str, Any]:
    def one(v: Any) -> Any:
        if isinstance(v, MsgId):
            return f"#{keys[v.key].id}" if v.hash else keys[v.key].id
        if isinstance(v, list):
            return [one(x) for x in v]
        return v

    return {k: one(v) for k, v in args.items()}


def _model(run: Run, turns: list[Turn], model: str) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        idx = sum(isinstance(m, ModelResponse) for m in messages)
        turn = turns[idx]
        if turn.fail:
            raise ModelHTTPError(529, model, turn.fail)
        parts: list[ModelResponsePart] = []
        if turn.think:
            parts.append(ThinkingPart(turn.think))
        if turn.say:
            parts.append(TextPart(turn.say))
        parts += [
            ToolCallPart(name, args, tool_call_id=f"call_{run.key}_{idx}_{j}")
            for j, (name, args) in enumerate(turn.calls)
        ]
        if turn.output is not None and run.text_output:
            parts.append(TextPart(turn.output.model_dump_json(exclude_defaults=True)))
        elif turn.output is not None:
            out = turn.output.model_dump(mode="json", exclude_defaults=True)
            parts.append(ToolCallPart(info.output_tools[0].name, out, tool_call_id=f"call_{run.key}_{idx}_out"))
        written = len(turn.think) + len(turn.say) + sum(len(json.dumps(a, ensure_ascii=False)) for _, a in turn.calls)
        if turn.output is not None:
            written += len(turn.output.model_dump_json())
        usage = RequestUsage(input_tokens=2600 + 900 * idx, output_tokens=40 + written, cache_read_tokens=2000 * idx)
        return ModelResponse(parts=parts, usage=usage)

    return FunctionModel(respond, model_name=model)


def _event(trigger: Message, quoted: Message | None) -> GroupMessageEvent:
    def message(segments: list[dict[str, Any]]) -> OneBotMessage:
        return OneBotMessage([MessageSegment(s["type"], s["data"]) for s in segments if s["type"] != "reply"])

    data: dict[str, Any] = {
        "time": int(trigger.sent_at.timestamp()),
        "self_id": SELF,
        "post_type": "message",
        "sub_type": "normal",
        "user_id": trigger.user_id,
        "message_type": "group",
        "message_id": trigger.qq_message_id,
        "message": message(trigger.segments),
        "original_message": message(trigger.segments),
        "raw_message": "",
        "font": 0,
        "sender": {"user_id": trigger.user_id, "card": trigger.sender_name},
        "to_me": True,
        "group_id": trigger.group_id,
    }
    if quoted is not None:
        data["reply"] = {
            "time": int(quoted.sent_at.timestamp()),
            "message_type": "group",
            "message_id": quoted.qq_message_id,
            "real_id": quoted.qq_message_id,
            "sender": {"user_id": quoted.user_id, "nickname": quoted.sender_name},
            "message": message(quoted.segments),
        }
    return GroupMessageEvent.model_validate(data)


def _request_text(m: Message) -> str:
    return "".join(s["data"]["text"] for s in m.segments if s["type"] == "text").strip()


def _qq(m: Message) -> QQMessage:
    return store.qq_from_row(m)


@contextmanager
def _as_of(when: datetime) -> Generator[None]:
    """``build_context`` reads "recent" chat relative to now; the seed replays past triggers, so answer as of then."""
    real = search.recent_messages

    async def recent(group_id: int, *, limit: int = 40, minutes: int = 0) -> list[MessageView]:
        earlier = [m for m in await real(group_id, limit=300, minutes=24 * 60) if m.sent_at <= when]
        cutoff = when - timedelta(minutes=minutes)
        in_window = next((i for i, m in enumerate(earlier) if m.sent_at >= cutoff), len(earlier))
        return earlier[min(max(0, len(earlier) - limit), in_window) :]

    search.recent_messages = recent
    try:
        yield
    finally:
        search.recent_messages = real


async def _prompt(run: Run, chat: Chat | None, trigger: Message | None, notes: TraceNotes) -> tuple[str, str]:
    """``(instructions, prompt)`` for the run, built like the real handlers build them."""
    now = NOW.astimezone().strftime("%Y-%m-%d %H:%M %A")
    nonce = secrets.token_hex(3)
    if run.handler == "ops":
        where = f"群「{GROUPS[run.group_id]}」（{run.group_id}）" if run.group_id is not None else "私聊"
        if trigger is not None:
            text, who, asker = _request_text(trigger), sender_label(trigger), _qq(trigger)
        else:
            text, who = PRIVATE_TEXT, f"{MEMBERS[run.user_id]}({run.user_id})"
            asker = QQMessage(user_id=run.user_id, sender_name=MEMBERS[run.user_id], segments=[text_segment(text)])
        notes.context = QQContextDisplay(trigger=asker, blocks=["请求"])
        instructions = render("ops_system", now=now, where=where, in_group=run.group_id is not None, nonce=nonce)
        return instructions, f'<请求-{nonce} 发送者="{who}">\n{text}\n</请求-{nonce}>'
    settings = Settings()
    tool_names = [t.__name__ for t in CHAT_TOOLS]
    if run.handler == "report":
        prompt = run.prompt
    else:
        assert chat is not None
        assert trigger is not None
        quoted = chat.quoted(trigger) if run.quoted else None
        target = "quoted_or_self" if run.quoted else "self"
        resolved = Resolved(run.handler, 0, "", (), target, 0, False)
        request_text = "" if run.handler == "tldr" else _request_text(trigger)
        with _as_of(trigger.sent_at):
            ctx = await build_context(
                _event(trigger, quoted), resolved, settings, self_id=SELF, bot_name=PERSONA, request_text=request_text
            )
        notes.context = ctx.display
        prompt, nonce = ctx.prompt, ctx.nonce
    instructions = render(
        f"{'chat' if run.handler == 'report' else run.handler}_system",
        persona=settings.persona,
        group_name=GROUPS.get(run.group_id or 0, "本群"),
        now=now,
        tools=tool_names,
        sandbox=None,
        sandbox_links=False,
        actions=["recall_message"],
        recall_denied=False,
        report=run.handler == "report",
        style=render("style_rules"),
        data_rules=render("data_rules", nonce=nonce, me=self_label(PERSONA), sandbox=False),
    )
    return instructions, prompt


def _retime(entries: list[dict[str, Any]], start: datetime, rng: random.Random) -> datetime:
    """Spread the recorded entries over a plausible timeline (the scripted run itself took milliseconds)."""
    t = start
    for e in entries:
        if e["kind"] == "assistant":
            written = sum(int(p.get("chars", 0)) for p in e.get("parts", []))
            ms = rng.randint(900, 2400) + 18 * min(written, 600)
            t += timedelta(milliseconds=ms)
            e["duration_ms"] = ms
        elif e["kind"] == "tool":
            ms = rng.randint(150, 2600) if e.get("outcome") != "cancelled" else rng.randint(4000, 9000)
            t += timedelta(milliseconds=ms)
            e["duration_ms"] = ms
        elif e["kind"] == "event":
            t += timedelta(milliseconds=rng.randint(200, 900))
        e["at"] = t.isoformat()
    return t


async def invoke(run: Run, chats: dict[int, Chat], report_job: Job | None = None) -> UUID:
    chat = chats.get(run.group_id) if run.group_id is not None else None
    trigger = chat.keys[run.key] if chat is not None and run.key in chat.keys else None
    if trigger is not None:
        start = trigger.sent_at + timedelta(milliseconds=600)
    elif report_job is not None and report_job.ended_at is not None:
        start = datetime.fromtimestamp(report_job.ended_at, UTC) + timedelta(seconds=4)
    else:
        start = NOW - timedelta(minutes=45)
    user_id = trigger.user_id if trigger is not None else run.user_id
    rng = random.Random(run.key)
    notes = TraceNotes(full=run.full, bot=(SELF, PERSONA))
    recorder = TranscriptRecorder()
    output: BaseModel | None = None
    error = run.error
    model = FAST if run.handler == "ops" else MODEL
    ran = run.status not in ("blocked_input", "ratelimited", "running")
    if ran:
        instructions, prompt = await _prompt(run, chat, trigger, notes)
        keys = chat.keys if chat else {}
        turns = [replace(t, calls=[(n, _resolve_args(a, keys)) for n, a in t.calls]) for t in run.turns]
        output_type = OpsReply if run.handler == "ops" else BotReply
        agent: Agent[SeedDeps, Any] = Agent(
            _model(run, turns, model),
            output_type=PromptedOutput(output_type) if run.text_output else output_type,
            deps_type=SeedDeps,
            instructions=instructions,
            tools=OPS_TOOLS if run.handler == "ops" else CHAT_TOOLS,
            capabilities=[recorder],
            name=run.handler,
            retries=2,
        )
        target = chat.quoted(trigger) if chat is not None and trigger is not None and run.quoted else None
        deps = SeedDeps(notes, chat or Chat(0), before=start, target=target)
        task = asyncio.create_task(agent.run(prompt, deps=deps))
        if run.status == "cancelled":
            await asyncio.sleep(0.2)
            task.cancel()
        try:
            output = (await task).output
        except asyncio.CancelledError:
            pass
        except ModelHTTPError:
            assert run.status == "failed"

    responses = [m for m in recorder.history if isinstance(m, ModelResponse)]
    inv = Invocation(
        id=uuid4(),
        created_at=start,
        group_id=run.group_id,
        user_id=user_id,
        handler=run.handler,
        trigger_qq_message_id=trigger.qq_message_id if trigger else None,
        request_text=_request_text(trigger) if trigger else report_job.title if report_job else PRIVATE_TEXT,
        status=run.status,
        models=[model] if ran else [],
        tokens_in=sum(r.usage.input_tokens for r in responses),
        tokens_out=sum(r.usage.output_tokens for r in responses),
        tokens_cached=sum(r.usage.cache_read_tokens for r in responses),
        tool_calls=dict(Counter(t.name for t in recorder.tools.values())),
        ack_kind="react" if trigger is not None and run.status != "ratelimited" else None,
        ack_latency_ms=rng.randint(300, 900) if trigger is not None and run.status != "ratelimited" else None,
        input_verdict=verdict_record(
            "input", ("Unsafe", 0.97, "drop", ["违法"]) if run.status == "blocked_input" else SAFE
        )
        if run.handler in ("chat", "tldr") and run.status not in ("ratelimited", "running")
        else None,
        output_verdict=verdict_record("output", run.output_verdict) if run.output_verdict else None,
        reply=output.model_dump(mode="json") if output is not None else None,
        trace_id=TRACE_PREFIX + secrets.token_hex(14),  # the prefix marks seeded rows for reset
        error=error,
    )
    async with get_session() as s:
        s.add(inv)
    end = start + timedelta(milliseconds=rng.randint(400, 1200))
    if ran:
        await store.save_invocation(inv.id, recorder, notes, run.group_id)
        row = await store.for_invocation(inv.id)
        assert row is not None
        entries = list(row.entries)
        end = _retime(entries, start + timedelta(milliseconds=200), rng)
        async with get_session() as s:
            await s.execute(
                update(Transcript)
                .where(Transcript.id == row.id)
                .values(entries=entries, created_at=start, updated_at=end)
            )
    duration = int((end - start).total_seconds() * 1000) + (900 if ran else 0)
    reply_qq: int | None = None
    if chat is not None and isinstance(output, BotReply) and run.status == "ok":
        sent = await bot_message(
            chat, start + timedelta(milliseconds=duration), output.title or "", [s.content for s in output.sections]
        )
        reply_qq = sent.qq_message_id
    async with get_session() as s:
        await s.execute(
            update(Invocation)
            .where(Invocation.id == inv.id)
            .values(duration_ms=None if run.status == "running" else duration, reply_qq_message_id=reply_qq)
        )
        if inv.input_verdict:
            s.add(_moderation(inv.id, inv.input_verdict, start + timedelta(milliseconds=350)))
        if inv.output_verdict:
            s.add(_moderation(inv.id, inv.output_verdict, end + timedelta(milliseconds=300)))
    return inv.id


def _moderation(invocation_id: UUID, v: dict[str, Any], at: datetime) -> ModerationResult:
    return ModerationResult(
        created_at=at,
        invocation_id=invocation_id,
        stage=v["stage"],
        verdict=v["verdict"],
        score=v["score"],
        categories=v["categories"],
        action=v["action"],
        model=v["model"],
        latency_ms=v["latency_ms"],
    )
