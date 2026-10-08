"""Fast first responses: a model-picked reaction (tldr) or a short line / sticker (chat)."""

import time

from pydantic_ai import Agent
from sqlalchemy import select

from yggdrasil.agent.schema import AckDecision, ReactDecision
from yggdrasil.db.models import Sticker
from yggdrasil.db.session import get_session
from yggdrasil.delivery.emoji import emoji_label
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


async def pick_reaction(content: str, settings: Settings) -> str:
    cfg = settings.interaction
    allowed = list(dict.fromkeys(cfg.react_emoji_whitelist)) or [cfg.react_default]
    resolved = chat_model("quick_ack")
    agent = Agent(resolved.model, output_type=ReactDecision, name="tldr_react")
    prompt = render("tldr_react", content=content[:1500], emojis=[{"id": e, "label": emoji_label(e)} for e in allowed])
    result = await agent.run(prompt, model_settings=resolved.settings)
    usage = result.usage
    record_usage("quick_ack", resolved.label, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
    return result.output.emoji_id if result.output.emoji_id in allowed else cfg.react_default


async def draft_ack(request: str, recent: str, settings: Settings) -> AckDecision:
    cfg = settings.interaction
    stickers = await sticker_candidates() if cfg.sticker_ack else []
    resolved = chat_model("quick_ack")
    agent = Agent(resolved.model, output_type=AckDecision, name="quick_ack")
    prompt = render(
        "quick_ack",
        persona_name=settings.persona.name,
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
        render("review_notice", persona_name=settings.persona.name, request=request[:800]),
        model_settings=resolved.settings,
    )
    return result.output.strip()[:60]
