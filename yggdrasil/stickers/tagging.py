"""Vision tagging of collected stickers (worker): description, text, emotion tags, when to use, and moderation.

Task ``sticker`` (a vision route, e.g. Claude Haiku). One call per sticker covers both tagging and moderation; an
unsafe verdict is recorded as ``unsafe_category`` and disables the sticker only when the global
``moderation.disable_unsafe_stickers`` setting is on. Each sticker gets ``MAX_ATTEMPTS`` tries before it is marked
failed.
"""

import asyncio
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import BinaryContent
from sqlalchemy import select, update

from yggdrasil.agent.media import fit_image, media_type
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Sticker, StickerBlob
from yggdrasil.db.session import get_session
from yggdrasil.memory import llm
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot

TASK = "sticker"
BATCH = 30
MAX_ATTEMPTS = 3


class StickerTags(BaseModel):
    description: str = Field(description="一句话描述画面")
    ocr_text: str = Field(default="", description="图中文字，原样；没有则为空")
    emotions: list[str] = Field(description="1-4 个情绪或语气标签")
    usage: str = Field(description="群聊里什么时候适合发")
    safe: bool
    unsafe_category: Literal["", "sexual", "gore", "extremism", "personal_data"] = ""


async def tag_pending(limit: int = BATCH) -> dict[str, int]:
    """Tag up to ``limit`` downloaded stickers, most-seen first."""
    if not llm.available(TASK):
        return {"skipped": 1}
    async with get_session() as session:
        ids = (
            await session.scalars(
                select(Sticker.id)
                .where(Sticker.status == "untagged")
                .order_by(Sticker.seen_count.desc(), Sticker.id)
                .limit(limit)
            )
        ).all()
    instructions = render("sticker_tag")
    stats = {"tagged": 0, "unsafe": 0, "failed": 0}
    for sticker_id in ids:
        async with get_session() as session:
            blob = await session.scalar(select(StickerBlob.data).where(StickerBlob.sticker_id == sticker_id))
        if blob is None:
            continue
        image = await asyncio.to_thread(fit_image, blob)
        try:
            tags = await llm.run_structured(
                TASK,
                StickerTags,
                ["给这张表情包打标签。", BinaryContent(data=image, media_type=media_type(image))],
                instructions=instructions,
            )
        except Exception as exc:
            LOGGER.warning("sticker {} tagging failed: {!r}", sticker_id, exc)
            stats["failed"] += await _record_failure(sticker_id, exc)
            continue
        await _save(sticker_id, tags)
        stats["tagged"] += 1
        stats["unsafe"] += not tags.safe
    return stats


async def _save(sticker_id: int, tags: StickerTags) -> None:
    values: dict[str, object] = {
        "status": "ready",
        "error": "",
        "description": tags.description.strip(),
        "ocr_text": tags.ocr_text.strip(),
        "emotions": [e.strip() for e in tags.emotions if e.strip()][:4],
        "usage": tags.usage.strip(),
        "tagged_at": datetime.now(UTC),
        "unsafe_category": "" if tags.safe else tags.unsafe_category or "unspecified",
    }
    if not tags.safe and snapshot().settings().moderation.disable_unsafe_stickers:
        values["enabled"] = False
    async with get_session() as session:
        await session.execute(update(Sticker).where(Sticker.id == sticker_id).values(**values))


async def _record_failure(sticker_id: int, exc: Exception) -> int:
    """Count the attempt; returns 1 once the sticker gives up for good."""
    async with get_session() as session:
        attempts = await session.scalar(
            update(Sticker)
            .where(Sticker.id == sticker_id)
            .values(tag_attempts=Sticker.tag_attempts + 1, error=f"tagging: {exc}"[:300])
            .returning(Sticker.tag_attempts)
        )
        if attempts is not None and attempts >= MAX_ATTEMPTS:
            await session.execute(update(Sticker).where(Sticker.id == sticker_id).values(status="failed"))
            return 1
    return 0
