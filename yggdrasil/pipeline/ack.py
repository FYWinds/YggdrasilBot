"""Fast first responses for chat: a short line or a sticker (tldr just reacts with a fixed emoji)."""

import time

from pydantic_ai import Agent
from sqlalchemy import select

from yggdrasil.agent.schema import AckDecision
from yggdrasil.db.models import Sticker
from yggdrasil.db.session import get_session
from yggdrasil.llm.registry import chat_model, record_usage
from yggdrasil.prompts import render
from yggdrasil.settings import Settings

_sticker_cache: tuple[float, list[dict]] = (0.0, [])


async def sticker_candidates(limit: int = 30) -> list[dict]:
    global _sticker_cache
    if time.monotonic() - _sticker_cache[0] < 600:
        return _sticker_cache[1]
    async with get_session() as session:
        rows = (
            await session.scalars(
                select(Sticker)
                .where(Sticker.enabled, Sticker.summary != "")
                .order_by(Sticker.seen_count.desc())
                .limit(limit)
            )
        ).all()
    cands = [{"id": s.id, "summary": s.summary} for s in rows]
    _sticker_cache = (time.monotonic(), cands)
    return cands


async def draft_ack(request: str, recent: str, settings: Settings) -> AckDecision:
    cfg = settings.interaction
    stickers = await sticker_candidates() if cfg.sticker_ack else []
    resolved = chat_model("quick_ack")
    agent = Agent(resolved.model, output_type=AckDecision, name="quick_ack")
    prompt = render(
        "quick_ack",
        persona=settings.persona,
        style=render("style_rules"),
        request=request[:800],
        recent=recent[-1500:],
        stickers=stickers,
        allow_sticker=cfg.sticker_ack,
    )
    result = await agent.run(prompt, model_settings=resolved.settings)
    usage = result.usage
    record_usage("quick_ack", resolved.label, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
    decision = result.output
    if decision.kind == "sticker" and decision.sticker_id not in {s["id"] for s in stickers}:
        decision = AckDecision(kind="text", text=decision.text or "稍等，我看看")
    return decision


async def review_notice(request: str, settings: Settings) -> str:
    resolved = chat_model("quick_ack")
    agent = Agent(resolved.model, name="review_notice")
    result = await agent.run(
        render("review_notice", persona=settings.persona, request=request[:800]),
        model_settings=resolved.settings,
    )
    return result.output.strip()[:60]
