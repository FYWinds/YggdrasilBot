"""Background jobs in the admin API: running ones from Redis and finished ones from the archive, in one list."""

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace

import fakeredis
import httpx
import pytest
from sqlalchemy import delete

from yggdrasil.admin import sandbox_api
from yggdrasil.db.models import SandboxJob, Transcript
from yggdrasil.db.session import get_session
from yggdrasil.sandbox import archive
from yggdrasil.sandbox.jobs import ACTIVE_KEY, Job, JobEvent, JobRunner, JobStatus, job_key
from yggdrasil.transcript import store
from yggdrasil.transcript.schema import Entry, TextPart

GROUP, OTHER = 990051, 990052


def _job(title: str, status: JobStatus, *, group: int = GROUP, ago: float = 0, job_id: str | None = None) -> Job:
    now = time.time() - ago
    return Job(
        id=job_id or uuid.uuid4().hex[:8],
        kind="command",
        group_id=group,
        user_id=10001,
        trigger_message_id=None,
        invocation_id=None,
        request_text="",
        title=title,
        spec="python train.py",
        workspace="ws",
        claim="c",
        created_at=now,
        deadline=now + 600,
        status=status,
        ended_at=None if status == "running" else now + 60,
        result="loss 0.3" if status == "succeeded" else "",
    )


@dataclass
class Jobs:
    tag: str  # in every title, so rows left by other runs never match
    running: Job
    done: Job
    failed_other_group: Job
    both: Job  # archived while its Redis record is still active (the moment between archiving and settling)


@pytest.fixture
async def jobs(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Jobs]:
    tag = uuid.uuid4().hex[:8]
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    monkeypatch.setattr(sandbox_api, "runner", lambda: JobRunner(redis=lambda: redis))  # pyright: ignore[reportArgumentType, reportReturnType]
    j = Jobs(
        tag,
        running=_job(f"训练 {tag}", "running", ago=10),
        done=_job(f"编译 {tag}", "succeeded", ago=100),
        failed_other_group=_job(f"爬虫 {tag}", "failed", group=OTHER, ago=200),
        both=_job(f"回报中 {tag}", "succeeded", ago=50),
    )
    for live in (j.running, j.both):
        await redis.hset(job_key(live.id), mapping=live.to_hash())  # pyright: ignore[reportArgumentType, reportGeneralTypeIssues]
        await redis.sadd(ACTIVE_KEY, live.id)  # pyright: ignore[reportGeneralTypeIssues]
    events = [JobEvent(at=time.time() - 90 + i, text=f"事件 {i}") for i in range(50)]
    for done in (j.done, j.failed_other_group, j.both):
        await archive.archive_job(done, events)
    yield j
    async with get_session() as session:
        await session.execute(delete(SandboxJob).where(SandboxJob.title.like(f"%{tag}")))
    await redis.aclose()


async def _list(client: httpx.AsyncClient, **params: str | int) -> tuple[list[str], int]:
    r = await client.get("/api/sandbox/jobs", params=params)
    assert r.status_code == 200
    body = r.json()
    return [i["id"] for i in body["items"]], body["total"]


async def test_running_jobs_come_first_then_the_archive_without_duplicates(
    client: httpx.AsyncClient, jobs: Jobs
) -> None:
    ids, total = await _list(client, q=jobs.tag)
    assert ids == [jobs.running.id, jobs.both.id, jobs.done.id, jobs.failed_other_group.id]
    assert total == 4
    # Paging crosses from the live part into the archive.
    assert (await _list(client, q=jobs.tag, limit=2, offset=1))[0] == [jobs.both.id, jobs.done.id]


async def test_filters_apply_to_live_and_archived_jobs(client: httpx.AsyncClient, jobs: Jobs) -> None:
    assert (await _list(client, q=jobs.tag, status="running"))[0] == [jobs.running.id]
    assert (await _list(client, q=jobs.tag, status="failed"))[0] == [jobs.failed_other_group.id]
    assert (await _list(client, q=jobs.tag, group_id=OTHER))[0] == [jobs.failed_other_group.id]
    assert (await _list(client, q=f"爬虫 {jobs.tag}"))[0] == [jobs.failed_other_group.id]


async def test_detail_of_an_archived_job_has_its_whole_event_log(client: httpx.AsyncClient, jobs: Jobs) -> None:
    r = await client.get(f"/api/sandbox/jobs/{jobs.done.id}")
    assert r.status_code == 200
    body = r.json()
    assert (body["status"], body["result"], body["spec"]) == ("succeeded", "loss 0.3", "python train.py")
    assert [e["text"] for e in body["events"]] == [f"事件 {i}" for i in range(50)]
    running = (await client.get(f"/api/sandbox/jobs/{jobs.running.id}")).json()
    assert running["status"] == "running"
    assert (await client.get("/api/sandbox/jobs/nope")).status_code == 404


async def test_command_job_transcript_is_the_script_and_its_output(client: httpx.AsyncClient, jobs: Jobs) -> None:
    body = (await client.get(f"/api/sandbox/jobs/{jobs.done.id}/transcript")).json()
    assert (body["source"], body["live"]) == ("command", False)
    entries = body["entries"]
    assert [e["kind"] for e in entries] == ["user", "tool", "event"]
    assert entries[0]["parts"][0]["text"] == "python train.py"
    assert (entries[1]["name"], entries[1]["outcome"], entries[1]["duration_ms"]) == ("bash", "ok", 60_000)
    assert entries[1]["parts"][0]["text"] == "loss 0.3"
    assert entries[2]["text"] == "已完成"
    running = (await client.get(f"/api/sandbox/jobs/{jobs.running.id}/transcript")).json()
    assert running["live"] is True
    assert [e["kind"] for e in running["entries"]] == ["user"]
    assert (await client.get("/api/sandbox/jobs/nope/transcript")).status_code == 404


async def test_agent_job_transcript_accumulates_appended_messages(client: httpx.AsyncClient, jobs: Jobs) -> None:
    recorded = replace(_job(f"建站 {jobs.tag}", "failed", ago=30), kind="agent", error="模型请求失败")
    unrecorded = replace(_job(f"旧任务 {jobs.tag}", "succeeded", ago=40), kind="agent")
    events = [JobEvent(at=time.time() - 35, text="开始工作")]
    for job in (recorded, unrecorded):
        await archive.archive_job(job, events)
    await store.append_job(recorded.id, [Entry(kind="user", parts=[TextPart(text="搭个网站", chars=4)])], {}, full=True)
    await store.append_job(
        recorded.id, [Entry(kind="assistant", parts=[TextPart(text="好的", chars=2)])], {}, full=True
    )
    try:
        body = (await client.get(f"/api/sandbox/jobs/{recorded.id}/transcript")).json()
        assert (body["available"], body["source"]) == (True, "omp")
        assert [e["kind"] for e in body["entries"]] == ["user", "assistant", "event"]
        assert body["entries"][-1] == {**body["entries"][-1], "level": "error", "text": "失败：模型请求失败"}
        # A job from before transcripts shows its task and its event log.
        old = (await client.get(f"/api/sandbox/jobs/{unrecorded.id}/transcript")).json()
        assert old["available"] is False
        assert [(e["kind"], e.get("text")) for e in old["entries"]] == [
            ("user", None),
            ("event", "开始工作"),
            ("event", "已完成"),
        ]
    finally:
        async with get_session() as session:
            await session.execute(delete(Transcript).where(Transcript.job_id == recorded.id))
