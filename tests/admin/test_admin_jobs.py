"""Background jobs in the admin API: running ones from Redis and finished ones from the archive, in one list."""

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import fakeredis
import httpx
import pytest
from sqlalchemy import delete

from yggdrasil.admin import sandbox_api
from yggdrasil.db.models import SandboxJob
from yggdrasil.db.session import get_session
from yggdrasil.sandbox import archive
from yggdrasil.sandbox.jobs import ACTIVE_KEY, Job, JobEvent, JobRunner, JobStatus, job_key

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
