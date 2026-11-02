"""Background sandbox jobs: archived ones with transcripts and a report run, and one still running in Redis."""

import base64
import sys
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from yggdrasil.devtools.seed.chat import Chat
from yggdrasil.devtools.seed.data import ADMIN, JOB_PREFIX, MEDIA, MODEL, NOW, TECH
from yggdrasil.devtools.seed.replay import invoke
from yggdrasil.devtools.seed.scenarios import SAFE, Run, Turn, bot_reply
from yggdrasil.kv import redis_client
from yggdrasil.sandbox import archive
from yggdrasil.sandbox.jobs import ACTIVE_KEY, Job, JobEvent, events_key, group_key, job_key, render_rules, running_key
from yggdrasil.settings import Settings
from yggdrasil.transcript import store
from yggdrasil.transcript.capture import text_part
from yggdrasil.transcript.notes import Blob
from yggdrasil.transcript.omp import message_entries
from yggdrasil.transcript.schema import Entry


def _job(
    suffix: str, kind: Literal["command", "agent"], title: str, spec: str, *, hours: float, minutes: float, **kw: Any
) -> Job:
    created = (NOW - timedelta(hours=hours)).timestamp()
    ended = created + minutes * 60 if kw.get("status", "succeeded") not in ("running", "starting") else None
    defaults: dict[str, Any] = {
        "group_id": TECH,
        "user_id": 20003,
        "trigger_message_id": None,
        "invocation_id": None,
        "request_text": title,
        "workspace": "train" if kind == "command" else "site",
        "claim": f"ygg-sbx-{TECH}-seed",
        "deadline": created + 3600,
        "ended_at": ended,
        "status": "succeeded",
    }
    return Job(id=f"{JOB_PREFIX}{suffix}", kind=kind, title=title, spec=spec, created_at=created, **(defaults | kw))


def _omp_messages(job: Job, *, finished: bool) -> list[dict[str, Any]]:
    """What omp reports for a small site-building session (``message_end`` payloads)."""
    t = int(job.created_at * 1000) + 3000
    shot = base64.b64encode(MEDIA["screenshot"].data).decode()
    msgs: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "先建一个静态站点目录，写首页，再起个服务器截图。"},
                {
                    "type": "toolCall",
                    "id": "toolu_1",
                    "name": "bash",
                    "arguments": {
                        "command": "mkdir -p site && cd site && cat > index.html <<'EOF'\n<h1>鹈鹕</h1>\nEOF"
                    },
                },
            ],
            "model": MODEL,
            "usage": {"input": 5200, "output": 180, "cacheRead": 0},
            "stopReason": "toolUse",
            "timestamp": t,
            "duration": 4200,
        },
        {
            "role": "toolResult",
            "toolCallId": "toolu_1",
            "toolName": "bash",
            "content": [{"type": "text", "text": ""}],
            "isError": False,
            "timestamp": t + 4800,
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "首页写好了，起个服务器截图看看效果。"},
                {
                    "type": "toolCall",
                    "id": "toolu_2",
                    "name": "browser",
                    "arguments": {"action": "screenshot", "url": "http://127.0.0.1:8000/"},
                },
            ],
            "model": MODEL,
            "usage": {"input": 5600, "output": 90, "cacheRead": 5000},
            "stopReason": "toolUse",
            "timestamp": t + 5000,
            "duration": 2600,
        },
        {
            "role": "toolResult",
            "toolCallId": "toolu_2",
            "toolName": "browser",
            "content": [{"type": "image", "data": shot, "mimeType": "image/png"}],
            "isError": False,
            "timestamp": t + 11000,
        },
    ]
    if finished:
        msgs += [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "toolu_3",
                        "name": "show",
                        "arguments": {"path": "/workspace/site/shot.png", "caption": "首页截图"},
                    }
                ],
                "model": MODEL,
                "usage": {"input": 7400, "output": 60, "cacheRead": 5500},
                "stopReason": "toolUse",
                "timestamp": t + 11500,
                "duration": 1800,
            },
            {
                "role": "toolResult",
                "toolCallId": "toolu_3",
                "toolName": "show",
                "content": [{"type": "text", "text": "已记下这张图，会附在完成报告里"}],
                "isError": False,
                "timestamp": t + 13500,
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "完成了：/workspace/site 下是一个单页站点，首页截图已附上。"}],
                "model": MODEL,
                "usage": {"input": 7600, "output": 40, "cacheRead": 7300},
                "stopReason": "stop",
                "timestamp": t + 14000,
                "duration": 1500,
            },
        ]
    return msgs


async def _transcribe(job: Job, messages: list[dict[str, Any]]) -> None:
    """Write a job's transcript the way ``JobRunner`` does: the rules and the task, then omp's messages."""
    at = datetime.fromtimestamp(job.created_at, UTC)
    opening = [
        Entry(kind="system", at=at, parts=[text_part(render_rules(Settings(), job, links=False), True)]),
        Entry(kind="user", at=at, parts=[text_part(job.spec, True)]),
    ]
    await store.append_job(job.id, opening, {}, full=True)
    starts: dict[str, float] = {}
    for msg in messages:
        blobs: dict[str, Blob] = {}
        await store.append_job(
            job.id, message_entries(msg, full=True, blobs=blobs, tool_starts=starts), blobs, full=True
        )


async def add_jobs(chats: dict[int, Chat]) -> None:
    train = _job(
        "train",
        "command",
        "训练小模型",
        "python train.py --epochs 3",
        hours=5,
        minutes=12,
        exit_code=0,
        result="epoch 1 loss 0.82\nepoch 2 loss 0.51\nepoch 3 loss 0.33\n模型已保存到 /workspace/runs/model.pt",
        report="done",
    )
    crawl = _job(
        "crawl",
        "command",
        "抓取更新日志",
        "python crawl.py https://example.com/changelog",
        hours=4,
        minutes=1,
        status="failed",
        exit_code=1,
        error="退出码 1",
        result='Traceback (most recent call last):\n  File "crawl.py", line 12\nConnectionError: example.com refused',
        report="done",
    )
    site = _job(
        "site",
        "agent",
        "鹈鹕网站",
        "搭一个介绍「鹈鹕」服务器的单页网站并截图",
        hours=3,
        minutes=6,
        model=MODEL,
        result="完成了：/workspace/site 下是一个单页站点，首页截图已附上。",
        images=[{"path": "/workspace/site/shot.png", "caption": "首页截图"}],
        usage={"requests": 4, "input_tokens": 25800, "cached_tokens": 17800, "output_tokens": 370},
        report="done",
    )
    scrape = _job(
        "scrape",
        "agent",
        "整理群聊表情包",
        "把最近一周的表情包整理成一个画廊页面",
        hours=2,
        minutes=3,
        status="cancelled",
        model=MODEL,
        cancel=f"admin:{ADMIN}",
        error="已取消",
        usage={"requests": 2, "input_tokens": 10800, "cached_tokens": 5000, "output_tokens": 270},
        report="done",
    )
    report = Run(
        "report",
        "report",
        "ok",
        [
            Turn(
                output=bot_reply(
                    "鹈鹕网站做好了",
                    "老王要的单页网站做好了，放在工作区 site 的 /workspace/site 下，首页截图附在后面。",
                )
            )
        ],
        user_id=site.user_id,
        prompt=f"后台任务「{site.title}」已完成。\n任务：{site.spec}\n结果：{site.result}\n请向发起人简短汇报。",
        output_verdict=SAFE,
    )
    site.report_invocation = str(await invoke(report, chats, report_job=site))

    def events(job: Job, *texts: str) -> list[JobEvent]:
        end = job.ended_at or NOW.timestamp()
        step = (end - job.created_at) / max(1, len(texts))
        return [JobEvent(at=job.created_at + i * step, text=t) for i, t in enumerate(texts)]

    for job, log in (
        (train, events(train, "启动", "进度：epoch 1/3", "进度：epoch 2/3", "进度：epoch 3/3", "已完成")),
        (crawl, events(crawl, "启动", "失败：退出码 1")),
        (
            site,
            events(
                site,
                "开始工作",
                "bash: mkdir -p site",
                "browser: screenshot",
                "show /workspace/site/shot.png",
                "已完成",
            ),
        ),
        (scrape, events(scrape, "开始工作", "bash: ls stickers", "请求取消（admin）", "已取消")),
    ):
        await archive.archive_job(job, log)
    await _transcribe(site, _omp_messages(site, finished=True))
    await _transcribe(scrape, _omp_messages(scrape, finished=False)[:2])

    live = _job(
        "live",
        "agent",
        "给聚会做个报名页",
        "做一个周日聚会的报名页，能填名字和人数",
        hours=0.1,
        minutes=0,
        status="running",
        user_id=20001,
        model=MODEL,
        progress="首页写好了，正在截图",
        deadline=(NOW + timedelta(minutes=40)).timestamp(),
    )
    await _transcribe(live, _omp_messages(live, finished=False))
    try:
        r = redis_client()
        await r.hset(job_key(live.id), mapping=live.to_hash())  # pyright: ignore[reportArgumentType, reportGeneralTypeIssues]
        await r.hset(running_key(live.group_id), live.id, live.kind)  # pyright: ignore[reportGeneralTypeIssues]
        await r.zadd(group_key(live.group_id), {live.id: live.created_at})
        await r.sadd(ACTIVE_KEY, live.id)  # pyright: ignore[reportGeneralTypeIssues]
        for e in events(live, "开始工作", "bash: mkdir -p site", "进度：首页写好了，正在截图"):
            await r.lpush(events_key(live.id), f"{e.at!r}\t{e.text}")  # pyright: ignore[reportGeneralTypeIssues]
    except Exception as e:  # the rest of the seed is still useful without Redis
        sys.stderr.write(f"redis unavailable, skipped the running job: {e}\n")
