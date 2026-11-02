"""Rows for the other tables: groups and members, stickers, memory, bot events and review items."""

import io
from datetime import timedelta
from typing import Any
from uuid import UUID

from PIL import Image
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

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
    Sticker,
    StickerBlob,
    StickerUsage,
    TaskRoute,
    Topic,
    TopicMessage,
    UserAlias,
    UserProfile,
    UserProfileFact,
)
from yggdrasil.db.session import get_session
from yggdrasil.devtools.seed.chat import Chat, format_lines
from yggdrasil.devtools.seed.data import (
    ADMIN,
    FAST,
    GAME,
    GROUPS,
    MEDIA,
    MEMBERS,
    MODEL,
    NOW,
    PROVIDER,
    SUPERUSER,
    TECH,
    broken_ref,
    mface_segment,
)
from yggdrasil.devtools.seed.scenarios import Verdict, bot_reply, verdict_record
from yggdrasil.memory.common import fact_weight, topic_weight
from yggdrasil.memory.digest import DigestDraft, DigestEvent, render_digest


async def add_base() -> None:
    async with get_session() as s:
        s.add_all(Group(group_id=g, name=name, enabled=True) for g, name in GROUPS.items())
        for g in GROUPS:
            s.add_all(
                UserAlias(group_id=g, user_id=u, name=name, first_seen=NOW - timedelta(days=60), last_seen=NOW)
                for u, name in MEMBERS.items()
            )
        s.add(
            UserAlias(
                group_id=TECH,
                user_id=20003,
                name="王工",
                first_seen=NOW - timedelta(days=90),
                last_seen=NOW - timedelta(days=30),
            )
        )
        provider = Provider(name=PROVIDER, base_url="https://api.example.com/v1", models=[MODEL, FAST])
        s.add(provider)
        await s.flush()
        for task, model in (("chat", MODEL), ("tldr", MODEL), ("ops", FAST), ("quick_ack", FAST), ("vision", MODEL)):
            await s.execute(
                pg_insert(TaskRoute)
                .values(task=task, provider_id=provider.id, model=model)
                .on_conflict_do_nothing(index_elements=["task"])
            )


async def add_stickers() -> None:
    specs: list[dict[str, Any]] = [
        {
            "name": "doge",
            "status": "ready",
            "frames": 3,
            "description": "一只眯眼笑的狗头",
            "usage": "调侃、表示「懂的都懂」",
            "emotions": ["调侃", "得意"],
            "usage_by_group": {TECH: 12},
        },
        {"name": "cat", "status": "untagged", "frames": 1, "usage_by_group": {GAME: 3}},
    ]
    async with get_session() as s:
        for spec in specs:
            media = MEDIA[spec["name"]]
            img = Image.open(io.BytesIO(media.data))
            sticker = Sticker(
                kind="image",
                ref=media.md5,
                summary="动画表情",
                status=spec["status"],
                mime=media.mime,
                size_bytes=len(media.data),
                width=img.width,
                height=img.height,
                frames=spec["frames"],
                description=spec.get("description", ""),
                usage=spec.get("usage", ""),
                emotions=spec.get("emotions", []),
                tagged_at=NOW - timedelta(hours=2) if spec["status"] == "ready" else None,
                seen_count=sum(spec["usage_by_group"].values()),
            )
            s.add(sticker)
            await s.flush()
            s.add(StickerBlob(sticker_id=sticker.id, data=media.data))
            s.add_all(
                StickerUsage(sticker_id=sticker.id, group_id=g, count=n, last_seen=NOW)
                for g, n in spec["usage_by_group"].items()
            )
        broken = Sticker(kind="image", ref=broken_ref(), summary="动画表情", status="failed", error="too_large")
        mface = mface_segment()["data"]
        happy = Sticker(
            kind="mface",
            ref=f"{mface['emoji_package_id']}:{mface['emoji_id']}",
            emoji_id=mface["emoji_id"],
            package_id=mface["emoji_package_id"],
            key=mface["key"],
            summary="开心",
            seen_count=5,
        )
        s.add_all([broken, happy])
        await s.flush()
        s.add(StickerUsage(sticker_id=happy.id, group_id=TECH, count=5, last_seen=NOW))


async def add_memory(chats: dict[int, Chat]) -> None:
    tech, game = chats[TECH].keys, chats[GAME].keys

    def ids(keys: dict[str, Message], *names: str) -> list[int]:
        return [keys[n].id for n in names]

    async with get_session() as s:
        closed = Topic(
            group_id=TECH,
            title="Python 3.14 free-threading 性能",
            summary="老王贴了 benchmark：单线程慢 5%，8 线程快 5 倍；Neko 确认是关掉 GIL 跑的。",
            keywords=["free-threading", "GIL", "benchmark"],
            participants=[20001, 20002, 20003, 20004],
            start_at=tech["hello"].sent_at,
            end_at=tech["gil_ans"].sent_at,
            first_msg_id=tech["hello"].id,
            last_msg_id=tech["gil_ans"].id,
            msg_count=6,
            status="closed",
            closed_at=tech["gil_ans"].sent_at + timedelta(hours=1),
            importance=4,
            importance_reason="有实测数据和结论",
            weight=topic_weight(4, 6, 4),
            scored_at=tech["gil_ans"].sent_at + timedelta(hours=2),
        )
        # Scored low by curation and archived: hidden from the agent, restorable in the console.
        banter = Topic(
            group_id=TECH,
            title="掷骰子和猜拳",
            summary="路人甲掷了个骰子，阿明出了石头剪刀布，小红戳了一下。",
            keywords=["骰子", "猜拳"],
            participants=[20005, 20001, 20002],
            start_at=tech["poke"].sent_at,
            end_at=tech["rps"].sent_at,
            first_msg_id=tech["poke"].id,
            last_msg_id=tech["rps"].id,
            msg_count=3,
            status="archived",
            closed_at=tech["rps"].sent_at + timedelta(hours=2),
            importance=1,
            importance_reason="只有互动表情，没有内容",
            weight=topic_weight(1, 3, 3),
            scored_at=tech["rps"].sent_at + timedelta(hours=3),
        )
        meetup = Topic(
            group_id=TECH,
            title="周末线下聚会",
            summary="聚会改到周日下午，地点不变；老王会带「鹈鹕」（他的旧服务器）。",
            keywords=["聚会", "周日"],
            participants=[20001, 20002, 20003, 20005],
            start_at=tech["meetup"].sent_at,
            end_at=tech["pelican2"].sent_at,
            first_msg_id=tech["meetup"].id,
            last_msg_id=tech["pelican2"].id,
            msg_count=4,
        )
        season = Topic(
            group_id=GAME,
            title="第七赛季开荒",
            summary="Neko 招人今晚一起打新赛季，阿明贴了更新说明。",
            keywords=["新赛季", "更新"],
            participants=[20001, 20004, 20005],
            start_at=game["g_hi"].sent_at,
            end_at=game["g_long"].sent_at,
            first_msg_id=game["g_hi"].id,
            last_msg_id=game["g_long"].id,
            msg_count=4,
        )
        s.add_all([closed, banter, meetup, season])
        await s.flush()
        members = {
            closed.id: ids(tech, "hello", "ft", "bench", "bench_note", "ask_gil", "gil_ans"),
            banter.id: ids(tech, "poke", "dice", "rps"),
            meetup.id: ids(tech, "meetup", "meetup2", "pelican", "pelican2"),
            season.id: ids(game, "g_hi", "g_me", "g_shot", "g_long"),
        }
        s.add_all(TopicMessage(topic_id=t, message_id=m) for t, ms in members.items() for m in ms)
        day = (NOW - timedelta(days=1)).astimezone().date()
        draft = DigestDraft(
            overview="主要聊 Python 3.14 的 free-threading 构建，顺便约了周末聚会",
            events=[
                DigestEvent(
                    title="free-threading 实测",
                    topic_ids=[closed.id],
                    participants=[20003, 20004, 20002],
                    what="老王贴了 benchmark：单线程慢 5% 左右，8 线程快 5 倍；Neko 确认多线程是关掉 GIL 跑的",
                    conclusion="C 扩展还要等适配",
                ),
                DigestEvent(
                    title="周末聚会改期",
                    topic_ids=[meetup.id],
                    participants=[20001, 20003],
                    what="阿明通知聚会改到周日下午，地点不变；老王会带他的旧服务器「鹈鹕」",
                ),
            ],
        )
        topics = {closed.id: closed, meetup.id: meetup}
        s.add(
            DailyDigest(
                group_id=TECH,
                day=day,
                summary=render_digest(day, len(chats[TECH].rows), draft, topics, MEMBERS) or "",
                topic_ids=list(topics),
            )
        )
        s.add_all(
            [
                GlossaryTerm(
                    group_id=TECH,
                    term="鹈鹕",
                    definition="老王那台旧服务器的名字",
                    examples=[{"msg_id": tech["pelican2"].id, "text": "鹈鹕是老王给他那台旧服务器起的名字"}],
                    first_seen=tech["meetup2"].sent_at,
                    last_seen=tech["pelican2"].sent_at,
                    hits=3,
                    confidence=0.8,
                ),
                GlossaryTerm(
                    group_id=TECH,
                    term="3.14t",
                    aliases=["free-threaded 构建"],
                    definition="Python 3.14 去掉 GIL 的构建",
                    hits=5,
                    confidence=1.0,
                    locked=True,
                    source="admin",
                ),
                GlossaryCandidate(
                    group_id=TECH,
                    term="开摆",
                    count=4,
                    first_seen=NOW - timedelta(days=3),
                    last_seen=NOW - timedelta(hours=5),
                    example_msg_ids=ids(tech, "ft"),
                ),
                GlossaryCandidate(
                    group_id=GAME,
                    term="潮汐港",
                    count=2,
                    first_seen=NOW - timedelta(hours=2),
                    last_seen=NOW - timedelta(hours=2),
                    status="rejected",
                ),
            ]
        )
        scored = NOW - timedelta(hours=1)

        def fact(
            user_id: int,
            text: str,
            keys: list[str],
            importance: int,
            archived: str | None = None,
            *,
            locked: bool = False,
        ) -> UserProfileFact:
            sources = ids(tech, *keys)
            return UserProfileFact(
                group_id=TECH,
                user_id=user_id,
                fact=text,
                source_msg_ids=sources,
                importance=importance,
                weight=fact_weight(importance, len(sources)),
                scored_at=scored,
                archived_at=scored if archived else None,
                archive_reason=archived,
                locked=locked,
            )

        s.add_all(
            [
                UserProfile(
                    group_id=TECH,
                    user_id=20003,
                    display_name="老王",
                    summary="后端工程师，喜欢跑 benchmark，有台叫「鹈鹕」的旧服务器",
                    interests=["Python 性能", "自建服务器"],
                    traits=["较真", "爱分享数据"],
                    last_closed_at=closed.closed_at,
                    last_topic_id=closed.id,
                ),
                UserProfile(
                    group_id=TECH,
                    user_id=20001,
                    display_name="阿明",
                    summary="群主，负责组织线下聚会",
                    interests=["Python", "聚会"],
                    traits=["热心"],
                ),
                UserProfile(group_id=GAME, user_id=20004, display_name="Neko", opted_out=True),
                # Curation merged a duplicate into the first fact (sources unioned) and archived a one-off plan.
                fact(20003, "在 3.14t 上跑过多线程 benchmark", ["bench_note", "bench"], 3),
                fact(20003, "有台叫「鹈鹕」的旧服务器", ["meetup2"], 4),
                fact(20003, "跑过 free-threading 的 benchmark", ["bench"], 3, "duplicate"),
                fact(20003, "周日下午要去线下聚会", ["meetup2"], 1, "low"),
                fact(20001, "群主，负责组织线下聚会", ["meetup"], 5),
                # Archived for low importance, then restored by an admin: locked, curation keeps it.
                fact(20001, "关注 Python 新版本", ["hello"], 2, locked=True),
            ]
        )
        s.add(
            MessageChunk(
                group_id=TECH,
                first_msg_id=tech["hello"].id,
                last_msg_id=tech["gil_ans"].id,
                start_at=tech["hello"].sent_at,
                end_at=tech["gil_ans"].sent_at,
                msg_count=6,
                text=format_lines([m for m in chats[TECH].rows if m.id in members[closed.id]]),
            )
        )


async def add_events() -> None:
    def ev(kind: str, at: float, **kw: Any) -> BotEvent:
        return BotEvent(kind=kind, at=NOW - timedelta(minutes=at), **kw)

    async with get_session() as s:
        s.add_all(
            [
                ev(
                    "request.group.invite",
                    90,
                    group_id=10009,
                    user_id=20002,
                    data={"request_type": "group", "sub_type": "invite", "comment": "", "flag": "seed-flag-1"},
                    status="pending",
                ),
                ev(
                    "request.group.add",
                    60,
                    group_id=TECH,
                    user_id=20006,
                    data={"request_type": "group", "sub_type": "add", "comment": "朋友推荐来的", "flag": "seed-flag-2"},
                    status="approved",
                    handled_by=f"qq:{SUPERUSER}",
                    handled_at=NOW - timedelta(minutes=58),
                ),
                ev(
                    "request.friend",
                    300,
                    user_id=20005,
                    data={"request_type": "friend", "comment": "加个好友", "flag": "seed-flag-3"},
                    status="expired",
                ),
                ev(
                    "notice.group_ban.ban",
                    45,
                    group_id=TECH,
                    user_id=20005,
                    operator_id=20003,
                    data={"notice_type": "group_ban", "sub_type": "ban", "duration": 600},
                ),
                ev(
                    "notice.group_decrease.kick",
                    200,
                    group_id=GAME,
                    user_id=20006,
                    operator_id=20001,
                    data={"notice_type": "group_decrease", "sub_type": "kick"},
                ),
            ]
        )


async def add_reviews(review_inv: UUID) -> None:
    def item(status: str, minutes: float, text: str, verdict: Verdict, **kw: Any) -> ReviewItem:
        return ReviewItem(
            created_at=NOW - timedelta(minutes=minutes),
            group_id=TECH,
            user_id=20003,
            request_text=text,
            reply=bot_reply("回复", "（示例回复）" + text).model_dump(mode="json"),
            verdict=verdict_record("output", verdict),
            status=status,
            expires_at=NOW - timedelta(minutes=minutes) + timedelta(hours=24),
            **kw,
        )

    async with get_session() as s:
        pending = await s.scalar(select(Invocation).where(Invocation.id == review_inv))
        assert pending is not None
        s.add_all(
            [
                ReviewItem(
                    created_at=pending.created_at,
                    invocation_id=review_inv,
                    group_id=TECH,
                    user_id=pending.user_id,
                    trigger_qq_message_id=pending.trigger_qq_message_id,
                    request_text=pending.request_text,
                    reply=pending.reply or {},
                    verdict=pending.output_verdict or {},
                    expires_at=pending.created_at + timedelta(hours=24),
                ),
                item(
                    "approved",
                    600,
                    "公司内网怎么连",
                    ("Controversial", 0.55, "review", ["网络安全"]),
                    decided_by=ADMIN,
                    decided_at=NOW - timedelta(minutes=590),
                ),
                item(
                    "rejected",
                    900,
                    "帮我编一个请假理由",
                    ("Controversial", 0.58, "review", ["欺骗"]),
                    decided_by=f"qq:{SUPERUSER}",
                    decided_at=NOW - timedelta(minutes=880),
                ),
                ReviewItem(
                    created_at=NOW - timedelta(days=2),
                    group_id=GAME,
                    user_id=20005,
                    request_text="外挂哪里买",
                    reply=bot_reply("外挂", "（示例回复）外挂会封号").model_dump(mode="json"),
                    verdict=verdict_record("output", ("Controversial", 0.66, "review", ["违规"])),
                    status="expired",
                    expires_at=NOW - timedelta(days=1),
                ),
            ]
        )
