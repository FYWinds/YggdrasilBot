"""Incremental user profiles (task ``memory_profile``).

A profile is updated from the closed topics it has not consumed yet in which the user
wrote at least ``profile_min_messages`` messages. Topics are consumed in close order
(``closed_at``, then id), not id order: topics close in a different order than they start,
so an id watermark skipped a topic still open when a newer one was consumed. Every fact
keeps the ids of the messages supporting it. Opted-out users are skipped entirely.
"""

from collections.abc import Collection, Sequence
from datetime import timedelta

from pydantic import BaseModel, Field
from sqlalchemy import delete, exists, func, select, text, true, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Message, Topic, TopicMessage, UserProfile, UserProfileFact
from yggdrasil.db.session import get_session
from yggdrasil.memory import llm
from yggdrasil.memory.common import format_line, people_for
from yggdrasil.memory.search import VIEW_COLUMNS, MessageView
from yggdrasil.people import UNKNOWN_NAME, clean_name
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot

TASK = "memory_profile"
MAX_USERS_PER_RUN = 20
MAX_TOPICS = 20
MAX_MESSAGES = 80
MAX_FACTS_IN_PROMPT = 40
# Only topics closed this long ago are consumed: a close committing while a run reads must not
# land behind the watermark that run sets.
CLOSE_SETTLE = timedelta(minutes=5)

_DUE_SQL = text("""
SELECT m.user_id, count(*) AS n
FROM topics t
JOIN topic_messages tm ON tm.topic_id = t.id
JOIN messages m ON m.id = tm.message_id AND m.group_id = t.group_id AND m.sent_at BETWEEN t.start_at AND t.end_at
LEFT JOIN user_profiles p ON p.group_id = t.group_id AND p.user_id = m.user_id
WHERE t.group_id = :g AND t.status = 'closed' AND t.closed_at < now() - CAST(:settle AS interval)
  AND (p.last_closed_at IS NULL OR (t.closed_at, t.id) > (p.last_closed_at, p.last_topic_id))
  AND NOT m.is_self AND NOT m.recalled AND NOT COALESCE(p.opted_out, false)
GROUP BY m.user_id
HAVING count(*) >= :min
ORDER BY n DESC
LIMIT :lim
""")


class NewFact(BaseModel):
    fact: str
    source_msg_ids: list[int]


class ProfileUpdate(BaseModel):
    summary: str
    interests: list[str] = Field(default_factory=list)
    traits: list[str] = Field(default_factory=list)
    new_facts: list[NewFact] = Field(default_factory=list)
    remove_fact_ids: list[int] = Field(default_factory=list)


def validate_update(update: ProfileUpdate, shown_msg_ids: Collection[int], fact_ids: Collection[int]) -> ProfileUpdate:
    """Facts must cite messages that were shown; removals must name existing facts."""
    shown, known = set(shown_msg_ids), set(fact_ids)
    facts = []
    for f in update.new_facts:
        sources = [i for i in dict.fromkeys(f.source_msg_ids) if i in shown]
        if f.fact.strip() and sources:
            facts.append(NewFact(fact=f.fact.strip(), source_msg_ids=sources))
    return update.model_copy(
        update={
            "summary": update.summary.strip(),
            "interests": [s.strip() for s in update.interests if s.strip()][:10],
            "traits": [s.strip() for s in update.traits if s.strip()][:8],
            "new_facts": facts,
            "remove_fact_ids": [i for i in dict.fromkeys(update.remove_fact_ids) if i in known],
        }
    )


async def update_user(group_id: int, user_id: int) -> bool:
    """Update one profile from its new topics; False when skipped (opted out / nothing new)."""
    snap = snapshot()
    async with get_session() as session:
        profile = await session.get(UserProfile, (group_id, user_id))
        if profile is not None and profile.opted_out:
            return False
        participated = (
            select(TopicMessage.topic_id)
            .join(Message, Message.id == TopicMessage.message_id)
            .where(
                TopicMessage.topic_id == Topic.id,
                Message.group_id == group_id,
                Message.user_id == user_id,
                Message.recalled.is_(False),
            )
            .exists()
        )
        unconsumed = (
            tuple_(Topic.closed_at, Topic.id) > tuple_(profile.last_closed_at, profile.last_topic_id)
            if profile is not None and profile.last_closed_at is not None
            else true()
        )
        topics = (
            await session.scalars(
                select(Topic)
                .where(
                    Topic.group_id == group_id,
                    Topic.status == "closed",
                    Topic.closed_at < func.now() - CLOSE_SETTLE,
                    unconsumed,
                    participated,
                )
                .order_by(Topic.closed_at, Topic.id)
                .limit(MAX_TOPICS)
            )
        ).all()
        if not topics:
            return False
        rows = await session.execute(
            select(*VIEW_COLUMNS)
            .join(TopicMessage, TopicMessage.message_id == Message.id)
            .where(
                TopicMessage.topic_id.in_([t.id for t in topics]),
                Message.group_id == group_id,
                Message.user_id == user_id,
                Message.recalled.is_(False),
                Message.sent_at.between(min(t.start_at for t in topics), max(t.end_at for t in topics)),
            )
            .order_by(Message.id.desc())
            .limit(MAX_MESSAGES)
        )
        messages = [MessageView(*r) for r in reversed(rows.all())]
        facts = (
            await session.execute(
                select(UserProfileFact.id, UserProfileFact.fact)
                .where(UserProfileFact.group_id == group_id, UserProfileFact.user_id == user_id)
                .order_by(UserProfileFact.id.desc())
                .limit(MAX_FACTS_IN_PROMPT)
            )
        ).all()
    if not messages:
        return False
    name = messages[-1].sender_name or str(user_id)
    bot_name = snap.settings(group_id).persona.name
    people = await people_for(group_id, messages, bot_name=bot_name)
    prompt = render(
        TASK,
        bot_name=bot_name,
        name=llm.fence(clean_name(people.names.get(user_id) or messages[-1].sender_name) or UNKNOWN_NAME),
        user_id=user_id,
        profile={
            "summary": llm.fence(profile.summary) if profile else "",
            "interests": [llm.fence(s) for s in profile.interests] if profile else [],
            "traits": [llm.fence(s) for s in profile.traits] if profile else [],
        },
        facts=[{"id": fid, "fact": llm.fence(f)} for fid, f in facts],
        topics=[{"title": llm.fence(t.title), "summary": llm.fence(t.summary)} for t in topics],
        messages="\n".join(llm.fence(format_line(m, people, with_id=True)[:300]) for m in messages),
    )
    result = validate_update(
        await llm.run_structured(TASK, ProfileUpdate, prompt), [m.id for m in messages], [f[0] for f in facts]
    )
    async with get_session() as session:
        # The user may have opted out while the model was running.
        if await session.scalar(
            select(
                exists().where(UserProfile.group_id == group_id, UserProfile.user_id == user_id, UserProfile.opted_out)
            )
        ):
            return False
        values = {
            "display_name": name,
            "summary": result.summary or (profile.summary if profile else ""),
            "interests": result.interests,
            "traits": result.traits,
            "last_closed_at": topics[-1].closed_at,
            "last_topic_id": topics[-1].id,
            # ON CONFLICT DO UPDATE skips the column's onupdate.
            "updated_at": func.now(),
        }
        stmt = pg_insert(UserProfile).values(group_id=group_id, user_id=user_id, **values)
        await session.execute(stmt.on_conflict_do_update(index_elements=["group_id", "user_id"], set_=values))
        if result.remove_fact_ids:
            await session.execute(
                delete(UserProfileFact).where(
                    UserProfileFact.group_id == group_id,
                    UserProfileFact.user_id == user_id,
                    UserProfileFact.id.in_(result.remove_fact_ids),
                )
            )
        session.add_all(
            UserProfileFact(group_id=group_id, user_id=user_id, fact=f.fact, source_msg_ids=f.source_msg_ids)
            for f in result.new_facts
        )
    return True


async def due_users(group_id: int) -> Sequence[int]:
    mem = snapshot().settings(group_id).memory
    async with get_session() as session:
        rows = await session.execute(
            _DUE_SQL,
            {"g": group_id, "min": mem.profile_min_messages, "lim": MAX_USERS_PER_RUN, "settle": CLOSE_SETTLE},
        )
        return [r.user_id for r in rows]


async def update_group(group_id: int) -> dict[str, int]:
    """Hourly entry point: update every due profile of the group (one user's failure doesn't stop others)."""
    stats = {"updated": 0, "failed": 0}
    if not llm.available(TASK):
        LOGGER.debug("group {}: task {} not configured", group_id, TASK)
        return stats
    for user_id in await due_users(group_id):
        try:
            stats["updated"] += await update_user(group_id, user_id)
        except Exception:
            stats["failed"] += 1
            LOGGER.exception("group {}: profile update failed", group_id)
    return stats
