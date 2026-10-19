"""The report of a finished background job: a synthetic chat invocation that replies to the requester.

It runs on the chat route with the chat prompt and tools (the sandbox suite, minus the tools that start or
cancel jobs), so the model can look into the workspace before answering, and goes through the usual
moderation, concurrency slots, token budget, delivery and invocation record. The job's images (omp's
``show``) are attached up front, after image moderation; its public links and result are in the prompt.
"""

import secrets

from nonebot.adapters.onebot.v11 import Bot
from pydantic_ai import Tool
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.usage import RunUsage

from yggdrasil.agent import sandbox_tools
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.job_tools import JOB_CONTROL_TOOLS, jobs
from yggdrasil.agent.schema import BotReply
from yggdrasil.agent.tools import MAX_IMAGES, tool_names, tools_for
from yggdrasil.bot.state import any_bot, identity
from yggdrasil.core.log import LOGGER
from yggdrasil.delivery.send import send_text
from yggdrasil.llm.registry import TaskNotConfigured, chat_model
from yggdrasil.llm.streaming import RunStats
from yggdrasil.napcat.api import NapCatAPI
from yggdrasil.people import member_label
from yggdrasil.pipeline import ratelimit
from yggdrasil.pipeline.invoke import Invocator
from yggdrasil.runtime import snapshot
from yggdrasil.sandbox import SandboxError, manager
from yggdrasil.sandbox.jobs import KIND_TEXT, STATUS_TEXT, Job, ReportDeferred, fmt_duration
from yggdrasil.settings import Settings
from yggdrasil.tools.errors import ToolError

_RESULT_CHARS = 12000


async def run_report(job: Job) -> str | None:
    """Report ``job`` to its requester; returns the invocation id. Raises ``ReportDeferred`` when it cannot
    start yet (no bot connection, the requester's queue is full)."""
    bot = any_bot()
    if bot is None:
        raise ReportDeferred("no bot connection")
    snap = snapshot()
    if not snap.group_enabled(job.group_id):
        LOGGER.info("sandbox job {}: group {} is disabled, no report", job.id, job.group_id)
        return None
    settings = snap.settings(job.group_id)
    api = NapCatAPI(bot)
    if await ratelimit.tokens_exhausted(job.group_id, settings.ratelimit):
        # No model call past the group's daily budget: a plain notice still closes the loop.
        await _notice(bot, api, job, settings)
        await ratelimit.add_tokens(job.group_id, _relay_tokens(job))
        return None
    async with ratelimit.slot(job.group_id, job.user_id, settings.ratelimit) as granted:
        if not granted:
            raise ReportDeferred("requester's queue is full")
        inv = ReportInvocator(bot, api, job, settings)
        await inv.run()
        return str(inv.invocation_id) if inv.invocation_id is not None else None


def _relay_tokens(job: Job) -> int:
    return job.usage.get("input_tokens", 0) + job.usage.get("output_tokens", 0)


def _headline(job: Job) -> str:
    return f"后台任务「{job.title}」{STATUS_TEXT[job.status]}"


async def _notice(bot: Bot, api: NapCatAPI, job: Job, settings: Settings) -> None:
    ident = await identity(bot)
    text = f"{_headline(job)}（今天的用量已到上限，不再生成详细说明）。"
    for link in job.links:
        text += f"\n{link.get('url')}"
    await send_text(
        api,
        group_id=job.group_id,
        user_id=job.user_id,
        reply_to=job.trigger_message_id,
        text=text,
        self_id=ident.self_id,
        nickname=ident.nickname,
    )


class ReportInvocator(Invocator):
    """A chat run without a triggering event: replies to the job's trigger message."""

    def __init__(self, bot: Bot, api: NapCatAPI, job: Job, settings: Settings) -> None:
        self._setup(
            bot,
            api,
            "chat",
            settings,
            job.group_id,
            user_id=job.user_id,
            trigger_message_id=job.trigger_message_id or 0,
            role="member",
            record_handler="report",
        )
        self.job = job
        self._relay_counted = False

    async def _run(self) -> str:
        job = self.job
        ident = await identity(self.bot)
        sandbox_on = sandbox_tools.suite_enabled(
            self.settings,
            "chat",
            self.group_id,
            global_settings=snapshot().settings(),
            configured=manager().configured,
        )
        await self._start_row(f"[后台任务 {job.id}] {job.title}：{STATUS_TEXT[job.status]}")
        try:
            resolved_model = chat_model("chat")
        except TaskNotConfigured:
            LOGGER.warning("chat has no model route; job {} is not reported", job.id)
            return "failed"
        deps = Deps(
            self.api,
            self.group_id,
            self.user_id,
            self.settings,
            Handles(),
            "chat",
            self_id=ident.self_id,
            trigger_message_id=job.trigger_message_id,
            invocation_id=str(self.invocation_id) if self.invocation_id is not None else None,
            request_text=job.request_text,
        )
        tools_cfg = self.settings.tools
        tool_list = tool_names(tools_cfg.enabled, "chat")
        prompt_tools = list(tool_list)
        extra_tools: list[Tool[Deps]] = [Tool(jobs, takes_ctx=True)]
        capabilities: list[AbstractCapability[Deps]] = []
        if sandbox_on and self.group_id is not None:
            # The job's workspace is pre-selected so the model can look at what the job left there.
            self.sandbox = deps.sandbox = manager().session(
                self.group_id, invocation_id=deps.invocation_id, cfg=tools_cfg, workspace=job.workspace
            )
            extra_tools = [t for t in sandbox_tools.bot_tools() if t.name not in JOB_CONTROL_TOOLS]
            prompt_tools += list(sandbox_tools.HARNESS_TOOL_NAMES)
            capabilities = sandbox_tools.capabilities()
        prompt_tools += [t.name for t in extra_tools]
        image_notes = await self._attach_images(deps)
        nonce = secrets.token_hex(3)
        requester = await member_label(self.group_id, self.user_id)
        prompt = build_prompt(job, nonce=nonce, requester=requester, image_notes=image_notes)
        agent = self._agent(
            resolved_model,
            tools=tools_for(tool_list) + extra_tools,
            capabilities=capabilities,
            prompt_tools=prompt_tools,
            actions=[],
            recall_denied=False,
            nonce=nonce,
            report=True,
        )
        mod_text = f"{job.request_text}\n{job.result[:4000]}"
        return await self._execute(agent, prompt, deps, resolved_model, mod_text)

    async def _attach_images(self, deps: Deps) -> list[str]:
        """Moderate and attach the images the job showed; returns notes about the ones that were dropped."""
        if not self.job.images:
            return []
        notes: list[str] = []
        found = await manager().backend_for(self.job.group_id, self.job.workspace) if manager().configured else None
        if found is None or found[1] != self.job.claim:
            if found is not None:
                await found[0].aclose()
            return ["任务给出的图片随工作区一起没了，发不出来。"]
        backend = found[0]
        try:
            for item in self.job.images[:MAX_IMAGES]:
                path, caption = item.get("path", ""), item.get("caption", "")
                try:
                    blob = await backend.read_bytes(path)
                    await sandbox_tools.attach_image(deps, blob, caption)
                except ToolError as e:
                    notes.append(f"{path}：{e}")
                except (OSError, SandboxError) as e:
                    notes.append(f"{path} 读不出来（{e}）")
        finally:
            await backend.aclose()
        return notes

    def _account(self, model_label: str, usage: RunUsage, deps: Deps, stats: RunStats) -> None:
        super()._account(model_label, usage, deps, stats)
        if self.job.usage and not self._relay_counted:  # what omp spent through the relay belongs here too
            self._relay_counted = True
            self.meta.models.append(f"relay/{self.job.model}")
            self.meta.tokens_in += self.job.usage.get("input_tokens", 0)
            self.meta.tokens_out += self.job.usage.get("output_tokens", 0)
            self.meta.tokens_cached += self.job.usage.get("cached_tokens", 0)

    def _billable_tokens(self) -> int:
        # The relay tokens are billed with the report even when it never reached the model.
        return super()._billable_tokens() + (0 if self._relay_counted else _relay_tokens(self.job))

    async def _status(self, emoji: str) -> bool:
        # The trigger message already carries the original run's reactions; adding them again only looks
        # automated. The report's outcome is in the invocation record.
        return False

    async def deliver(self, reply: BotReply) -> None:
        # A forward message cannot quote: a short quoted line first tells the requester which request this is.
        if self.job.trigger_message_id:
            ident = await identity(self.bot)
            await send_text(
                self.api,
                group_id=self.group_id,
                user_id=self.user_id,
                reply_to=self.job.trigger_message_id,
                text=f"{_headline(self.job)}，结果见下面。",
                self_id=ident.self_id,
                nickname=ident.nickname,
            )
        await super().deliver(reply)


def build_prompt(job: Job, *, nonce: str, requester: str, image_notes: list[str]) -> str:
    """The report's user prompt: the job and its outcome as data blocks."""

    def block(tag: str, body: str) -> str:
        name = f"{tag}-{nonce}"
        return f"<{name}>\n{body.replace(nonce, '*' * len(nonce))}\n</{name}>"

    took = fmt_duration((job.ended_at or job.created_at) - job.created_at)
    facts = [
        f"编号：{job.id}",
        f"类型：{KIND_TEXT[job.kind]}",
        f"标题：{job.title}",
        f"状态：{STATUS_TEXT[job.status]}（用时 {took}）",
        f"请求者：{requester}",
        f"工作区：{job.workspace}（这次回复已经选中它，工作区工具直接作用在它上面）",
    ]
    if job.error:
        facts.append(f"说明：{job.error}")
    if job.usage:
        facts.append(
            f"模型用量：{job.usage.get('requests', 0)} 次请求，输入 {job.usage.get('input_tokens', 0)} token，"
            f"输出 {job.usage.get('output_tokens', 0)} token"
        )
    parts = [
        block("原始请求", job.request_text or "（无）"),
        block("后台任务", "\n".join(facts)),
        block("任务内容", job.spec[:4000]),
    ]
    result = job.result or "（没有输出）"
    if len(result) > _RESULT_CHARS:
        result = result[:_RESULT_CHARS] + "\n…（后面省略）"
    parts.append(block("任务结果" if job.kind == "command" else "任务的最终回答", result))
    if job.links:
        parts.append(block("公开链接", "\n".join(f"{x.get('url')}（端口 {x.get('port')}）" for x in job.links)))
    attached = len(job.images) - len(image_notes) if job.images else 0
    notes = []
    if attached > 0:
        notes.append(f"任务给出的 {attached} 张图已经附在这次回复的合并转发里，正文不用再描述图里的内容。")
    notes += image_notes
    notes.append(
        "这是之前接下的后台任务的回报，不是新的请求：用 mode=forward 把结果告诉请求者，"
        "说清做成了什么、在哪里（路径、链接）、没完成什么；失败、超时或取消时说明原因和下一步建议。"
    )
    if job.kind == "command":
        notes.append(f"完整输出在工作区「{job.workspace}」的 {job.dir}/out，需要细看时可以读它。")
    return "\n\n".join([*parts, *notes])
