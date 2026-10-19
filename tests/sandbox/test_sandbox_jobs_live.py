"""Background jobs end to end against a sandboxd running the workspace image's ``/opt/ygg`` helpers
(``ygg-sbx-test:dev``): the chat tool starts a command, the invocation's end-of-run reap spares it, the poller
reads its files over sandboxd and the real report invocation answers the trigger message (only the model is
stubbed; Postgres and Redis as in the rest of the suite). The delegate path runs the fake omp peer inside the
sandbox under ``ygg rpc``.

Needs the YGG_TEST_SANDBOXD_* variables (see ``helpers.py``); skipped otherwise.
"""

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import fakeredis
import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits
from sqlalchemy import delete, select

from tests.sandbox.helpers import HAVE_SANDBOXD, NO_SANDBOXD, SANDBOXD_TOKEN
from yggdrasil import relay, runtime
from yggdrasil.agent import job_tools
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.db.models import Invocation
from yggdrasil.db.session import get_session
from yggdrasil.llm.registry import ResolvedModel
from yggdrasil.napcat.api import NapCatAPI
from yggdrasil.pipeline import invoke, report
from yggdrasil.sandbox import WorkspaceManager, omp
from yggdrasil.sandbox.backend import Endpoint
from yggdrasil.sandbox.jobs import START_MARGIN_S, Job, JobRunner
from yggdrasil.sandbox.k8s import KubeClient
from yggdrasil.sandbox.state import keep_key, ws_key
from yggdrasil.settings import Settings

pytestmark = pytest.mark.skipif(not HAVE_SANDBOXD, reason=NO_SANDBOXD)

GROUP = 990031
USER = 20002
TRIGGER = 424242
FAKE_OMP = Path(__file__).with_name("fake_omp.py")
FAKE_ANTHROPIC = Path(__file__).with_name("fake_anthropic.py")
SETTINGS_DOC: dict[str, Any] = {"moderation": {"enabled": False}, "interaction": {"metadata_node": False}}


class FakeBot:
    """Just the OneBot actions a report uses."""

    self_id = "10000"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, action: str, **data: Any) -> Any:
        self.calls.append((action, data))
        if action == "get_login_info":
            return {"user_id": 10000, "nickname": "bot"}
        if action == "send_group_msg":
            return {"message_id": 5001}
        if action == "send_group_forward_msg":
            return {"message_id": 5002, "res_id": "r1"}
        return None


@pytest.fixture
def mgr(sandboxd: Endpoint, kube_client: KubeClient, fake_redis: fakeredis.FakeAsyncRedis) -> WorkspaceManager:
    return WorkspaceManager(
        token=SANDBOXD_TOKEN,
        namespace="ns",
        warmpool="pool",
        link_domain=None,
        kube=kube_client,
        redis=lambda: fake_redis,
        endpoint=lambda _ip: sandboxd,
    )


@pytest.fixture
async def runner(
    mgr: WorkspaceManager, fake_redis: fakeredis.FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[JobRunner]:
    settings = Settings.model_validate(SETTINGS_DOC)
    r = JobRunner(
        workspaces=lambda: mgr,
        redis=lambda: fake_redis,  # pyright: ignore[reportArgumentType, reportReturnType]
        settings=lambda gid: settings,
        link_domain=lambda: None,
        poll_s=0.2,
    )
    monkeypatch.setattr(job_tools, "runner", lambda: r)
    monkeypatch.setattr(report, "manager", lambda: mgr)
    monkeypatch.setattr(invoke, "manager", lambda: mgr)
    monkeypatch.setattr(
        runtime, "_snapshot", runtime.Snapshot(groups={GROUP: runtime.GroupInfo(GROUP, "测试群", True, SETTINGS_DOC)})
    )
    async with get_session() as s:
        await s.execute(delete(Invocation).where(Invocation.group_id == GROUP))
    yield r
    await r.aclose()
    await mgr.destroy_all(reason="test")


def _ctx(deps: Deps) -> RunContext[Deps]:
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


def _model(seen: list[str]) -> FunctionModel:
    """The report's model: records the prompt and answers with a forward reply."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append("\n".join(str(getattr(p, "content", "")) for m in messages for p in m.parts))
        args = {"mode": "forward", "title": "训练结果", "sections": [{"content": "训练跑完了：退出码 4，见输出末尾。"}]}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        part = respond(messages, info).parts[0]
        assert isinstance(part, ToolCallPart)
        yield {0: DeltaToolCall(name=part.tool_name, json_args=part.args_as_json_str())}

    return FunctionModel(respond, stream_function=stream)


async def _until_reported(runner: JobRunner, job_id: str, seconds: float = 60) -> Job:
    async with asyncio.timeout(seconds):
        while True:
            await runner.tick(wait=True)
            job = await runner.get(job_id)
            assert job is not None
            if not job.running and job.report not in ("pending", "running"):
                return job
            await asyncio.sleep(0.3)


async def test_background_command_reports_back(
    runner: JobRunner, mgr: WorkspaceManager, fake_redis: fakeredis.FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = FakeBot()
    seen: list[str] = []
    monkeypatch.setattr(report, "any_bot", lambda: bot)
    monkeypatch.setattr(
        report,
        "chat_model",
        lambda task: ResolvedModel(task, _model(seen), ModelSettings(), UsageLimits(), "test/report-model"),
    )

    settings = Settings.model_validate(SETTINGS_DOC)
    session = mgr.session(GROUP, invocation_id="inv-chat", cfg=settings.tools)
    await session.open("train", new=True, description="训练模型")
    slow: list[bool] = []

    async def on_slow() -> None:
        slow.append(True)

    deps = Deps(
        NapCatAPI(bot),  # pyright: ignore[reportArgumentType]
        GROUP,
        USER,
        settings,
        Handles(),
        "chat",
        on_slow_tool=on_slow,
        trigger_message_id=TRIGGER,
        invocation_id="inv-chat",
        request_text="帮我跑一下训练",
    )
    deps.sandbox = session
    script = "echo start\nsleep 3\nprintf '\\033[31mred\\033[0m\\n'\necho 训练完成\nexit 4\n"
    text = str(await job_tools.background(_ctx(deps), script, "训练模型"))
    assert "已在工作区「train」后台开始后台命令" in text, text
    assert slow == [True]
    job = (await runner.for_group(GROUP))[0]
    assert job.status == "running"
    assert job.workspace == "train"
    assert await fake_redis.hget(keep_key(GROUP, "train"), f"job:{job.id}") == str(job.pid)  # pyright: ignore[reportGeneralTypeIssues]

    # The chat invocation ends: its reap must spare the job.
    await session.close()
    await runner.tick(wait=True)
    assert (await runner.get(job.id)).running  # pyright: ignore[reportOptionalMemberAccess]

    listed = str(await job_tools.jobs(_ctx(deps)))
    assert job.id in listed
    assert "运行中" in listed

    done = await _until_reported(runner, job.id)
    assert done.status == "failed"
    assert done.exit_code == 4
    assert "训练完成" in done.result
    assert "\x1b" not in done.result
    assert "red" in done.result
    assert done.report == "done", done
    assert await fake_redis.hget(keep_key(GROUP, "train"), f"job:{job.id}") is None  # pyright: ignore[reportGeneralTypeIssues]

    assert seen
    assert "退出码 4" in seen[0]
    assert "帮我跑一下训练" in seen[0]
    assert "工作区：train（这次回复已经选中它" in seen[0]  # the report runs with the job's workspace selected
    quoted = [d for a, d in bot.calls if a == "send_group_msg"]
    assert quoted
    assert quoted[0]["message"][0] == {"type": "reply", "data": {"id": str(TRIGGER)}}
    assert "训练模型" in quoted[0]["message"][1]["data"]["text"]
    forwards = [d for a, d in bot.calls if a == "send_group_forward_msg"]
    assert forwards
    assert "退出码 4" in json.dumps(forwards[0]["messages"], ensure_ascii=False)
    async with get_session() as s:
        row = (await s.execute(select(Invocation).where(Invocation.group_id == GROUP))).scalar_one()
    assert row.handler == "report"
    assert row.status == "ok"
    assert row.trigger_qq_message_id == TRIGGER
    assert row.user_id == USER
    assert str(row.id) == done.report_invocation
    assert row.models == ["test/report-model"]


async def test_cancel_job_tool_stops_a_command(runner: JobRunner, mgr: WorkspaceManager) -> None:
    settings = Settings.model_validate(SETTINGS_DOC)
    session = mgr.session(GROUP, invocation_id="inv-cancel", cfg=settings.tools)
    await session.open("sleepy", new=True, description="睡觉")
    deps = Deps(None, GROUP, USER, settings, Handles(), "chat", trigger_message_id=TRIGGER)  # pyright: ignore[reportArgumentType]
    deps.sandbox = session
    await job_tools.background(_ctx(deps), "sleep 600", "睡觉")
    job = (await runner.for_group(GROUP))[0]
    poller = asyncio.create_task(runner.run_forever())
    try:
        text = str(await job_tools.cancel_job(_ctx(deps), job.id))
    finally:
        poller.cancel()
        await session.close()
    assert "已取消任务" in text, text
    done = await runner.get(job.id)
    assert done is not None
    assert done.status == "cancelled"
    assert done.report == "skipped"
    found = await mgr.backend_for(GROUP, "sleepy")
    assert found is not None
    try:
        alive = await found[0].exec(["/bin/sh", "-c", f'kill -0 "$(cat {job.dir}/pid)"'], seconds=10)
    finally:
        await found[0].aclose()
    assert alive.exit_code != 0  # the supervisor and its script are gone


@pytest.fixture
def delegate_env(runner: JobRunner, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Relay stubs (the model endpoint is reached directly at 127.0.0.1:8090 inside the sandbox) and a
    recording reporter."""
    seen: dict[str, list[Any]] = {"revoked": [], "issued": [], "reports": []}

    async def models() -> list[relay.RelayModel]:
        return [relay.RelayModel("claude-a", "Claude A", 200000, 64000, True, True)]

    async def issue(**kw: Any) -> str:
        seen["issued"].append(kw)
        return "tok-live"

    async def revoke(job_id: str) -> None:
        seen["revoked"].append(job_id)

    async def usage(job_id: str) -> relay.RelayUsage:
        return relay.RelayUsage(requests=1, input_tokens=10, cached_tokens=0, output_tokens=5)

    async def fake_report(job: Job) -> str | None:
        seen["reports"].append(job)
        return "inv-r"

    monkeypatch.setattr(relay, "relay_url", lambda: "http://127.0.0.1:8090")
    monkeypatch.setattr(relay, "relay_models", models)
    monkeypatch.setattr(relay, "issue_token", issue)
    monkeypatch.setattr(relay, "revoke_token", revoke)
    monkeypatch.setattr(relay, "relay_usage", usage)
    monkeypatch.setattr(runner, "_report", fake_report)
    return seen


async def test_delegate_drives_omp_through_ygg_rpc(
    runner: JobRunner,
    mgr: WorkspaceManager,
    fake_redis: fakeredis.FakeAsyncRedis,
    delegate_env: dict[str, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ``ygg rpc`` host with the fake omp standing in for the binary (the test image has no omp). The
    delegate reuses an existing workspace that is 17 minutes from its hard cap: the start extends it."""
    settings = Settings.model_validate(SETTINGS_DOC)
    earlier = mgr.session(GROUP, invocation_id="inv-earlier", cfg=settings.tools)
    await earlier.open("site", new=True, description="鹈鹕网站")
    peer = "/workspace/.ygg/fake-omp"
    await earlier.backend.write_bytes(peer, b"#!/usr/bin/env python3\n" + FAKE_OMP.read_bytes())
    await earlier.backend.write_bytes("/workspace/site/shot.png", b"\x89PNG fake")
    await earlier.backend.exec(["chmod", "+x", peer])
    await earlier.close()
    monkeypatch.setattr(omp, "OMP_BIN", peer)
    near = time.time() + 17 * 60
    await fake_redis.hset(ws_key(GROUP, "site"), "max_until", repr(near))  # pyright: ignore[reportGeneralTypeIssues]

    session = mgr.session(GROUP, invocation_id="inv-delegate", cfg=settings.tools)
    deps = Deps(None, GROUP, USER, settings, Handles(), "chat", trigger_message_id=TRIGGER)  # pyright: ignore[reportArgumentType]
    deps.sandbox = session
    text = str(await job_tools.delegate(_ctx(deps), "搭个网站并截图", "网站", workspace="site"))
    assert "已选中工作区「site」" in text, text
    assert "已在工作区「site」后台开始后台大任务" in text, text
    job = (await runner.for_group(GROUP))[0]
    info = await mgr.get(GROUP, "site")
    assert info is not None
    assert info.max_until >= job.deadline + settings.tools.sandbox_invocation_s + START_MARGIN_S - 1
    await session.close()

    done = await _until_reported(runner, job.id)
    assert done.status == "succeeded", done.error
    assert done.result.startswith("完成了。host_1:已记下这张图")
    assert done.images == [{"path": "/workspace/site/shot.png", "caption": "首页截图"}]
    assert done.progress == "做到一半"
    assert delegate_env["revoked"] == [job.id]
    assert [j.id for j in delegate_env["reports"]] == [job.id]
    found = await mgr.backend_for(GROUP, "site")
    assert found is not None
    try:
        models_yml = await found[0].read_bytes(omp.MODELS_PATH)
        exit_info = await found[0].read_bytes(f"{job.dir}/exit")
    finally:
        await found[0].aclose()
    assert json.loads(models_yml)["providers"]["ygg"]["models"][0]["id"] == "claude-a"
    assert json.loads(exit_info)["exit"] == 0  # closed stdin, drained, exited by itself


@pytest.mark.skipif(
    not os.environ.get("YGG_TEST_SANDBOXD_OMP"),
    reason="set YGG_TEST_SANDBOXD_OMP=1 when the sandboxd runs the full workspace image (ygg-sbx-full:dev, with omp)",
)
async def test_delegate_with_real_omp(
    runner: JobRunner, mgr: WorkspaceManager, delegate_env: dict[str, list[Any]]
) -> None:
    """The real omp binary with our launch flags, models.yml, setup frames and host tools; the model is a
    scripted Anthropic endpoint inside the sandbox (tests/sandbox/fake_anthropic.py) that calls ``show``."""
    settings = Settings.model_validate(SETTINGS_DOC)
    session = mgr.session(GROUP, invocation_id="inv-omp", cfg=settings.tools)
    await session.open("shots", new=True, description="截图")
    log = "/workspace/.ygg/fake-anthropic.log"
    await session.backend.write_bytes("/workspace/.ygg/fake_anthropic.py", FAKE_ANTHROPIC.read_bytes())
    await session.backend.write_bytes("/workspace/site/shot.png", b"\x89PNG fake")
    deps = Deps(None, GROUP, USER, settings, Handles(), "chat", trigger_message_id=TRIGGER)  # pyright: ignore[reportArgumentType]
    deps.sandbox = session
    text = str(await job_tools.delegate(_ctx(deps), "给首页截个图，用 show 发出来", "截图"))
    assert "已在工作区「shots」后台开始后台大任务" in text, text
    await session.close()  # the invocation's reap; the model server starts after it so it is not reaped
    found = await mgr.backend_for(GROUP, "shots")
    assert found is not None
    backend = found[0]
    try:
        await backend.exec(
            [
                "/bin/sh",
                "-c",
                'rm -f "$2"; setsid python3 /workspace/.ygg/fake_anthropic.py 8090 "$2" </dev/null >/dev/null 2>&1 &',
                "sh",
                "8090",
                log,
            ]
        )
        job = (await runner.for_group(GROUP))[0]
        done = await _until_reported(runner, job.id, seconds=180)
        requests = [json.loads(x) for x in (await backend.read_bytes(log)).decode().splitlines()]
        stderr = (await backend.read_bytes(f"{job.dir}/stderr.log")).decode("utf-8", "replace")
        await backend.exec(["/bin/sh", "-c", "pkill -f fake_anthropic.py; true"])
    finally:
        await backend.aclose()
    assert done.status == "succeeded", f"{done.error}\nstderr:\n{stderr[-3000:]}"
    assert done.result == "完成了：页面已截图。"
    assert done.images == [{"path": "/workspace/site/shot.png", "caption": "首页"}]
    assert len(requests) >= 2
    assert requests[0]["authorization"] == "Bearer tok-live" or requests[0]["x-api-key"] == "tok-live"
    assert requests[0]["model"] == "claude-a"
    tools = set(requests[0]["tools"])
    assert {"show", "progress", "bash", "read", "eval"} <= tools, tools  # host tools are top level (essential)
    assert "task" not in tools
    assert "web_search" not in tools
