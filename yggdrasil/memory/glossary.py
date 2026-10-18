"""Group slang glossary: statistical candidate mining + LLM definitions (task ``memory_glossary``).

Mining (daily) tokenizes new messages with jieba and keeps words outside jieba's
dictionary and token n-grams whose message frequency spikes against the group's own
history. The definition step sends the top candidates with example messages to the
model; accepted terms are upserted into ``glossary_terms`` (``locked`` rows are never
overwritten).
"""

import re
import warnings
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import GlossaryCandidate, GlossaryTerm, Message
from yggdrasil.db.session import get_session
from yggdrasil.memory import llm
from yggdrasil.memory.common import format_line, people_for, pipeline_state
from yggdrasil.memory.search import VIEW_COLUMNS, MessageView
from yggdrasil.memory.vectors import delete_vectors, glossary_text, try_embed
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot
from yggdrasil.settings import MemorySettings

PIPELINE = "glossary"
TASK = "memory_glossary"
MINE_BATCH = 5000
MINE_MIN_MESSAGES = 50
HISTORY_SAMPLE = 5000
SPIKE_RATIO = 3.0
UPSERT_LIMIT = 200
EXAMPLES_PER_TERM = 5
PROMPT_EXAMPLES = 3
STALE_AFTER = timedelta(days=30)
MAX_EXISTING_IN_PROMPT = 200

_PLACEHOLDER = re.compile(r"\[[^\[\]\n]{0,60}\]")
_MENTION = re.compile(r"@\S+")
_URL = re.compile(r"https?://\S+")
_WORDLIKE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9]+$")
_LAUGH = re.compile(r"^(?:[哈呵嘿嘻啊哦嗯草xX2-9hHwW])+$")
_ASCII = re.compile(r"^[A-Za-z0-9]+$")

STOPWORDS = frozenset(
    """的 了 是 在 我 你 他 她 它 我们 你们 他们 她们 这 那 这个 那个 这样 那样 什么 怎么 为什么 吗 呢 吧 啊 呀 哦 嗯
    哈 和 与 就 都 也 还 又 很 太 没 没有 不 不是 有 要 会 能 可以 一个 一下 一样 已经 然后 但是 所以 因为 如果 就是
    还是 或者 而且 其实 感觉 觉得 知道 应该 可能 现在 今天 明天 昨天 时候 自己 大家 东西 真的 好像 这么 那么 怎么样
    the a an to of and or is are be in on at it this that for with you me my we ok OK""".split()
)
_STOP_CHARS = frozenset("的了是在吗呢吧啊呀哦嗯和就都也还又很")


@dataclass(slots=True)
class CandidateStat:
    term: str
    count: int  # number of recent messages containing it
    examples: list[int] = field(default_factory=list)  # message ids
    oov: bool = False
    spike: float = 0.0


@cache
def _jieba() -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        import jieba
    jieba.setLogLevel(30)
    jieba.dt.check_initialized()
    return jieba


def clean(text: str) -> str:
    """Strip placeholders (``[图片]``), mentions and URLs before tokenizing."""
    return _URL.sub(" ", _MENTION.sub(" ", _PLACEHOLDER.sub(" ", text)))


def in_dictionary(word: str) -> bool:
    return _jieba().dt.FREQ.get(word, 0) > 0


def acceptable(term: str, exclude: Collection[str] = ()) -> bool:
    if not 2 <= len(term) <= 8 or term.isdigit() or len(set(term)) == 1:
        return False
    if _LAUGH.match(term) or term in STOPWORDS or term.lower() in exclude:
        return False
    if _ASCII.match(term) and len(term) <= 2:
        return False
    return term[0] not in _STOP_CHARS and term[-1] not in _STOP_CHARS


def _grams(tokens: list[str], exclude: Collection[str]) -> set[tuple[str, bool]]:
    """Unigrams outside the dictionary and 2–3 dictionary-word n-grams; ``(term, is_oov_unigram)``."""
    out: set[tuple[str, bool]] = set()
    for n in (1, 2, 3):
        for i in range(len(tokens) - n + 1):
            parts = tokens[i : i + n]
            if not all(_WORDLIKE.match(p) for p in parts):
                continue
            # Multi-token n-grams are built from multi-character dictionary words only (single characters
            # are mostly particles and verbs gluing phrases together); unknown tokens are candidates themselves.
            if n > 1 and (
                parts[0] in STOPWORDS
                or parts[-1] in STOPWORDS
                or any(len(p) < 2 or not in_dictionary(p) for p in parts)
            ):
                continue
            term = "".join(parts)
            if in_dictionary(term) or not acceptable(term, exclude):
                continue
            out.add((term, n == 1))
    return out


def mine_candidates(
    recent: Sequence[tuple[int, str]],
    history: Sequence[str],
    *,
    min_count: int,
    limit: int,
    exclude: Collection[str] = (),
    spike_ratio: float = SPIKE_RATIO,
) -> list[CandidateStat]:
    """Candidate terms from ``recent`` ``(msg_id, text)`` messages against ``history`` texts.

    Counts are per message. Out-of-dictionary words qualify at ``min_count``; multi-token
    n-grams additionally need their message rate to be ``spike_ratio`` times the history rate.
    Terms contained in a longer candidate with at least 80% of their count are dropped.
    """
    if not recent:
        return []
    jieba = _jieba()
    excluded = {e.lower() for e in exclude}
    stats: dict[str, CandidateStat] = {}
    for msg_id, text in recent:
        for term, oov in _grams(jieba.lcut(clean(text)), excluded):
            st = stats.get(term)
            if st is None:
                st = stats[term] = CandidateStat(term, 0, oov=oov)
            st.count += 1
            if len(st.examples) < EXAMPLES_PER_TERM:
                st.examples.append(msg_id)
    pool = [s for s in stats.values() if s.count >= min_count]
    hist_n = max(len(history), 1)
    for s in pool:
        recent_rate = s.count / len(recent)
        hist_rate = sum(s.term in h for h in history) / hist_n
        s.spike = recent_rate / hist_rate if hist_rate else float("inf")
    kept = [s for s in pool if s.oov or s.spike >= spike_ratio]
    kept = [s for s in kept if not any(o is not s and s.term in o.term and o.count >= 0.8 * s.count for o in kept)]
    kept.sort(key=lambda s: (not s.oov, -s.count, s.term))
    return kept[:limit]


async def _excluded_terms(group_id: int, names: Collection[str]) -> set[str]:
    snap = snapshot()
    persona = snap.settings(group_id).persona
    out = {n.lower() for n in (*names, persona.name, *persona.aliases) if n}
    async with get_session() as session:
        for term, aliases in (
            await session.execute(
                select(GlossaryTerm.term, GlossaryTerm.aliases).where(GlossaryTerm.group_id == group_id)
            )
        ).all():
            out.add(term.lower())
            out.update(a.lower() for a in aliases)
        decided = await session.scalars(
            select(GlossaryCandidate.term).where(
                GlossaryCandidate.group_id == group_id, GlossaryCandidate.status != "pending"
            )
        )
        out.update(t.lower() for t in decided)
    return out


async def mine_group(group_id: int, *, now: datetime | None = None) -> int:
    """Mine candidates from messages after the watermark, batch after batch until caught up
    (a group can outgrow one batch a day). Returns candidates upserted."""
    mem = snapshot().settings(group_id).memory
    now = now or datetime.now(UTC)
    upserted = 0
    while (batch := await _mine_batch(group_id, mem, now)) is not None:
        upserted += batch[0]
        if not batch[1]:
            break
    return upserted


async def _mine_batch(group_id: int, mem: MemorySettings, now: datetime) -> tuple[int, bool] | None:
    """One batch after the watermark: (candidates upserted, batch was full). None when fewer than
    ``MINE_MIN_MESSAGES`` messages wait; they stay for a later run."""
    async with get_session() as session:
        st = await pipeline_state(session, PIPELINE, group_id)
        watermark = st.last_msg_id
        visible = (Message.group_id == group_id, Message.recalled.is_(False), Message.is_self.is_(False))
        rows = await session.execute(
            select(Message.id, Message.sent_at, Message.sender_name, Message.plain_text)
            .where(*visible, Message.id > watermark)
            .order_by(Message.id)
            .limit(MINE_BATCH)
        )
        loaded = rows.all()
        recent = [r for r in loaded if r.plain_text.strip()]
        if len(recent) < MINE_MIN_MESSAGES:
            return None
        history = list(
            (
                await session.scalars(
                    select(Message.plain_text)
                    .where(*visible, Message.id <= watermark)
                    .order_by(Message.sent_at.desc())
                    .limit(HISTORY_SAMPLE)
                )
            ).all()
        )
    exclude = await _excluded_terms(group_id, {r.sender_name for r in recent})
    sent_at = {r.id: r.sent_at for r in recent}
    found = mine_candidates(
        [(r.id, r.plain_text) for r in recent],
        [clean(h) for h in history],
        min_count=max(2, mem.glossary_min_count // 2),
        limit=UPSERT_LIMIT,
        exclude=exclude,
    )
    async with get_session() as session:
        for c in found:
            seen = [sent_at[i] for i in c.examples]
            stmt = pg_insert(GlossaryCandidate).values(
                group_id=group_id,
                term=c.term,
                count=c.count,
                first_seen=min(seen),
                last_seen=max(seen),
                status="pending",
                example_msg_ids=c.examples,
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["group_id", "term"],
                    set_={
                        "count": GlossaryCandidate.count + stmt.excluded.count,
                        "last_seen": stmt.excluded.last_seen,
                        "example_msg_ids": stmt.excluded.example_msg_ids,
                        "updated_at": func.now(),
                    },
                    where=GlossaryCandidate.status == "pending",
                )
            )
        # Candidates that never reached the threshold fade out.
        await session.execute(
            delete(GlossaryCandidate).where(
                GlossaryCandidate.group_id == group_id,
                GlossaryCandidate.status == "pending",
                GlossaryCandidate.count < mem.glossary_min_count,
                GlossaryCandidate.last_seen < now - STALE_AFTER,
            )
        )
        st = await pipeline_state(session, PIPELINE, group_id)
        st.last_msg_id = loaded[-1].id
    return len(found), len(loaded) == MINE_BATCH


class TermDecision(BaseModel):
    term: str
    accept: bool
    definition: str = ""
    aliases: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class GlossaryDecisions(BaseModel):
    decisions: list[TermDecision]


def valid_decisions(decisions: Sequence[TermDecision], candidates: Collection[str]) -> list[TermDecision]:
    """Keep one decision per known candidate; accepted ones need a definition."""
    known, seen = set(candidates), set()
    out = []
    for d in decisions:
        term = d.term.strip()
        if term not in known or term in seen or (d.accept and not d.definition.strip()):
            continue
        seen.add(term)
        aliases = [a.strip() for a in dict.fromkeys(d.aliases) if a.strip() and a.strip() != term][:5]
        out.append(d.model_copy(update={"term": term, "aliases": aliases, "definition": d.definition.strip()}))
    return out


async def define_group(group_id: int) -> dict[str, int]:
    """Ask the model about the top pending candidates; upsert accepted terms."""
    mem = snapshot().settings(group_id).memory
    stats = {"accepted": 0, "rejected": 0, "skipped_locked": 0}
    if not llm.available(TASK):
        LOGGER.debug("group {}: task {} not configured", group_id, TASK)
        return stats
    async with get_session() as session:
        cands = (
            await session.scalars(
                select(GlossaryCandidate)
                .where(
                    GlossaryCandidate.group_id == group_id,
                    GlossaryCandidate.status == "pending",
                    GlossaryCandidate.count >= mem.glossary_min_count,
                )
                .order_by(GlossaryCandidate.count.desc())
                .limit(mem.glossary_max_candidates)
            )
        ).all()
        if not cands:
            return stats
        ids = {i for c in cands for i in c.example_msg_ids[:PROMPT_EXAMPLES]}
        examples = {
            r[0]: MessageView(*r)
            for r in await session.execute(
                select(*VIEW_COLUMNS).where(
                    Message.group_id == group_id, Message.id.in_(ids), Message.recalled.is_(False)
                )
            )
        }
        existing = (
            await session.execute(
                select(GlossaryTerm.term, GlossaryTerm.definition)
                .where(GlossaryTerm.group_id == group_id)
                .order_by(GlossaryTerm.hits.desc(), GlossaryTerm.id)
                .limit(MAX_EXISTING_IN_PROMPT)
            )
        ).all()
    bot_name = snapshot().settings(group_id).persona.name
    people = await people_for(group_id, examples.values(), bot_name=bot_name)
    prompt = render(
        TASK,
        bot_name=bot_name,
        candidates=[
            {
                "term": llm.fence(c.term),
                "count": c.count,
                "examples": [
                    llm.fence(format_line(examples[i], people)[:300])
                    for i in c.example_msg_ids[:PROMPT_EXAMPLES]
                    if i in examples
                ],
            }
            for c in cands
        ],
        existing_terms=[{"term": llm.fence(t), "definition": llm.fence(d[:80])} for t, d in existing],
    )
    result = await llm.run_structured(TASK, GlossaryDecisions, prompt)
    by_term = {c.term: c for c in cands}
    decisions = valid_decisions(result.decisions, by_term.keys())
    embed: list[tuple[int, int, str]] = []
    async with get_session() as session:
        for d in decisions:
            cand = by_term[d.term]
            await session.execute(
                update(GlossaryCandidate)
                .where(GlossaryCandidate.group_id == group_id, GlossaryCandidate.term == d.term)
                .values(status="accepted" if d.accept else "rejected")
            )
            if not d.accept:
                stats["rejected"] += 1
                continue
            row = await session.scalar(
                select(GlossaryTerm).where(GlossaryTerm.group_id == group_id, GlossaryTerm.term == d.term)
            )
            if row is not None and row.locked:
                stats["skipped_locked"] += 1
                continue
            sample = [
                {"msg_id": i, "text": examples[i].text[:200]}
                for i in cand.example_msg_ids[:PROMPT_EXAMPLES]
                if i in examples
            ]
            if row is None:
                row = GlossaryTerm(group_id=group_id, term=d.term, first_seen=cand.first_seen, source="auto")
                session.add(row)
            row.definition, row.aliases, row.confidence = d.definition, d.aliases, d.confidence
            row.examples, row.last_seen = sample, cand.last_seen
            await session.flush()
            await delete_vectors(session, "glossary", [row.id])
            embed.append((row.id, group_id, glossary_text(row.term, row.aliases, row.definition)))
            stats["accepted"] += 1
    await try_embed("glossary", embed)
    return stats
