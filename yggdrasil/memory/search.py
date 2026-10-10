"""Read API over group memory, used by the bot's agent tools and context assembly.

Every query is scoped to one group and excludes recalled messages.
"""

from collections.abc import Hashable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import Row, bindparam, func, or_, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil.core.log import LOGGER
from yggdrasil.db.base import EMBED_DIM
from yggdrasil.db.models import GlossaryTerm, Message, Topic, UserProfile, UserProfileFact
from yggdrasil.db.session import get_session
from yggdrasil.llm import registry
from yggdrasil.memory.vectors import query_text
from yggdrasil.runtime import snapshot

RRF_K = 60
BM25_TOP = 50
VECTOR_TOP = 30
RERANK_POOL = 40
RERANK_MAX_CHARS = 1200
RECENT_CAP = 300
QQ_ID_LOOKBACK = timedelta(days=7)
GLOSSARY_MAX_DISTANCE = 0.45
PROFILE_FACTS = 20


@dataclass(frozen=True, slots=True)
class MessageView:
    id: int
    qq_message_id: int
    sent_at: datetime
    user_id: int
    sender_name: str
    text: str
    reply_to_qq_id: int | None
    is_self: bool


@dataclass(frozen=True, slots=True)
class HistoryHit:
    kind: str  # message | chunk | topic | digest
    ref_id: int
    text: str
    when: datetime
    score: float
    msg_id: int | None = None
    user_id: int | None = None
    sender_name: str | None = None


@dataclass(frozen=True, slots=True)
class TopicView:
    id: int
    title: str
    summary: str
    keywords: list[str]
    participants: list[int]
    start_at: datetime
    end_at: datetime
    msg_count: int


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    id: int
    term: str
    aliases: list[str]
    definition: str
    confidence: float
    locked: bool


@dataclass(frozen=True, slots=True)
class ProfileView:
    user_id: int
    display_name: str
    summary: str
    interests: list[str]
    traits: list[str]
    facts: list[str]
    updated_at: datetime


VIEW_COLUMNS = (
    Message.id,
    Message.qq_message_id,
    Message.sent_at,
    Message.user_id,
    Message.sender_name,
    Message.plain_text,
    Message.reply_to_qq_id,
    Message.is_self,
)


def _view(row: Row[Any] | Sequence[Any]) -> MessageView:
    return MessageView(*row)


def _visible(group_id: int) -> tuple[Any, ...]:
    return (Message.group_id == group_id, Message.recalled.is_(False))


# --- raw messages -----------------------------------------------------------------------------


async def recent_messages(group_id: int, *, limit: int = 40, minutes: int = 0) -> list[MessageView]:
    """The last ``limit`` messages, extended to everything from the last ``minutes`` minutes
    (capped at 300). Chronological."""
    newest_first = select(*VIEW_COLUMNS).where(*_visible(group_id)).order_by(Message.sent_at.desc(), Message.id.desc())
    async with get_session() as session:
        rows = list((await session.execute(newest_first.limit(limit))).all()) if limit > 0 else []
        if minutes > 0:
            since = datetime.now(UTC) - timedelta(minutes=minutes)
            windowed = await session.execute(newest_first.where(Message.sent_at >= since).limit(RECENT_CAP))
            seen = {r[0] for r in rows}
            rows.extend(r for r in windowed.all() if r[0] not in seen)
    views = sorted((_view(r) for r in rows), key=lambda v: (v.sent_at, v.id))
    return views[-max(limit, RECENT_CAP) :]


async def messages_around(group_id: int, message_id: int, before: int = 10, after: int = 10) -> list[MessageView]:
    """Context window around a message (included unless recalled). Chronological."""
    async with get_session() as session:
        anchor = await session.scalar(
            select(Message.sent_at).where(Message.id == message_id, Message.group_id == group_id)
        )
        if anchor is None:
            return []
        key = tuple_(Message.sent_at, Message.id)
        base = select(*VIEW_COLUMNS).where(*_visible(group_id))
        older = await session.execute(
            base.where(key < tuple_(anchor, message_id))
            .order_by(Message.sent_at.desc(), Message.id.desc())
            .limit(before)
        )
        newer = await session.execute(
            base.where(key >= tuple_(anchor, message_id)).order_by(Message.sent_at, Message.id).limit(after + 1)
        )
        rows = [*reversed(older.all()), *newer.all()]
    return [_view(r) for r in rows]


async def find_by_qq_id(group_id: int, qq_message_id: int) -> MessageView | None:
    """Resolve a NapCat message id (ids repeat over time, so only the last 7 days are searched)."""
    since = datetime.now(UTC) - QQ_ID_LOOKBACK
    async with get_session() as session:
        row = (
            await session.execute(
                select(*VIEW_COLUMNS)
                .where(*_visible(group_id), Message.qq_message_id == qq_message_id, Message.sent_at >= since)
                .order_by(Message.sent_at.desc())
                .limit(1)
            )
        ).first()
    return _view(row) if row else None


# --- hybrid history search --------------------------------------------------------------------


def rrf_fuse[K: Hashable](rankings: Sequence[Sequence[K]], k: int = RRF_K) -> list[tuple[K, float]]:
    """Reciprocal rank fusion: ``score(d) = Σ 1 / (k + rank)`` with 1-based ranks.

    Duplicates within one ranking count once (best rank). Ties keep first-seen order.
    """
    scores: dict[K, float] = {}
    for ranking in rankings:
        seen: set[K] = set()
        for rank, key in enumerate(ranking, start=1):
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


async def _bm25(
    session: AsyncSession,
    group_id: int,
    query: str,
    since: datetime | None,
    until: datetime | None,
    user_id: int | None,
) -> list[HistoryHit]:
    conds = ["plain_text ||| :q", "group_id = :g", "recalled = false"]
    params: dict[str, Any] = {"q": query, "g": group_id, "lim": BM25_TOP}
    if since is not None:
        conds.append("sent_at >= :since")
        params["since"] = since
    if until is not None:
        conds.append("sent_at < :until")
        params["until"] = until
    if user_id is not None:
        conds.append("user_id = :u")
        params["u"] = user_id
    sql = (
        "SELECT id, sent_at, user_id, sender_name, plain_text, pdb.score(id) AS score FROM messages "
        f"WHERE {' AND '.join(conds)} ORDER BY score DESC, id DESC LIMIT :lim"
    )
    rows = (await session.execute(text(sql), params)).all()
    return [
        HistoryHit("message", r.id, r.plain_text, r.sent_at, float(r.score), r.id, r.user_id, r.sender_name)
        for r in rows
    ]


async def _query_vector(query: str) -> tuple[int, list[float]] | None:
    space = snapshot().embed_space
    if space is None or space.dim != EMBED_DIM:
        return None
    [vec] = await registry.embed([query_text(space.model, query)], provider_id=space.provider_id, model=space.model)
    return space.id, vec


_VECTOR_SQL = """
SELECT v.kind, v.ref_id, v.embedding <=> CAST(:qv AS halfvec) AS dist,
       COALESCE(c.text, t.title || E'\\n' || t.summary, d.summary) AS body,
       COALESCE(c.start_at, t.start_at, d.day::timestamp AT TIME ZONE 'Asia/Shanghai') AS at
FROM memory_vectors v
LEFT JOIN message_chunks c ON v.kind = 'chunk' AND c.id = v.ref_id
LEFT JOIN topics t ON v.kind = 'topic' AND t.id = v.ref_id
LEFT JOIN daily_digests d ON v.kind = 'digest' AND d.id = v.ref_id
WHERE v.group_id = :g AND v.space_id = :s AND v.kind IN ('chunk', 'topic', 'digest')
  AND COALESCE(c.id, t.id, d.id) IS NOT NULL
"""


async def _vector(
    session: AsyncSession,
    group_id: int,
    space_id: int,
    vec: list[float],
    since: datetime | None,
    until: datetime | None,
    user_id: int | None,
) -> list[HistoryHit]:
    conds: list[str] = []
    params: dict[str, Any] = {"qv": vec, "g": group_id, "s": space_id, "lim": VECTOR_TOP}
    if since is not None:
        conds.append("COALESCE(c.end_at, t.end_at, (d.day + 1)::timestamp AT TIME ZONE 'Asia/Shanghai') >= :since")
        params["since"] = since
    if until is not None:
        conds.append("COALESCE(c.start_at, t.start_at, d.day::timestamp AT TIME ZONE 'Asia/Shanghai') < :until")
        params["until"] = until
    if user_id is not None:
        conds.append(
            "((v.kind = 'chunk' AND EXISTS (SELECT 1 FROM messages m WHERE m.group_id = :g AND m.user_id = :u"
            " AND NOT m.recalled AND m.id BETWEEN c.first_msg_id AND c.last_msg_id"
            " AND m.sent_at BETWEEN c.start_at AND c.end_at))"
            " OR (v.kind = 'topic' AND :u = ANY(t.participants)))"
        )
        params["u"] = user_id
    sql = _VECTOR_SQL + "".join(f"  AND {c}\n" for c in conds) + "ORDER BY dist LIMIT :lim"
    await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    stmt = text(sql).bindparams(bindparam("qv", type_=HALFVEC(EMBED_DIM)))
    rows = (await session.execute(stmt, params)).all()
    return [HistoryHit(r.kind, r.ref_id, r.body, r.at, 1.0 - float(r.dist)) for r in rows]


async def _rerank(query: str, hits: list[HistoryHit], top_k: int) -> list[HistoryHit] | None:
    try:
        ranked = await registry.rerank(query, [h.text[:RERANK_MAX_CHARS] for h in hits], top_n=top_k)
    except registry.TaskNotConfigured:
        return None
    except Exception as e:
        LOGGER.warning("rerank failed, keeping fused order: {!r}", e)
        return None
    return [replace(hits[i], score=score) for i, score in ranked[:top_k] if 0 <= i < len(hits)]


async def search_history(
    group_id: int,
    query: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    user_id: int | None = None,
    top_k: int = 12,
) -> list[HistoryHit]:
    """BM25 over messages + vectors over chunks/topics/digests, RRF-fused, then reranked if a rerank route exists."""
    query = query.strip()
    if not query or top_k <= 0:
        return []
    try:
        qv = await _query_vector(query)
    except Exception as e:
        LOGGER.warning("query embedding failed, BM25 only: {!r}", e)
        qv = None
    async with get_session() as session:
        lexical = await _bm25(session, group_id, query, since, until, user_id)
        semantic = await _vector(session, group_id, *qv, since, until, user_id) if qv else []
    by_key = {(h.kind, h.ref_id): h for h in (*semantic, *lexical)}
    fused = rrf_fuse([[(h.kind, h.ref_id) for h in lexical], [(h.kind, h.ref_id) for h in semantic]])
    pool = [replace(by_key[key], score=score) for key, score in fused[: max(RERANK_POOL, top_k)]]
    if len(pool) > 1:
        reranked = await _rerank(query, pool, top_k)
        if reranked is not None:
            return reranked
    return pool[:top_k]


# --- topics, glossary, profiles ---------------------------------------------------------------


def _topic_view(t: Topic) -> TopicView:
    return TopicView(
        t.id, t.title, t.summary, list(t.keywords), list(t.participants), t.start_at, t.end_at, t.msg_count
    )


async def open_topics(group_id: int, limit: int = 5) -> list[TopicView]:
    """Open topics, most recently active first."""
    async with get_session() as session:
        rows = await session.scalars(
            select(Topic)
            .where(Topic.group_id == group_id, Topic.status == "open")
            .order_by(Topic.end_at.desc())
            .limit(limit)
        )
        return [_topic_view(t) for t in rows]


def _entry(g: GlossaryTerm) -> GlossaryEntry:
    return GlossaryEntry(g.id, g.term, list(g.aliases), g.definition, g.confidence, g.locked)


async def glossary_terms(group_id: int) -> list[GlossaryEntry]:
    async with get_session() as session:
        rows = await session.scalars(select(GlossaryTerm).where(GlossaryTerm.group_id == group_id))
        return [_entry(g) for g in rows]


async def lookup_glossary(group_id: int, term: str, limit: int = 3) -> list[GlossaryEntry]:
    """Exact term/alias match (case-insensitive); else substring match; else nearest vectors."""
    term = term.strip()
    if not term:
        return []
    lowered = term.lower()
    async with get_session() as session:
        scoped = select(GlossaryTerm).where(GlossaryTerm.group_id == group_id)
        exact = (
            await session.scalars(
                scoped.where(
                    or_(
                        func.lower(GlossaryTerm.term) == lowered,
                        text("lower(:t) = ANY(SELECT lower(a) FROM unnest(glossary_terms.aliases) a)").bindparams(
                            t=term
                        ),
                    )
                )
            )
        ).all()
        if exact:
            return [_entry(g) for g in exact]
        pattern = f"%{term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
        fuzzy = (
            await session.scalars(
                scoped.where(or_(GlossaryTerm.term.ilike(pattern), GlossaryTerm.definition.ilike(pattern)))
                .order_by(func.length(GlossaryTerm.term))
                .limit(limit)
            )
        ).all()
        if fuzzy:
            return [_entry(g) for g in fuzzy]
    try:
        qv = await _query_vector(term)
    except Exception as e:
        LOGGER.warning("glossary query embedding failed: {!r}", e)
        return []
    if qv is None:
        return []
    space_id, vec = qv
    async with get_session() as session:
        await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        stmt = text(
            "SELECT v.ref_id FROM memory_vectors v WHERE v.group_id = :g AND v.space_id = :s"
            " AND v.kind = 'glossary' AND (v.embedding <=> CAST(:qv AS halfvec)) <= :maxd"
            " ORDER BY v.embedding <=> CAST(:qv AS halfvec) LIMIT :lim"
        ).bindparams(bindparam("qv", type_=HALFVEC(EMBED_DIM)))
        ids = list(
            (
                await session.scalars(
                    stmt,
                    {"g": group_id, "s": space_id, "qv": vec, "maxd": GLOSSARY_MAX_DISTANCE, "lim": limit},
                )
            ).all()
        )
        if not ids:
            return []
        rows = {g.id: g for g in await session.scalars(select(GlossaryTerm).where(GlossaryTerm.id.in_(ids)))}
    return [_entry(rows[i]) for i in ids if i in rows]


async def profiles(group_id: int, user_ids: Sequence[int]) -> dict[int, ProfileView]:
    """Profiles of the given users (missing and opted-out users are absent)."""
    if not user_ids:
        return {}
    async with get_session() as session:
        rows = (
            await session.scalars(
                select(UserProfile).where(
                    UserProfile.group_id == group_id,
                    UserProfile.user_id.in_(set(user_ids)),
                    UserProfile.opted_out.is_(False),
                )
            )
        ).all()
        if not rows:
            return {}
        ranked = (
            select(
                UserProfileFact.user_id,
                UserProfileFact.fact,
                func.row_number()
                .over(partition_by=UserProfileFact.user_id, order_by=UserProfileFact.id.desc())
                .label("rn"),
            )
            .where(UserProfileFact.group_id == group_id, UserProfileFact.user_id.in_([p.user_id for p in rows]))
            .subquery()
        )
        facts: dict[int, list[str]] = {}
        for uid, fact in (
            await session.execute(
                select(ranked.c.user_id, ranked.c.fact).where(ranked.c.rn <= PROFILE_FACTS).order_by(ranked.c.rn)
            )
        ).all():
            facts.setdefault(uid, []).append(fact)
    return {
        p.user_id: ProfileView(
            p.user_id,
            p.display_name,
            p.summary,
            list(p.interests),
            list(p.traits),
            facts.get(p.user_id, []),
            p.updated_at,
        )
        for p in rows
    }


async def get_profile(group_id: int, user_id: int) -> ProfileView | None:
    """None when the user has no profile or opted out."""
    return (await profiles(group_id, [user_id])).get(user_id)
