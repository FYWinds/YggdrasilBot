"""Synthetic, public-safe data for a local console::

    uv run python -m yggdrasil.devtools.seed [--reset]

Two example groups (10001, 10002) get members, chat covering every QQ segment type, stickers, memory (topics with
curation scores, one archived; a digest; glossary; profiles with active, archived and restored facts), bot events,
review items, the bot's invocations of every handler and outcome, and background jobs (archived ones and one running
in Redis). Transcripts come from the real recorder: each invocation is a scripted ``FunctionModel`` run whose prompt
is built by the real ``build_context`` and whose tools record what they showed the way the real ones do; job
transcripts go through the real omp message conversion.

Everything written is scoped (the two groups, members 20001-20009, ``seed-`` job ids, the seed provider, stickers
and images), so running it again replaces the previous seed and ``--reset`` only removes it. Uses the usual
``POSTGRES_*`` / ``REDIS_URL`` settings and refuses to run with ``YGG_ENV=prod``.
"""

import sys
from uuid import UUID

from sqlalchemy import delete, or_, select

from yggdrasil.db.models import (
    BotEvent,
    DailyDigest,
    GlossaryCandidate,
    GlossaryTerm,
    Group,
    Invocation,
    Message,
    MessageChunk,
    Provider,
    ReviewItem,
    SandboxJob,
    Sticker,
    StickerUsage,
    TaskRoute,
    Topic,
    Transcript,
    TranscriptBlob,
    UserAlias,
    UserProfile,
    UserProfileFact,
)
from yggdrasil.db.session import get_session
from yggdrasil.devtools.seed.chat import GAME_CHAT, TECH_CHAT, insert_chat
from yggdrasil.devtools.seed.data import (
    FLAG_PREFIX,
    GAME,
    GROUPS,
    JOB_IDS,
    MEDIA,
    PROVIDER,
    TECH,
    TRACE_PREFIX,
    broken_ref,
    mface_segment,
)
from yggdrasil.devtools.seed.jobs import add_jobs
from yggdrasil.devtools.seed.replay import invoke
from yggdrasil.devtools.seed.scenarios import PRIVATE_OPS, RUNS
from yggdrasil.devtools.seed.tables import add_base, add_events, add_memory, add_reviews, add_stickers
from yggdrasil.kv import redis_client
from yggdrasil.sandbox.jobs import ACTIVE_KEY, events_key, group_key, job_key, running_key
from yggdrasil.transcript.notes import blob_id


async def reset() -> None:
    groups = list(GROUPS)
    seeded_events = or_(BotEvent.group_id.in_(groups), BotEvent.data["flag"].astext.startswith(FLAG_PREFIX))
    async with get_session() as s:
        await s.execute(delete(Transcript).where(Transcript.job_id.in_(JOB_IDS)))
        await s.execute(
            delete(Invocation).where(or_(Invocation.group_id.in_(groups), Invocation.trace_id.startswith(TRACE_PREFIX)))
        )
        await s.execute(delete(ReviewItem).where(ReviewItem.group_id.in_(groups)))
        await s.execute(delete(BotEvent).where(seeded_events))
        await s.execute(delete(SandboxJob).where(SandboxJob.id.in_(JOB_IDS)))
        await s.execute(delete(Message).where(Message.group_id.in_(groups)))
        await s.execute(delete(UserAlias).where(UserAlias.group_id.in_(groups)))
        await s.execute(delete(MessageChunk).where(MessageChunk.group_id.in_(groups)))
        await s.execute(delete(Topic).where(Topic.group_id.in_(groups)))  # topic_messages cascade
        for model in (DailyDigest, GlossaryTerm, GlossaryCandidate, UserProfile, UserProfileFact):
            await s.execute(delete(model).where(model.group_id.in_(groups)))
        refs = [m.md5 for m in MEDIA.values()] + [broken_ref(), f"230001:{mface_segment()['data']['emoji_id']}"]
        await s.execute(delete(Sticker).where(Sticker.ref.in_(refs)))  # blobs and usage cascade
        await s.execute(delete(StickerUsage).where(StickerUsage.group_id.in_(groups)))
        shas = [blob_id(m.data) for m in MEDIA.values()]
        await s.execute(delete(TranscriptBlob).where(TranscriptBlob.sha256.in_(shas)))
        provider = await s.scalar(select(Provider.id).where(Provider.name == PROVIDER))
        if provider is not None:
            await s.execute(delete(TaskRoute).where(TaskRoute.provider_id == provider))
            await s.execute(delete(Provider).where(Provider.id == provider))
        await s.execute(delete(Group).where(Group.group_id.in_(groups)))
    try:
        r = redis_client()
        for job_id in JOB_IDS:
            await r.delete(job_key(job_id), events_key(job_id))
            await r.srem(ACTIVE_KEY, job_id)  # pyright: ignore[reportGeneralTypeIssues]
        for g in groups:
            await r.zrem(group_key(g), *JOB_IDS)
            await r.hdel(running_key(g), *JOB_IDS)  # pyright: ignore[reportGeneralTypeIssues]
    except Exception as e:
        sys.stderr.write(f"redis unavailable, skipped clearing the running job: {e}\n")


async def seed() -> None:
    await reset()
    await add_base()
    chats = {TECH: await insert_chat(TECH, TECH_CHAT, 5_000_000), GAME: await insert_chat(GAME, GAME_CHAT, 6_000_000)}
    await add_stickers()
    await add_memory(chats)
    await add_events()
    ids: dict[str, UUID] = {}
    for run in RUNS:
        ids[run.key] = await invoke(run, chats)
    await invoke(PRIVATE_OPS, chats)
    await add_reviews(ids["ask_review"])
    await add_jobs(chats)
