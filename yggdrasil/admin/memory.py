"""Memory browser: topics, daily digests, glossary, user profiles, pipeline watermarks."""

from datetime import UTC, date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Ok, Page, change, conflict, not_found
from yggdrasil.db.models import (
    DailyDigest,
    GlossaryTerm,
    MemoryVector,
    Message,
    PipelineState,
    Topic,
    UserProfile,
    UserProfileFact,
)
from yggdrasil.db.session import get_session
from yggdrasil.memory import topics

router = APIRouter(prefix="/memory", tags=["memory"])


class TopicOut(BaseModel):
    id: int
    group_id: int
    title: str
    summary: str
    keywords: list[str]
    participants: list[int]
    start_at: datetime
    end_at: datetime
    msg_count: int
    status: str
    dirty: bool
    importance: int | None = Field(description="整理任务给的重要度 1–5；未评分为空")
    importance_reason: str | None
    weight: float | None = Field(description="重要度 × 规模系数（消息数、参与人数）；未评分为空")


def _topic_out(t: Topic) -> TopicOut:
    return TopicOut(
        id=t.id,
        group_id=t.group_id,
        title=t.title,
        summary=t.summary,
        keywords=list(t.keywords),
        participants=list(t.participants),
        start_at=t.start_at,
        end_at=t.end_at,
        msg_count=t.msg_count,
        status=t.status,
        dirty=t.dirty,
        importance=t.importance,
        importance_reason=t.importance_reason,
        weight=t.weight,
    )


@router.get("/topics")
async def list_topics(
    group_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    status: Literal["open", "closed", "archived"] | None = None,
    q: str | None = None,
    user_id: int | None = None,
    min_messages: int | None = Query(None, ge=0),
    max_messages: int | None = Query(None, ge=0),
    min_participants: int | None = Query(None, ge=0),
    max_participants: int | None = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[TopicOut]:
    cond: list[ColumnElement[bool]] = []
    if group_id is not None:
        cond.append(Topic.group_id == group_id)
    if since is not None:
        cond.append(Topic.end_at >= since)
    if until is not None:
        cond.append(Topic.start_at <= until)
    if status is not None:
        cond.append(Topic.status == status)
    if q:
        like = f"%{q}%"
        cond.append(
            or_(
                Topic.title.ilike(like),
                Topic.summary.ilike(like),
                func.array_to_string(Topic.keywords, " ").ilike(like),
            )
        )
    if user_id is not None:
        cond.append(Topic.participants.contains([user_id]))
    ranges = (
        (Topic.msg_count, min_messages, max_messages),
        (func.cardinality(Topic.participants), min_participants, max_participants),
    )
    for column, low, high in ranges:
        if low is not None:
            cond.append(column >= low)
        if high is not None:
            cond.append(column <= high)
    async with get_session() as s:
        total = await s.scalar(select(func.count()).select_from(Topic).where(*cond)) or 0
        rows = await s.scalars(select(Topic).where(*cond).order_by(Topic.start_at.desc()).limit(limit).offset(offset))
        items = [_topic_out(t) for t in rows.all()]
    return Page[TopicOut](items=items, total=total)


class ArchiveIn(BaseModel):
    archived: bool


@router.post("/topics/{topic_id}/archive")
async def archive_topic(topic_id: int, body: ArchiveIn, actor: Actor) -> TopicOut:
    """Archive (hide from the agent) or restore one finished topic."""
    async with change(actor, "topic", "archive" if body.archived else "restore", topic_id) as c:
        t = await c.session.get(Topic, topic_id)
        if t is None:
            raise not_found("话题")
        if t.status == "open":
            raise conflict("进行中的话题不能归档，等它结束")
        if body.archived:
            t.status = "archived"
        elif t.status == "archived":
            # Profiles skip archived topics; a restored one is new to them (see Topic.closed_at). The restore
            # also counts as its curation review, so the hourly curation won't archive it again.
            t.status, t.closed_at = "closed", datetime.now(UTC)
            t.scored_at = t.scored_at or datetime.now(UTC)
        await c.session.flush()
        await c.session.refresh(t)
    return _topic_out(t)


class ArchiveSmallIn(BaseModel):
    group_id: int


class ArchiveSmallOut(BaseModel):
    archived: int


@router.post("/topics/archive-small")
async def archive_small_topics(body: ArchiveSmallIn, actor: Actor) -> ArchiveSmallOut:
    """Apply the group's current archive thresholds (size and curation weight) to its finished topics."""
    async with change(actor, "topic", "archive_small", body.group_id) as c:
        n = await topics.archive_closed(c.session, body.group_id)
        c.detail = {"group_id": body.group_id, "archived": n}
    return ArchiveSmallOut(archived=n)


class DigestOut(BaseModel):
    id: int
    group_id: int
    day: date
    summary: str
    topic_ids: list[int]


@router.get("/digests")
async def list_digests(
    group_id: int | None = None,
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[DigestOut]:
    cond = [DailyDigest.group_id == group_id] if group_id is not None else []
    async with get_session() as s:
        total = await s.scalar(select(func.count()).select_from(DailyDigest).where(*cond)) or 0
        rows = await s.scalars(
            select(DailyDigest).where(*cond).order_by(DailyDigest.day.desc()).limit(limit).offset(offset)
        )
        items = [
            DigestOut(id=d.id, group_id=d.group_id, day=d.day, summary=d.summary, topic_ids=list(d.topic_ids))
            for d in rows.all()
        ]
    return Page[DigestOut](items=items, total=total)


class GlossaryOut(BaseModel):
    id: int
    group_id: int
    term: str
    aliases: list[str]
    definition: str
    examples: list[Any]
    first_seen: datetime | None
    last_seen: datetime | None
    hits: int
    confidence: float
    locked: bool
    source: str
    updated_at: datetime


class GlossaryIn(BaseModel):
    group_id: int
    term: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    definition: str = Field(min_length=1)


class LockIn(BaseModel):
    locked: bool


def _glossary_out(g: GlossaryTerm) -> GlossaryOut:
    return GlossaryOut(
        id=g.id,
        group_id=g.group_id,
        term=g.term,
        aliases=list(g.aliases),
        definition=g.definition,
        examples=list(g.examples),
        first_seen=g.first_seen,
        last_seen=g.last_seen,
        hits=g.hits,
        confidence=g.confidence,
        locked=g.locked,
        source=g.source,
        updated_at=g.updated_at,
    )


def _drop_glossary_vectors(term_id: int) -> Any:
    # The memory pipeline re-embeds glossary rows that have no vector in the active space.
    return delete(MemoryVector).where(MemoryVector.kind == "glossary", MemoryVector.ref_id == term_id)


@router.get("/glossary")
async def list_glossary(
    group_id: int | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page[GlossaryOut]:
    cond = []
    if group_id is not None:
        cond.append(GlossaryTerm.group_id == group_id)
    if q:
        like = f"%{q}%"
        cond.append(or_(GlossaryTerm.term.ilike(like), GlossaryTerm.definition.ilike(like)))
    async with get_session() as s:
        total = await s.scalar(select(func.count()).select_from(GlossaryTerm).where(*cond)) or 0
        rows = await s.scalars(
            select(GlossaryTerm)
            .where(*cond)
            .order_by(GlossaryTerm.locked.desc(), GlossaryTerm.hits.desc(), GlossaryTerm.id)
            .limit(limit)
            .offset(offset)
        )
        items = [_glossary_out(g) for g in rows.all()]
    return Page[GlossaryOut](items=items, total=total)


@router.post("/glossary")
async def create_glossary_term(body: GlossaryIn, actor: Actor) -> GlossaryOut:
    try:
        async with change(actor, "glossary", "create") as c:
            g = GlossaryTerm(
                group_id=body.group_id,
                term=body.term.strip(),
                aliases=[a.strip() for a in body.aliases if a.strip()],
                definition=body.definition.strip(),
                examples=[],
                source="admin",
                locked=True,
                confidence=1.0,
            )
            c.session.add(g)
            await c.session.flush()
            await c.session.refresh(g)
            c.entity_id, c.detail = str(g.id), {"group_id": g.group_id, "term": g.term}
    except IntegrityError as e:
        raise conflict(f"群 {body.group_id} 已有词条 {body.term}") from e
    return _glossary_out(g)


@router.put("/glossary/{term_id}")
async def update_glossary_term(term_id: int, body: GlossaryIn, actor: Actor) -> GlossaryOut:
    try:
        async with change(actor, "glossary", "update", term_id) as c:
            g = await c.session.get(GlossaryTerm, term_id)
            if g is None:
                raise not_found("词条")
            c.detail = {"before": {"term": g.term, "definition": g.definition, "aliases": list(g.aliases)}}
            g.group_id = body.group_id
            g.term = body.term.strip()
            g.aliases = [a.strip() for a in body.aliases if a.strip()]
            g.definition = body.definition.strip()
            g.source = "admin"
            g.locked = True
            await c.session.execute(_drop_glossary_vectors(term_id))
            await c.session.flush()
            await c.session.refresh(g)
    except IntegrityError as e:
        raise conflict(f"群 {body.group_id} 已有词条 {body.term}") from e
    return _glossary_out(g)


@router.post("/glossary/{term_id}/lock")
async def lock_glossary_term(term_id: int, body: LockIn, actor: Actor) -> GlossaryOut:
    async with change(actor, "glossary", "lock" if body.locked else "unlock", term_id) as c:
        g = await c.session.get(GlossaryTerm, term_id)
        if g is None:
            raise not_found("词条")
        g.locked = body.locked
        await c.session.flush()
        await c.session.refresh(g)
    return _glossary_out(g)


@router.delete("/glossary/{term_id}")
async def delete_glossary_term(term_id: int, actor: Actor) -> Ok:
    async with change(actor, "glossary", "delete", term_id) as c:
        g = await c.session.get(GlossaryTerm, term_id)
        if g is None:
            raise not_found("词条")
        c.detail = {"group_id": g.group_id, "term": g.term, "definition": g.definition}
        await c.session.execute(_drop_glossary_vectors(term_id))
        await c.session.delete(g)
    return Ok()


class ProfileOut(BaseModel):
    group_id: int
    user_id: int
    display_name: str
    summary: str
    interests: list[str]
    traits: list[str]
    opted_out: bool
    updated_at: datetime


class FactOut(BaseModel):
    id: int
    fact: str
    source_msg_ids: list[int]
    created_at: datetime
    importance: int | None = Field(description="重要度 1–5（画像更新或整理任务给的）；未评分为空")
    weight: float | None = Field(description="重要度，佐证消息多的略高；排序时还会按时间衰减")
    archived_at: datetime | None = Field(description="归档时间；归档的事实 agent 和画像更新都看不到")
    archive_reason: str | None = Field(
        description="low 重要度低于阈值，duplicate 与另一条重复，cap 超出每人上限，outdated 被新消息推翻"
    )
    locked: bool = Field(description="管理员恢复过：整理任务不再归档它")


class ProfileDetail(ProfileOut):
    facts: list[FactOut]


def _fact_out(f: UserProfileFact) -> FactOut:
    return FactOut(
        id=f.id,
        fact=f.fact,
        source_msg_ids=list(f.source_msg_ids),
        created_at=f.created_at,
        importance=f.importance,
        weight=f.weight,
        archived_at=f.archived_at,
        archive_reason=f.archive_reason,
        locked=f.locked,
    )


def _profile_out(p: UserProfile) -> ProfileOut:
    return ProfileOut(
        group_id=p.group_id,
        user_id=p.user_id,
        display_name=p.display_name,
        summary=p.summary,
        interests=list(p.interests),
        traits=list(p.traits),
        opted_out=p.opted_out,
        updated_at=p.updated_at,
    )


@router.get("/profiles")
async def list_profiles(
    group_id: int | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[ProfileOut]:
    cond = []
    if group_id is not None:
        cond.append(UserProfile.group_id == group_id)
    if q:
        conds: list[ColumnElement[bool]] = [
            UserProfile.display_name.ilike(f"%{q}%"),
            UserProfile.summary.ilike(f"%{q}%"),
        ]
        if q.isdigit():
            conds.append(UserProfile.user_id == int(q))
        cond.append(or_(*conds))
    async with get_session() as s:
        total = await s.scalar(select(func.count()).select_from(UserProfile).where(*cond)) or 0
        rows = await s.scalars(
            select(UserProfile).where(*cond).order_by(UserProfile.updated_at.desc()).limit(limit).offset(offset)
        )
        items = [_profile_out(p) for p in rows.all()]
    return Page[ProfileOut](items=items, total=total)


@router.get("/profiles/{group_id}/{user_id}")
async def get_profile(group_id: int, user_id: int) -> ProfileDetail:
    async with get_session() as s:
        p = await s.get(UserProfile, (group_id, user_id))
        if p is None:
            raise not_found("画像")
        facts = await s.scalars(
            select(UserProfileFact)
            .where(UserProfileFact.group_id == group_id, UserProfileFact.user_id == user_id)
            .order_by(
                UserProfileFact.archived_at.is_not(None),
                UserProfileFact.weight.desc().nulls_last(),
                UserProfileFact.id.desc(),
            )
        )
        fact_items = [_fact_out(f) for f in facts.all()]
    return ProfileDetail(**_profile_out(p).model_dump(), facts=fact_items)


@router.delete("/profiles/{group_id}/{user_id}")
async def delete_profile(group_id: int, user_id: int, actor: Actor) -> Ok:
    async with change(actor, "profile", "delete", f"{group_id}:{user_id}") as c:
        p = await c.session.get(UserProfile, (group_id, user_id))
        facts = await c.session.execute(
            delete(UserProfileFact).where(UserProfileFact.group_id == group_id, UserProfileFact.user_id == user_id)
        )
        if p is None and not facts.rowcount:  # pyright: ignore[reportAttributeAccessIssue]
            raise not_found("画像")
        if p is not None:
            await c.session.delete(p)
    return Ok()


@router.delete("/profiles/{group_id}/{user_id}/facts/{fact_id}")
async def delete_profile_fact(group_id: int, user_id: int, fact_id: int, actor: Actor) -> Ok:
    async with change(actor, "profile_fact", "delete", fact_id) as c:
        f = await c.session.get(UserProfileFact, fact_id)
        if f is None or f.group_id != group_id or f.user_id != user_id:
            raise not_found("事实")
        c.detail = {"group_id": group_id, "fact": f.fact}
        await c.session.delete(f)
    return Ok()


@router.post("/profiles/{group_id}/{user_id}/facts/{fact_id}/restore")
async def restore_profile_fact(group_id: int, user_id: int, fact_id: int, actor: Actor) -> FactOut:
    """Bring an archived fact back; it is locked, so curation never archives it again."""
    async with change(actor, "profile_fact", "restore", fact_id) as c:
        f = await c.session.get(UserProfileFact, fact_id)
        if f is None or f.group_id != group_id or f.user_id != user_id:
            raise not_found("事实")
        if f.archived_at is None:
            raise conflict("这条事实没有归档")
        c.detail = {"group_id": group_id, "fact": f.fact, "archive_reason": f.archive_reason}
        f.archived_at, f.archive_reason, f.locked = None, None, True
        await c.session.flush()
        await c.session.refresh(f)
    return _fact_out(f)


class PipelineOut(BaseModel):
    pipeline: str
    group_id: int
    last_msg_id: int
    backlog: int = Field(description="水位之后的新消息数（上限 10000）")
    state: dict[str, Any]
    updated_at: datetime


@router.get("/pipelines")
async def list_pipelines() -> list[PipelineOut]:
    async with get_session() as s:
        rows = (await s.scalars(select(PipelineState).order_by(PipelineState.pipeline, PipelineState.group_id))).all()
        out = []
        for p in rows:
            capped = (
                select(Message.id)
                .where(Message.group_id == p.group_id, Message.id > p.last_msg_id)
                .limit(10000)
                .subquery()
            )
            backlog = await s.scalar(select(func.count()).select_from(capped)) or 0
            out.append(
                PipelineOut(
                    pipeline=p.pipeline,
                    group_id=p.group_id,
                    last_msg_id=p.last_msg_id,
                    backlog=backlog,
                    state=dict(p.state),
                    updated_at=p.updated_at,
                )
            )
    return out
