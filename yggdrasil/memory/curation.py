"""Memory curation: hourly scoring and archiving of closed topics and profile facts.

Topics (route ``memory_topic``): closed topics are rated newest first, in batches. The model rates each 1–5 with a
short reason; its weight scales that by size (:func:`~yggdrasil.memory.common.topic_weight`), and topics below
``topic_archive_below_weight`` are archived (out of search, digests, profile updates and prompts).

Facts (route ``memory_profile``): one call per user rates the unscored facts and names near-duplicates among the
user's active facts. Duplicates merge into the fact the model keeps (sources unioned, the rest archived), facts
rated below ``fact_archive_below_importance`` are archived, and each user keeps at most ``facts_per_user`` active
facts ranked by weight with recency decay. The profile updater rates new facts itself, so once the backlog is
done this mostly enforces the cap.

Archived facts stay in the table and can be restored in the console; restored facts are ``locked`` and never
archived again. Items an answer leaves out stay unrated, and an answer that rates nothing it was shown (empty, or
only ids that weren't shown) counts as a failure: both are asked about again next run. Per-run caps let an existing
backlog drain over a few runs.
"""

from collections.abc import Collection, Sequence

from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Topic, UserProfile, UserProfileFact
from yggdrasil.db.session import get_session
from yggdrasil.memory import llm
from yggdrasil.memory.common import fact_rank, fact_weight, local, topic_weight
from yggdrasil.memory.topics import archivable
from yggdrasil.people import UNKNOWN_NAME, clean_name
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot
from yggdrasil.settings import MemorySettings

PROMPT = "memory_curation"
TOPIC_TASK = "memory_topic"
FACT_TASK = "memory_profile"
TOPIC_BATCH = 25
MAX_TOPICS_PER_RUN = 200
MAX_USERS_PER_RUN = 20
MAX_FACTS_PER_CALL = 150
MAX_KNOWN_FACTS = 60  # already rated facts shown to find duplicates of the new ones
SUMMARY_MAX_CHARS = 400
REASON_MAX_CHARS = 40


class TopicScore(BaseModel):
    id: int
    importance: int = Field(description="1–5")
    reason: str = Field(default="", description="一句话理由，不超过 20 字")


class TopicScores(BaseModel):
    scores: list[TopicScore]


class FactScore(BaseModel):
    id: int
    importance: int = Field(description="1–5")


class FactMerge(BaseModel):
    keep: int = Field(description="保留的那条事实的 id")
    duplicates: list[int] = Field(description="和它说同一件事、要并入它的事实 id")


class FactReview(BaseModel):
    scores: list[FactScore] = Field(default_factory=list)
    merges: list[FactMerge] = Field(default_factory=list)


class NothingRated(ValueError):
    """The answer rated none of the items it was shown (empty, or only ids that weren't shown)."""


def clamp_importance(value: int) -> int:
    return min(max(value, 1), 5)


def valid_topic_scores(scores: Sequence[TopicScore], topic_ids: Collection[int]) -> dict[int, TopicScore]:
    """The first score of each shown topic, importance clamped to 1–5, reason on one short line."""
    shown = set(topic_ids)
    out: dict[int, TopicScore] = {}
    for s in scores:
        if s.id in shown and s.id not in out:
            reason = " ".join(s.reason.split())[:REASON_MAX_CHARS]
            out[s.id] = TopicScore(id=s.id, importance=clamp_importance(s.importance), reason=reason)
    return out


def valid_fact_scores(scores: Sequence[FactScore], fact_ids: Collection[int]) -> dict[int, int]:
    """fact id -> importance (1–5) for the facts that were up for rating; the first score wins."""
    shown = set(fact_ids)
    out: dict[int, int] = {}
    for s in scores:
        if s.id in shown and s.id not in out:
            out[s.id] = clamp_importance(s.importance)
    return out


def valid_merges(
    merges: Sequence[FactMerge], fact_ids: Collection[int], locked: Collection[int]
) -> dict[int, list[int]]:
    """kept fact id -> duplicate ids. Only shown facts, each in one merge at most; locked facts are never merged
    away (an admin restored them)."""
    shown, keep_out = set(fact_ids), set(locked)
    used: set[int] = set()
    out: dict[int, list[int]] = {}
    for m in merges:
        if m.keep not in shown or m.keep in used:
            continue
        dups = [i for i in dict.fromkeys(m.duplicates) if i in shown and i not in used and i not in keep_out]
        dups = [i for i in dups if i != m.keep]
        if dups:
            used.update([m.keep, *dups])
            out[m.keep] = dups
    return out


async def _score_topics(group_id: int, mem: MemorySettings, stats: dict[str, int]) -> None:
    bot_name = snapshot().settings(group_id).persona.name
    sent: list[int] = []  # each topic is asked about once per run
    while len(sent) < MAX_TOPICS_PER_RUN:
        async with get_session() as session:
            batch = (
                await session.scalars(
                    select(Topic)
                    .where(
                        Topic.group_id == group_id,
                        Topic.status == "closed",
                        Topic.scored_at.is_(None),
                        Topic.id.notin_(sent),
                    )
                    .order_by(Topic.closed_at.desc().nulls_last(), Topic.id.desc())
                    .limit(min(TOPIC_BATCH, MAX_TOPICS_PER_RUN - len(sent)))
                )
            ).all()
        if not batch:
            return
        ids = [t.id for t in batch]
        sent += ids
        prompt = render(
            PROMPT,
            mode="topics",
            bot_name=bot_name,
            topics=[
                {
                    "id": t.id,
                    "day": f"{local(t.start_at):%Y-%m-%d}",
                    "title": llm.fence(t.title),
                    "summary": llm.fence(t.summary[:SUMMARY_MAX_CHARS]),
                    "keywords": [llm.fence(k) for k in t.keywords],
                    "msg_count": t.msg_count,
                    "people": len(t.participants),
                }
                for t in batch
            ],
        )
        try:
            result = await llm.run_structured(TOPIC_TASK, TopicScores, prompt)
            scores = valid_topic_scores(result.scores, ids)
            if not scores:
                raise NothingRated(f"none of the {len(ids)} topics shown")
        except Exception:
            # Nothing is marked: these topics come first again next run.
            stats["failed"] += 1
            LOGGER.exception("group {}: topic curation failed", group_id)
            return
        async with get_session() as session:
            rated: list[int] = []
            for t in batch:
                if (s := scores.get(t.id)) is None:
                    continue  # left out of the answer: stays unrated, asked about again next run
                rated += await session.scalars(
                    update(Topic)
                    .where(Topic.id == t.id, Topic.status == "closed", Topic.scored_at.is_(None))
                    .values(
                        importance=s.importance,
                        importance_reason=s.reason or None,
                        weight=topic_weight(s.importance, t.msg_count, len(t.participants)),
                        scored_at=func.now(),
                    )
                    .returning(Topic.id)
                )
            archived = await session.execute(
                update(Topic).where(Topic.id.in_(rated), archivable(mem)).values(status="archived")
            )
        stats["topics_scored"] += len(rated)
        stats["topics_archived"] += archived.rowcount or 0  # type: ignore[attr-defined]
        if len(batch) < TOPIC_BATCH:
            return


async def _fact_users(group_id: int) -> list[int]:
    """Members with unrated active facts (not opted out), the most unrated first."""
    opted_out = exists().where(
        UserProfile.group_id == group_id, UserProfile.user_id == UserProfileFact.user_id, UserProfile.opted_out
    )
    async with get_session() as session:
        rows = await session.execute(
            select(UserProfileFact.user_id)
            .where(
                UserProfileFact.group_id == group_id,
                UserProfileFact.archived_at.is_(None),
                UserProfileFact.scored_at.is_(None),
                ~opted_out,
            )
            .group_by(UserProfileFact.user_id)
            .order_by(func.count().desc(), UserProfileFact.user_id)
            .limit(MAX_USERS_PER_RUN)
        )
        return list(rows.scalars())


async def _review_facts(group_id: int, user_id: int, mem: MemorySettings, stats: dict[str, int]) -> None:
    """Rate one member's unrated facts, merge duplicates and archive the low ones (one model call)."""
    async with get_session() as session:
        profile = await session.get(UserProfile, (group_id, user_id))
        active = select(UserProfileFact).where(
            UserProfileFact.group_id == group_id,
            UserProfileFact.user_id == user_id,
            UserProfileFact.archived_at.is_(None),
        )
        unrated = (
            await session.scalars(
                active.where(UserProfileFact.scored_at.is_(None)).order_by(UserProfileFact.id).limit(MAX_FACTS_PER_CALL)
            )
        ).all()
        known = (
            await session.scalars(
                active.where(UserProfileFact.scored_at.is_not(None))
                .order_by(fact_rank(mem.weight_half_life_days).desc(), UserProfileFact.id.desc())
                .limit(MAX_KNOWN_FACTS)
            )
        ).all()
    if not unrated:
        return
    prompt = render(
        PROMPT,
        mode="facts",
        bot_name=snapshot().settings(group_id).persona.name,
        name=llm.fence(clean_name(profile.display_name if profile else "") or UNKNOWN_NAME),
        user_id=user_id,
        facts=[{"id": f.id, "fact": llm.fence(f.fact)} for f in unrated],
        known=[{"id": f.id, "fact": llm.fence(f.fact)} for f in known],
    )
    review = await llm.run_structured(FACT_TASK, FactReview, prompt)
    shown = [*unrated, *known]
    scores = valid_fact_scores(review.scores, [f.id for f in unrated])
    merges = valid_merges(review.merges, [f.id for f in shown], [f.id for f in shown if f.locked])
    if not scores and not merges:
        raise NothingRated(f"none of the {len(unrated)} facts shown")

    importance: dict[int, int | None] = {f.id: f.importance for f in shown} | scores
    sources = {f.id: list(f.source_msg_ids) for f in shown}
    for keep, dups in merges.items():
        group = (keep, *dups)
        sources[keep] = list(dict.fromkeys(s for i in group for s in sources[i]))
        rated = [v for i in group if (v := importance[i]) is not None]
        importance[keep] = max(rated) if rated else None
    duplicates = {i for dups in merges.values() for i in dups}
    stats["facts_scored"] += len(scores)
    async with get_session() as session:
        for f in shown:
            score = importance[f.id]
            values: dict[str, object]
            if f.id in duplicates:
                values = {"archived_at": func.now(), "archive_reason": "duplicate"}
                stats["facts_merged"] += 1
            elif f.id in merges or (f.scored_at is None and score is not None):
                values = {"source_msg_ids": sources[f.id]}
                if score is not None:
                    values |= {"importance": score, "weight": fact_weight(score, len(sources[f.id]))}
                    if f.scored_at is None:
                        values["scored_at"] = func.now()
                        if score < mem.fact_archive_below_importance and not f.locked:
                            values |= {"archived_at": func.now(), "archive_reason": "low"}
                            stats["facts_archived"] += 1
            else:
                continue  # left out of the answer: an unrated fact stays unrated, asked about again next run
            await session.execute(
                update(UserProfileFact)
                .where(UserProfileFact.id == f.id, UserProfileFact.archived_at.is_(None))
                .values(**values)
            )


async def cap_facts(session: AsyncSession, group_id: int, mem: MemorySettings) -> int:
    """Archive what exceeds ``facts_per_user`` active facts per member, lowest weight with decay first. Members
    with unrated facts wait until they are rated; locked facts are kept (and still take a place)."""
    if mem.facts_per_user <= 0:
        return 0
    unrated = aliased(UserProfileFact)
    ranked = (
        select(
            UserProfileFact.id,
            UserProfileFact.locked,
            func.row_number()
            .over(
                partition_by=UserProfileFact.user_id,
                order_by=(fact_rank(mem.weight_half_life_days).desc(), UserProfileFact.id.desc()),
            )
            .label("rn"),
        )
        .where(
            UserProfileFact.group_id == group_id,
            UserProfileFact.archived_at.is_(None),
            ~exists().where(
                unrated.group_id == group_id,
                unrated.user_id == UserProfileFact.user_id,
                unrated.archived_at.is_(None),
                unrated.scored_at.is_(None),
            ),
        )
        .subquery()
    )
    overflow = select(ranked.c.id).where(ranked.c.rn > mem.facts_per_user, ~ranked.c.locked)
    result = await session.execute(
        update(UserProfileFact)
        .where(UserProfileFact.id.in_(overflow), UserProfileFact.archived_at.is_(None))
        .values(archived_at=func.now(), archive_reason="cap")
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def curate_group(group_id: int) -> dict[str, int]:
    """Hourly entry point (see module docstring); one member's failure doesn't stop the others."""
    mem = snapshot().settings(group_id).memory
    keys = ("topics_scored", "topics_archived", "facts_scored", "facts_merged", "facts_archived", "facts_capped")
    stats: dict[str, int] = dict.fromkeys((*keys, "failed"), 0)
    if llm.available(TOPIC_TASK):
        await _score_topics(group_id, mem, stats)
    else:
        LOGGER.debug("group {}: task {} not configured; topics stay unscored", group_id, TOPIC_TASK)
    if llm.available(FACT_TASK):
        for user_id in await _fact_users(group_id):
            try:
                await _review_facts(group_id, user_id, mem, stats)
            except Exception:
                stats["failed"] += 1
                LOGGER.exception("group {}: fact curation failed", group_id)
    else:
        LOGGER.debug("group {}: task {} not configured; facts stay unscored", group_id, FACT_TASK)
    async with get_session() as session:
        stats["facts_capped"] = await cap_facts(session, group_id, mem)
    return stats
