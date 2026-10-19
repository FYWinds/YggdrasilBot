"""Background job tools for chat (part of the sandbox suite): ``background``, ``delegate``, ``jobs``,
``cancel_job``. The jobs themselves live in ``yggdrasil.sandbox.jobs``; a finished job reports back through a
synthetic chat invocation (``yggdrasil.pipeline.report``), which keeps only ``jobs``."""

import time
from collections.abc import Awaitable, Callable

from pydantic_ai import RunContext, Tool

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools import ToolResult, _tool
from yggdrasil.sandbox import SandboxError, Session
from yggdrasil.sandbox.jobs import KIND_TEXT, STATUS_TEXT, Job, Requester, fmt_duration, relay_enabled, runner
from yggdrasil.tools.errors import ToolError

JOB_CONTROL_TOOLS = frozenset({"background", "delegate", "cancel_job"})
"""Tools a report invocation must not get (it may only look at jobs)."""
_CANCEL_WAIT_S = 20.0


def _session(ctx: RunContext[Deps]) -> Session:
    if ctx.deps.sandbox is None or ctx.deps.group_id is None:
        raise ToolError("这次没有可用的工作区")
    return ctx.deps.sandbox


def _requester(deps: Deps) -> Requester:
    assert deps.group_id is not None
    return Requester(
        group_id=deps.group_id,
        user_id=deps.user_id,
        trigger_message_id=deps.trigger_message_id,
        invocation_id=deps.invocation_id,
        request_text=deps.request_text,
    )


def _started(job: Job) -> str:
    minutes = int((job.deadline - job.created_at) // 60)
    return (
        f"已在工作区「{job.workspace}」后台开始{KIND_TEXT[job.kind]} {job.id}「{job.title}」，最长 {minutes} 分钟"
        "（工作区会保留到它结束）。结束后会自动在群里回复请求者结果，不用等它。"
        "现在直接回复群友：任务已经开始、大概要做什么、做完会自动通知；不要编造结果。"
    )


@_tool("background")
async def background(ctx: RunContext[Deps], script: str, title: str) -> ToolResult:
    """在当前选中的工作区后台运行一段 bash 脚本，不等它结束：用于要跑几分钟以上的单个长命令（训练、批量下载、
    长测试、大文件转换）。脚本在 /workspace 下执行，输出记到文件里；结束后会自动在群里把结果（退出码和输出的
    头尾）回复给请求者。title 是一句话的任务名（≤20 字）。几十秒能跑完的命令直接用 sh，不要用这个。"""
    session = _session(ctx)
    if not script.strip():
        raise ToolError("脚本不能为空")
    try:
        name = await session.require()
        job = await runner().start_command(
            _requester(ctx.deps), session.backend, workspace=name, script=script, title=title
        )
    except SandboxError as e:
        raise ToolError(str(e)) from e
    if ctx.deps.on_slow_tool is not None:
        await ctx.deps.on_slow_tool()
    return _started(job)


@_tool("delegate")
async def delegate(
    ctx: RunContext[Deps], task: str, title: str, workspace: str | None = None, new: bool = False, description: str = ""
) -> ToolResult:
    """把一个需要多步完成的大任务交给工作区里的编程 agent 在后台独立去做（它有命令行、读写文件、带浏览器的
    Python，能自己验证效果），适合「搭个网站并测好」「把这个仓库跑起来修好」这类要几分钟到十几分钟的活。
    task 要写清楚：目标、要用的文件或数据在哪、验收标准、交付物（图、公开链接、文件）。它看不到聊天记录，
    也没法向你提问；它的环境和你的 py kernel 不同，task 里只写要什么结果，不要指定函数、工具或端口。
    title 是一句话的任务名（≤20 字）。结束后会自动在群里把结果回复给请求者。
    workspace 选在哪个工作区做，用法同 open_workspace：现有的名字就接着用它（同一话题的后续活），配合
    new=True 和 description 就新建一个；不填用当前选中的工作区。工作区会保留到任务结束。
    一两分钟内能自己用 sh / py 做完的事不要委托。"""
    session = _session(ctx)
    if not task.strip():
        raise ToolError("任务描述不能为空")
    notes: list[str] = []
    try:
        if workspace is not None and workspace.strip():
            opened = await session.open(workspace.strip().lower(), new=new, description=description)
            notes.append(await session.summary(opened, files=False))
        elif new:
            raise ToolError("new=True 时要用 workspace 写新工作区的名字")
        name = await session.require()
        job = await runner().start_agent(_requester(ctx.deps), session.backend, workspace=name, task=task, title=title)
    except SandboxError as e:
        raise ToolError("\n".join([*notes, str(e)])) from e
    if ctx.deps.on_slow_tool is not None:
        await ctx.deps.on_slow_tool()
    return "\n".join([*notes, _started(job)])


def describe_job(job: Job, *, now: float, detail: bool = False) -> str:
    """One job for the model: id, kind, status, timing, progress; ``detail`` adds the result excerpt."""
    head = f"- {job.id} {KIND_TEXT[job.kind]}「{job.title}」（工作区 {job.workspace}）{STATUS_TEXT[job.status]}"
    if job.running:
        head += f"，已运行 {fmt_duration(now - job.created_at)}，最多还剩 {fmt_duration(job.deadline - now)}"
        if job.cancel:
            head += "（正在取消）"
    elif job.ended_at is not None:
        head += f"，用时 {fmt_duration(job.ended_at - job.created_at)}，{fmt_duration(now - job.ended_at)}前结束"
    lines = [head]
    if job.progress and job.running:
        lines.append(f"  进度：{job.progress}")
    if detail and not job.running:
        if job.error:
            lines.append(f"  原因：{job.error[:300]}")
        if job.result:
            lines.append(f"  结果摘要：{job.result[:300]}")
    for link in job.links:
        lines.append(f"  公开链接：{link.get('url')}（端口 {link.get('port')}）")
    return "\n".join(lines)


@_tool("jobs")
async def jobs(ctx: RunContext[Deps]) -> ToolResult:
    """查看本群的后台任务（运行中的和最近结束的）：编号、状态、用时、进度。"""
    if ctx.deps.group_id is None:
        raise ToolError("只有群聊有后台任务")
    items = await runner().for_group(ctx.deps.group_id, limit=8)
    if not items:
        return "本群最近没有后台任务"
    now = time.time()
    return "\n".join(describe_job(j, now=now, detail=True) for j in items)


@_tool("cancel_job")
async def cancel_job(ctx: RunContext[Deps], job_id: str) -> ToolResult:
    """取消本群一个正在运行的后台任务（job_id 是任务编号，可以先用 jobs 查）。群友要求停掉、取消时用。
    取消后不会再自动回报，这次回复里直接告诉群友已经停了。"""
    group_id = ctx.deps.group_id
    if group_id is None:
        raise ToolError("只有群聊有后台任务")
    r = runner()
    job = await r.cancel(job_id.strip().lower(), by=f"chat:{ctx.deps.user_id}", group_id=group_id)
    if job is None:
        raise ToolError(f"本群没有编号为 {job_id} 的后台任务")
    if not job.running:
        return f"任务 {job.id} 已经结束了（{STATUS_TEXT[job.status]}），不用取消"
    done = await r.wait_finished(job.id, within=_CANCEL_WAIT_S)
    if done is not None and not done.running:
        return f"已取消任务 {job.id}「{job.title}」。\n" + describe_job(done, now=time.time(), detail=True)
    return f"已请求取消任务 {job.id}「{job.title}」，几秒内会停下。"


def job_tools() -> list[Tool[Deps]]:
    """The job tools of the sandbox suite; ``delegate`` only when the LLM relay is configured. ``delegate`` may
    switch the selected workspace, so it runs alone like ``open_workspace``."""
    fns: list[Callable[..., Awaitable[ToolResult]]] = [background, jobs, cancel_job]
    tools: list[Tool[Deps]] = [Tool(fn, takes_ctx=True) for fn in fns]
    if relay_enabled():
        tools.insert(1, Tool(delegate, takes_ctx=True, sequential=True))
    return tools
