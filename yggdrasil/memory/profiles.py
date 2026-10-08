"""Incremental user profiles (task ``memory_profile``).

A profile is updated from closed topics newer than ``last_topic_id`` in which the
user wrote at least ``profile_min_messages`` messages. Every fact keeps the ids of the
messages supporting it. Opted-out users are skipped entirely.
"""

from collections.abc import Collection, Sequence

from pydantic import BaseModel, Field
from sqlalchemy import delete, exists, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Message, Topic, TopicMessage, UserProfile, UserProfileFact
from yggdrasil.db.session import get_session
from yggdrasil.memory import llm
from yggdrasil.memory.common import format_line
from yggdrasil.memory.search import VIEW_COLUMNS, MessageView
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot

TASK = "memory_profile"
MAX_USERS_PER_RUN = 20
MAX_TOPICS = 20
MAX_MESSAGES = 80
MAX_FACTS_IN_PROMPT = 40

_DUE_SQL = text("""
SELECT m.user_id, count(*) AS n
FROM topics t
JOIN topic_messages tm ON tm.topic_id = t.id
JOIN messages m ON m.id = tm.message_id AND m.group_id = t.group_id AND m.sent_at BETWEEN t.start_at AND t.end_at
LEFT JOIN user_profiles p ON p.group_id = t.group_id AND p.user_id = m.user_id
WHERE t.group_id = :g AND t.status = 'closed' AND t.id > COALESCE(p.last_topic_id, 0)
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
        topics = (
            await session.scalars(
                select(Topic)
                .where(
                    Topic.group_id == group_id,
                    Topic.status == "closed",
                    Topic.id > ((profile.last_topic_id if profile else None) or 0),
                    participated,
                )
                .order_by(Topic.id)
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
    prompt = render(
        TASK,
        bot_name=snap.settings(group_id).persona.name,
        name=llm.fence(name),
        user_id=user_id,
        profile={
            "summary": llm.fence(profile.summary) if profile else "",
            "interests": [llm.fence(s) for s in profile.interests] if profile else [],
            "traits": [llm.fence(s) for s in profile.traits] if profile else [],
        },
        facts=[{"id": fid, "fact": llm.fence(f)} for fid, f in facts],
        topics=[{"title": llm.fence(t.title), "summary": llm.fence(t.summary)} for t in topics],
        messages="\n".join(llm.fence(format_line(m, with_id=True)[:300]) for m in messages),
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
            "last_topic_id": max(t.id for t in topics),
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
            _DUE_SQL, {"g": group_id, "min": mem.profile_min_messages, "lim": MAX_USERS_PER_RUN}
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
