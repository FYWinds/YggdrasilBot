"""The background job framework with an in-memory workspace and in-process Redis: limits, deadlines, restart
resume, cancellation, a vanished workspace, the workspace binding and lifetime extension, report retries, and an
agent job driving the fake omp."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import fakeredis
import pytest
from pydantic_ai.workspaces import WorkspaceRef, WorkspaceUnavailableError

from tests.sandbox.local_rpc import LocalRpc, fake_env
from yggdrasil import relay
from yggdrasil.sandbox import SandboxError, SandboxToolError, omp
from yggdrasil.sandbox.backend import UNKNOWN_OUTCOME
from yggdrasil.sandbox.jobs import (
    ACTIVE_KEY,
    START_MARGIN_S,
    Job,
    JobOps,
    JobRunner,
    Output,
    ReportDeferred,
    Requester,
    job_key,
    keep_field,
    running_key,
)
from yggdrasil.sandbox.state import Link, keep_key
from yggdrasil.settings import Settings, ToolSettings

GROUP = 10001
WS = "train"
CLAIM = "ygg-sbx-10001-train-abcdef"
REQ = Requester(group_id=GROUP, user_id=20002, trigger_message_id=777, invocation_id="inv-1", request_text="跑个训练")


class Clock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


class Pod:
    """The workspace's files and processes."""

    def __init__(self, tmp: Path, clock: Clock) -> None:
        self.tmp = tmp
        self.clock = clock
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.exits: dict[str, omp.ExitInfo] = {}
        self.killed: list[str] = []
        self.rpcs: dict[str, LocalRpc] = {}
        self.rpc_starts: list[tuple[list[str], dict[str, str], str]] = []
        self.mode = "ok"
        self.next_pid = 100
        self.gone = False
        self.claim = CLAIM
        self.max_until = clock() + 240 * 60
        self.renewals = 0
        self.published: list[int] = []
        self.names: list[str] = []  # the workspace names the runner asked for
        self.extended: list[float] = []
        # Run while a start is in flight (the process exists already), e.g. a poll racing the start.
        self.during_start: Callable[[], Awaitable[None]] | None = None
        self.start_error: Exception | None = None  # raised after the process started (outcome unknown)

    def finish(self, directory: str, code: int, out: bytes = b"") -> None:
        self.files[f"{directory}/out"] = out
        self.exits[directory] = omp.ExitInfo(exit=code, signal=None)


class FakeBackend:
    def __init__(self, pod: Pod) -> None:
        self.pod = pod
        self.ref = WorkspaceRef(provider="sandboxd", id=pod.claim)

    async def working_dir(self) -> str:
        if self.pod.gone:
            raise WorkspaceUnavailableError("gone")
        return "/workspace"

    async def aclose(self) -> None:
        return None


class FakeOps(JobOps):
    def __init__(self, pod: Pod) -> None:
        self.pod = pod

    async def write(self, path: str, data: bytes) -> None:
        self.pod.files[path] = data
        self.pod.dirs.add(path.rsplit("/", 1)[0])

    async def start_sh(self, directory: str) -> int:
        assert f"{directory}/script.sh" in self.pod.files
        self.pod.dirs.add(directory)
        self.pod.next_pid += 1
        if self.pod.during_start is not None:
            await self.pod.during_start()
        if self.pod.start_error is not None:
            raise self.pod.start_error
        return self.pod.next_pid

    async def start_rpc(self, directory: str, argv: list[str], env: dict[str, str], *, cwd: str) -> int:
        self.pod.rpc_starts.append((argv, env, cwd))
        self.pod.dirs.add(directory)
        rpc = LocalRpc(self.pod.tmp / directory.rsplit("/", 1)[-1])
        self.pod.rpcs[directory] = rpc
        pid = await rpc.start(fake_env(self.pod.mode))
        if self.pod.during_start is not None:
            await self.pod.during_start()
        return pid

    async def kill(self, directory: str) -> None:
        self.pod.killed.append(directory)
        if directory in self.pod.rpcs:
            await self.pod.rpcs[directory].kill()
        else:
            self.pod.exits.setdefault(directory, omp.ExitInfo(exit=None, signal=15))

    async def send(self, directory: str, frame: dict[str, Any]) -> None:
        await self.pod.rpcs[directory].send(frame)

    async def close_stdin(self, directory: str) -> None:
        await self.pod.rpcs[directory].close_stdin()

    async def poll(self, directory: str, events: str = "", offset: int = 0, limit: int = 0) -> omp.Poll:
        if directory not in self.pod.dirs:
            return omp.Poll(gone=True, exit=None, data=b"")
        if directory in self.pod.rpcs:
            return await self.pod.rpcs[directory].poll(offset, limit)
        return omp.Poll(gone=False, exit=self.pod.exits.get(directory), data=b"")

    async def output(self, path: str, *, head: int, tail: int) -> Output:
        data = self.pod.files.get(path, b"")
        return Output(text=data.decode(), size=len(data), truncated=False)

    async def tail_text(self, path: str, chars: int) -> str:
        directory = path.rsplit("/", 1)[0]
        if directory in self.pod.rpcs:
            return await self.pod.rpcs[directory].stderr_tail(chars)
        return ""

    async def file_size(self, path: str) -> int | None:
        data = self.pod.files.get(path)
        return len(data) if data is not None else None


class FakeWorkspaces:
    """One workspace (``WS``) of ``GROUP``; another name has no pod."""

    def __init__(self, pod: Pod) -> None:
        self.pod = pod

    def _here(self, group_id: int, name: str) -> bool:
        self.pod.names.append(name)
        return group_id == GROUP and name == WS and not self.pod.gone

    async def backend_for(self, group_id: int, name: str) -> tuple[Any, str] | None:
        if not self._here(group_id, name):
            return None
        return FakeBackend(self.pod), self.pod.claim

    async def renew(self, group_id: int, name: str, cfg: ToolSettings) -> bool:
        self.pod.renewals += 1
        return self._here(group_id, name)

    async def extend(self, group_id: int, name: str, claim: str, until: float, cfg: ToolSettings) -> float | None:
        if not self._here(group_id, name) or claim != self.pod.claim:
            return None
        self.pod.extended.append(until)
        self.pod.max_until = max(self.pod.max_until, until)
        return self.pod.max_until

    async def publish(self, group_id: int, name: str, port: int, cfg: ToolSettings, *, audit: dict[str, Any]) -> Link:
        self._here(group_id, name)
        self.pod.published.append(port)
        return Link(port=port, url=f"https://sbx-x.example.com/{port}", command="python", created_at=0.0)

    async def unpublish(self, group_id: int, name: str, port: int, cfg: ToolSettings, *, audit: dict[str, Any]) -> bool:
        self._here(group_id, name)
        return True


class Reports:
    def __init__(self) -> None:
        self.jobs: list[Job] = []
        self.defer = 0

    async def __call__(self, job: Job) -> str | None:
        if self.defer:
            self.defer -= 1
            raise ReportDeferred("no bot")
        self.jobs.append(job)
        return f"inv-report-{len(self.jobs)}"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def pod(tmp_path: Path, clock: Clock) -> Pod:
    return Pod(tmp_path, clock)


@pytest.fixture
def reports() -> Reports:
    return Reports()


@pytest.fixture
def tools() -> dict[str, Any]:
    return {}


def make_runner(
    pod: Pod, redis: fakeredis.FakeAsyncRedis, reports: Reports, clock: Clock, tools: dict[str, Any]
) -> JobRunner:
    settings = Settings(tools=ToolSettings(**tools))
    return JobRunner(
        workspaces=lambda: FakeWorkspaces(pod),
        redis=lambda: redis,  # pyright: ignore[reportArgumentType, reportReturnType]
        ops=lambda backend: FakeOps(pod),
        report=reports,
        settings=lambda gid: settings,
        link_domain=lambda: "example.com",
        clock=clock,
    )


@pytest.fixture
def runner(
    pod: Pod, fake_redis: fakeredis.FakeAsyncRedis, reports: Reports, clock: Clock, tools: dict[str, Any]
) -> JobRunner:
    return make_runner(pod, fake_redis, reports, clock, tools)


@pytest.fixture
def fake_relay(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"issued": [], "revoked": []}

    async def models() -> list[relay.RelayModel]:
        return [relay.RelayModel("claude-a", "Claude A", 200000, 64000, True, True)]

    async def issue(**kw: Any) -> str:
        calls["issued"].append(kw)
        return "tok-secret"

    async def revoke(job_id: str) -> None:
        calls["revoked"].append(job_id)

    async def usage(job_id: str) -> relay.RelayUsage:
        return relay.RelayUsage(requests=3, input_tokens=1200, cached_tokens=800, output_tokens=90)

    monkeypatch.setattr(relay, "relay_url", lambda: "http://10.0.0.1:8090")
    monkeypatch.setattr(relay, "relay_models", models)
    monkeypatch.setattr(relay, "issue_token", issue)
    monkeypatch.setattr(relay, "revoke_token", revoke)
    monkeypatch.setattr(relay, "relay_usage", usage)
    return calls


async def _until(runner: JobRunner, job_id: str, *, done: bool = True, seconds: float = 20) -> Job:
    async with asyncio.timeout(seconds):
        while True:
            await runner.tick(wait=True)
            job = await runner.get(job_id)
            assert job is not None
            if (not job.running) == done and job.report not in ("pending", "running"):
                return job
            await asyncio.sleep(0.05)


async def test_command_job_runs_and_reports(
    runner: JobRunner, pod: Pod, fake_redis: fakeredis.FakeAsyncRedis, reports: Reports, clock: Clock
) -> None:
    job = await runner.start_command(REQ, FakeBackend(pod), workspace=WS, script="python train.py", title="训练模型")
    assert job.status == "running"
    assert job.pid == 101
    assert pod.files[f"{job.dir}/script.sh"] == b"python train.py"
    assert await fake_redis.hget(keep_key(GROUP, WS), keep_field(job.id)) == "101"  # pyright: ignore[reportGeneralTypeIssues]
    assert await fake_redis.hget(running_key(GROUP), job.id) == "command"  # pyright: ignore[reportGeneralTypeIssues]

    clock.offset += 600  # past a quarter of the idle timeout: the poll renews the workspace
    await runner.tick(wait=True)
    assert (await runner.get(job.id)).status == "running"  # pyright: ignore[reportOptionalMemberAccess]
    assert pod.renewals == 1

    pod.finish(job.dir, 0, b"epoch 1 loss 0.3\ndone\n")
    done = await _until(runner, job.id)
    assert done.status == "succeeded"
    assert done.exit_code == 0
    assert "退出码 0" in done.result
    assert "epoch 1 loss 0.3" in done.result
    assert done.report == "done"
    assert done.report_invocation == "inv-report-1"
    assert [j.id for j in reports.jobs] == [job.id]
    assert reports.jobs[0].trigger_message_id == 777
    assert await fake_redis.hget(keep_key(GROUP, WS), keep_field(job.id)) is None  # pyright: ignore[reportGeneralTypeIssues]
    assert await fake_redis.hlen(running_key(GROUP)) == 0  # pyright: ignore[reportGeneralTypeIssues]
    assert not await fake_redis.sismember(ACTIVE_KEY, job.id)  # pyright: ignore[reportGeneralTypeIssues]
    assert await fake_redis.ttl(job_key(job.id)) > 0
    assert [j.id for j in await runner.for_group(GROUP)] == [job.id]
    assert [e.text for e in await runner.events(job.id)][::-1][:1] == ["已启动"]


@pytest.mark.parametrize("tools", [{"sandbox_max_jobs": 2}])
async def test_limits(runner: JobRunner, pod: Pod, fake_relay: dict[str, Any]) -> None:
    backend = FakeBackend(pod)
    await runner.start_command(REQ, backend, workspace=WS, script="sleep 1000", title="a")
    agent = await runner.start_agent(REQ, backend, workspace=WS, task="搭个网站", title="b")
    with pytest.raises(SandboxError, match="最多 2 个"):
        await runner.start_command(REQ, backend, workspace=WS, script="sleep 1000", title="c")
    await runner.cancel(agent.id, by="chat:1")
    await _until(runner, agent.id)
    other = await runner.start_command(REQ, backend, workspace=WS, script="sleep 1000", title="d")
    assert other.status == "running"
    for rpc in pod.rpcs.values():
        await rpc.kill()


@pytest.mark.parametrize("tools", [{"sandbox_max_jobs": 3}])
async def test_one_agent_job_per_group(runner: JobRunner, pod: Pod, fake_relay: dict[str, Any]) -> None:
    pod.mode = "hang"
    backend = FakeBackend(pod)
    first = await runner.start_agent(REQ, backend, workspace=WS, task="任务一", title="一")
    with pytest.raises(SandboxError, match="只能有一个"):
        await runner.start_agent(REQ, backend, workspace=WS, task="任务二", title="二")
    await runner.cancel(first.id, by="chat:1")
    await _until(runner, first.id)


@pytest.mark.parametrize("tools", [{"sandbox_max_jobs": 0}])
async def test_jobs_switched_off(runner: JobRunner, pod: Pod) -> None:
    with pytest.raises(SandboxError, match="关闭"):
        await runner.start_command(REQ, FakeBackend(pod), workspace=WS, script="true", title="x")


async def test_a_job_start_extends_the_workspace_lifetime(runner: JobRunner, pod: Pod, clock: Clock) -> None:
    # Only 17 minutes left before the workspace's hard cap: the job still starts, and the cap moves out past its
    # deadline by an invocation's length (sandbox_invocation_s) plus the stop margin.
    pod.max_until = clock() + 17 * 60
    job = await runner.start_command(REQ, FakeBackend(pod), workspace=WS, script="true", title="x")
    assert job.status == "running"
    [until] = pod.extended
    assert until == pytest.approx(job.deadline + 600 + START_MARGIN_S, abs=1)
    assert pod.max_until == until
    # A cap already further out is kept.
    pod.max_until = clock() + 10 * 3600
    await runner.start_command(REQ, FakeBackend(pod), workspace=WS, script="true", title="y")
    assert pod.max_until == pytest.approx(clock() + 10 * 3600, abs=1)


async def test_jobs_are_bound_to_their_workspace(
    runner: JobRunner, pod: Pod, fake_redis: fakeredis.FakeAsyncRedis, reports: Reports
) -> None:
    job = await runner.start_command(REQ, FakeBackend(pod), workspace=WS, script="sleep 1000", title="x")
    assert job.workspace == WS
    assert (await runner.get(job.id)).workspace == WS  # pyright: ignore[reportOptionalMemberAccess]
    assert await fake_redis.hget(keep_key(GROUP, WS), keep_field(job.id)) == str(job.pid)  # pyright: ignore[reportGeneralTypeIssues]
    pod.names.clear()
    await runner.tick(wait=True)
    assert set(pod.names) == {WS}  # every poll addresses (group, name)
    # Started against a workspace name the pod does not belong to: refused before anything runs.
    with pytest.raises(SandboxError, match="重置或回收"):
        await runner.start_command(REQ, FakeBackend(pod), workspace="other", script="true", title="y")
    pod.finish(job.dir, 0)
    done = await _until(runner, job.id)
    assert done.status == "succeeded"
    assert await fake_redis.hget(keep_key(GROUP, WS), keep_field(job.id)) is None  # pyright: ignore[reportGeneralTypeIssues]


async def test_a_start_the_poller_gave_up_on_is_stopped(
    runner: JobRunner, pod: Pod, clock: Clock, fake_redis: fakeredis.FakeAsyncRedis, reports: Reports
) -> None:
    """The supervisor answers only after START_GRACE_S (the poller already failed the job): the late start
    must not revive it, and the process it started is killed instead of running untracked."""

    async def poll_meanwhile() -> None:
        clock.offset += 300
        await runner.tick(wait=True)

    pod.during_start = poll_meanwhile
    with pytest.raises(SandboxError, match="启动太慢"):
        await runner.start_command(REQ, FakeBackend(pod), workspace=WS, script="sleep 1000", title="慢")
    [job] = await runner.for_group(GROUP)
    assert job.status == "failed"
    assert job.pid is None
    assert pod.killed == [job.dir]
    assert await fake_redis.hget(keep_key(GROUP, WS), keep_field(job.id)) is None  # pyright: ignore[reportGeneralTypeIssues]
    assert await fake_redis.hlen(running_key(GROUP)) == 0  # pyright: ignore[reportGeneralTypeIssues]
    pod.during_start = None
    for _ in range(3):
        await runner.tick(wait=True)
    assert (await runner.get(job.id)).status == "failed"  # pyright: ignore[reportOptionalMemberAccess]


async def test_a_late_agent_start_revokes_its_token(
    runner: JobRunner, pod: Pod, clock: Clock, fake_relay: dict[str, Any]
) -> None:
    async def poll_meanwhile() -> None:
        clock.offset += 300
        await runner.tick(wait=True)

    pod.during_start = poll_meanwhile
    pod.mode = "hang"
    with pytest.raises(SandboxError, match="启动太慢"):
        await runner.start_agent(REQ, FakeBackend(pod), workspace=WS, task="做点什么", title="慢")
    [job] = await runner.for_group(GROUP)
    assert job.id in fake_relay["revoked"]
    assert pod.killed == [job.dir]
    proc = pod.rpcs[job.dir].proc
    assert proc is not None
    assert await asyncio.wait_for(proc.wait(), 5) != 0  # killed, not left running


async def test_unknown_start_outcome_stops_the_process_and_frees_the_slot(
    runner: JobRunner, pod: Pod, fake_redis: fakeredis.FakeAsyncRedis, reports: Reports
) -> None:
    pod.start_error = SandboxToolError(UNKNOWN_OUTCOME)
    with pytest.raises(SandboxError, match="结果未知") as raised:
        await runner.start_command(REQ, FakeBackend(pod), workspace=WS, script="sleep 1000", title="x")
    assert "停掉" in str(raised.value)
    [job] = await runner.for_group(GROUP)
    assert job.status == "failed"
    assert job.report == "skipped"
    assert pod.killed == [job.dir]  # the supervisor may have started: stopped by its pid file
    assert await fake_redis.hlen(running_key(GROUP)) == 0  # pyright: ignore[reportGeneralTypeIssues]
    assert not await fake_redis.sismember(ACTIVE_KEY, job.id)  # pyright: ignore[reportGeneralTypeIssues]
    pod.start_error = None
    await runner.start_command(REQ, FakeBackend(pod), workspace=WS, script="true", title="y")  # the slot is free again
    assert reports.jobs == []


async def test_deadline_stops_the_job(runner: JobRunner, pod: Pod, clock: Clock, reports: Reports) -> None:
    job = await runner.start_command(REQ, FakeBackend(pod), workspace=WS, script="sleep 99999", title="长任务")
    pod.files[f"{job.dir}/out"] = b"still going\n"
    clock.offset += 31 * 60
    done = await _until(runner, job.id)
    assert done.status == "timeout"
    assert pod.killed == [job.dir]
    assert "超过最长运行时间" in done.error
    assert "still going" in done.result
    assert done.report == "done"
    assert len(reports.jobs) == 1


async def test_restart_resumes_from_redis(
    pod: Pod, fake_redis: fakeredis.FakeAsyncRedis, reports: Reports, clock: Clock
) -> None:
    before = make_runner(pod, fake_redis, reports, clock, {})
    job = await before.start_command(REQ, FakeBackend(pod), workspace=WS, script="make", title="编译")
    await before.tick(wait=True)
    await before.aclose()
    del before  # the bot restarts: nothing in memory survives

    pod.finish(job.dir, 2, b"error: missing header\n")
    after = make_runner(pod, fake_redis, reports, clock, {})
    done = await _until(after, job.id)
    assert done.status == "failed"
    assert done.exit_code == 2
    assert "missing header" in done.result
    assert len(reports.jobs) == 1


async def test_two_pollers_report_once(
    pod: Pod, fake_redis: fakeredis.FakeAsyncRedis, reports: Reports, clock: Clock
) -> None:
    a = make_runner(pod, fake_redis, reports, clock, {})
    b = make_runner(pod, fake_redis, reports, clock, {})
    job = await a.start_command(REQ, FakeBackend(pod), workspace=WS, script="true", title="x")
    pod.finish(job.dir, 0, b"ok")
    for _ in range(5):
        await asyncio.gather(a.tick(wait=True), b.tick(wait=True))
    assert len(reports.jobs) == 1
    assert (await a.get(job.id)).report == "done"  # pyright: ignore[reportOptionalMemberAccess]


async def test_chat_cancel_skips_the_report(runner: JobRunner, pod: Pod, reports: Reports) -> None:
    job = await runner.start_command(REQ, FakeBackend(pod), workspace=WS, script="sleep 1000", title="x")
    other = await runner.cancel(job.id, by="chat:20002", group_id=GROUP + 1)
    assert other is None  # another group's job is invisible
    assert await runner.cancel(job.id, by="chat:20002", group_id=GROUP) is not None
    done = await _until(runner, job.id)
    assert done.status == "cancelled"
    assert done.report == "skipped"
    assert pod.killed == [job.dir]
    assert reports.jobs == []


async def test_admin_cancel_still_reports(runner: JobRunner, pod: Pod, reports: Reports) -> None:
    job = await runner.start_command(REQ, FakeBackend(pod), workspace=WS, script="sleep 1000", title="x")
    await runner.cancel(job.id, by="admin:ops@example.com")
    done = await _until(runner, job.id)
    assert done.status == "cancelled"
    assert done.report == "done"
    assert len(reports.jobs) == 1


async def test_workspace_gone_fails_the_job(runner: JobRunner, pod: Pod, reports: Reports) -> None:
    job = await runner.start_command(REQ, FakeBackend(pod), workspace=WS, script="sleep 1000", title="x")
    pod.gone = True
    done = await _until(runner, job.id)
    assert done.status == "failed"
    assert "回收或重置" in done.error
    assert len(reports.jobs) == 1


async def test_replaced_workspace_fails_the_job(runner: JobRunner, pod: Pod) -> None:
    job = await runner.start_command(REQ, FakeBackend(pod), workspace=WS, script="sleep 1000", title="x")
    pod.claim = "ygg-sbx-10001-zzzzzz"
    done = await _until(runner, job.id)
    assert done.status == "failed"
    assert "回收或重置" in done.error


async def test_deferred_report_is_retried(runner: JobRunner, pod: Pod, reports: Reports, clock: Clock) -> None:
    reports.defer = 1
    job = await runner.start_command(REQ, FakeBackend(pod), workspace=WS, script="true", title="x")
    pod.finish(job.dir, 0)
    await runner.tick(wait=True)
    pending = await runner.get(job.id)
    assert pending is not None
    assert pending.report == "pending"
    assert reports.jobs == []
    await runner.tick(wait=True)
    assert reports.jobs == []  # not before the retry delay
    clock.offset += 31
    done = await _until(runner, job.id)
    assert done.report == "done"
    assert len(reports.jobs) == 1


async def test_agent_job_end_to_end(
    runner: JobRunner,
    pod: Pod,
    reports: Reports,
    fake_relay: dict[str, Any],
    fake_redis: fakeredis.FakeAsyncRedis,
) -> None:
    pod.files["/workspace/site/shot.png"] = b"\x89PNG..."
    job = await runner.start_agent(REQ, FakeBackend(pod), workspace=WS, task="搭个鹈鹕网站并截图", title="鹈鹕网站")
    assert job.model == "claude-a"
    issued = fake_relay["issued"][0]
    assert issued["job_id"] == job.id
    assert issued["models"] == ["claude-a"]
    assert issued["max_input_tokens"] == 3_000_000
    assert issued["max_output_tokens"] == 100_000
    models = json.loads(pod.files[omp.MODELS_PATH])
    assert models["providers"]["ygg"]["baseUrl"] == "http://10.0.0.1:8090"
    rules = pod.files[f"{job.dir}/rules.md"].decode()
    assert "publish(port)" in rules
    assert job.id in rules
    argv, env, cwd = pod.rpc_starts[0]
    assert argv[argv.index("--append-system-prompt") + 1] == f"{job.dir}/rules.md"
    assert env[omp.TOKEN_ENV] == "tok-secret"
    assert cwd == "/workspace"
    assert await fake_redis.hget(keep_key(GROUP, WS), keep_field(job.id)) is not None  # pyright: ignore[reportGeneralTypeIssues]

    done = await _until(runner, job.id)
    assert done.status == "succeeded", done.error
    assert done.result.startswith("完成了。host_1:已记下这张图")
    assert done.images == [{"path": "/workspace/site/shot.png", "caption": "首页截图"}]
    assert done.progress == "做到一半"
    assert done.usage == {"requests": 3, "input_tokens": 1200, "cached_tokens": 800, "output_tokens": 90}
    assert fake_relay["revoked"] == [job.id]
    assert await fake_redis.hget(keep_key(GROUP, WS), keep_field(job.id)) is None  # pyright: ignore[reportGeneralTypeIssues]
    texts = [e.text for e in await runner.events(job.id)]
    assert "进度：做到一半" in texts
    assert "show /workspace/site/shot.png" in texts
    assert len(reports.jobs) == 1
    assert reports.jobs[0].images == done.images


async def test_agent_job_cancel_revokes_the_token(
    runner: JobRunner, pod: Pod, reports: Reports, fake_relay: dict[str, Any]
) -> None:
    pod.mode = "hang"
    job = await runner.start_agent(REQ, FakeBackend(pod), workspace=WS, task="一直做", title="无尽")
    async with asyncio.timeout(10):
        while json.loads((await runner.get(job.id)).driver)["phase"] != "prompting":  # pyright: ignore[reportOptionalMemberAccess]
            await runner.tick(wait=True)
            await asyncio.sleep(0.05)
    await runner.cancel(job.id, by="admin:ops@example.com")
    done = await _until(runner, job.id)
    assert done.status == "cancelled"
    assert fake_relay["revoked"] == [job.id]
    assert done.result == "完成了。"  # the partial answer collected after abort
    assert len(reports.jobs) == 1
    assert not pod.rpcs[job.dir].running
