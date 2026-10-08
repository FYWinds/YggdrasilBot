"""Memory browser: topics, daily digests, glossary, user profiles, pipeline watermarks."""

from datetime import date, datetime
from typing import Any

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


@router.get("/topics")
async def list_topics(
    group_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[TopicOut]:
    cond = []
    if group_id is not None:
        cond.append(Topic.group_id == group_id)
    if since is not None:
        cond.append(Topic.end_at >= since)
    if until is not None:
        cond.append(Topic.start_at <= until)
    async with get_session() as s:
        total = await s.scalar(select(func.count()).select_from(Topic).where(*cond)) or 0
        rows = await s.scalars(select(Topic).where(*cond).order_by(Topic.start_at.desc()).limit(limit).offset(offset))
        items = [
            TopicOut(
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
            )
            for t in rows.all()
        ]
    return Page[TopicOut](items=items, total=total)


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


class ProfileDetail(ProfileOut):
    facts: list[FactOut]


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
            .order_by(UserProfileFact.id.desc())
        )
        fact_items = [
            FactOut(id=f.id, fact=f.fact, source_msg_ids=list(f.source_msg_ids), created_at=f.created_at)
            for f in facts.all()
        ]
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
