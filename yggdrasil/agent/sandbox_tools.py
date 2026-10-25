"""The sandbox workspace suite for chat: ``open_workspace`` (choose the group's per-topic workspace), ``sh`` /
``py`` (big actions run by the in-sandbox ``/opt/ygg/ygg`` helper), the harness ``ws_read_file`` /
``ws_edit_file``, and the bot's own workspace tools.

A group has a few named workspaces, one per topic (``yggdrasil.sandbox``). The model selects one with
``open_workspace`` (reuse or create); the selected one is attached lazily by the backend on the first command or
file operation, so a run that never touches it costs nothing, and using a workspace tool before selecting one
fails with the group's list. The suite is switched on per group by the pseudo tool name ``sandbox`` in
``tools.enabled`` and globally by ``tools.sandbox_enabled`` (global settings only). Harness file tools get the
``ws_`` prefix: the bot's own ``read_file`` reads QQ attachments.

``sh`` / ``py`` write their input into ``/workspace/.ygg/`` over REST and run one ``Execute`` of the helper,
which prints a single JSON line. ``py`` side effects (``show`` / ``publish`` / ``unpublish`` in the kernel) are
only requests in the cell's ``effects.jsonl``; the bot carries them out after the cell, with its own image
moderation and link objects, and appends the outcome to the tool result.
"""

import asyncio
import io
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from PIL import Image
from pydantic_ai import RunContext, Tool, ToolReturn
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.capabilities.abstract import ValidatedToolArgs, WrapToolExecuteHandler
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.workspaces import WorkspaceError
from pydantic_ai_harness import FileSystem

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.media import ModelImage, fit_image, media_bytes, model_image
from yggdrasil.agent.tools import MAX_IMAGES, ToolResult, _handle, _tool, _vision, reserve_images, sandbox_requested
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import TOOL_CALLS, TOOL_DURATION
from yggdrasil.llm.registry import TaskNotConfigured, sees_images
from yggdrasil.moderation.image import ImageVerdict, check_image
from yggdrasil.sandbox import SandboxError, Session
from yggdrasil.settings import Settings, ToolSettings
from yggdrasil.tools.errors import ToolError

WS_PREFIX = "ws"
_FS_TOOLS = ("read_file", "edit_file")
HARNESS_TOOL_NAMES = tuple(f"{WS_PREFIX}_{name}" for name in _FS_TOOLS)

YGG = "/opt/ygg/ygg"
SCRATCH = "/workspace/.ygg"
IMAGE_MAX_BYTES = 10 * 1024 * 1024
PY_MAX_IMAGES = 4
"""Images one ``py`` call hands back to the model (display / matplotlib / shot)."""
_GRACE_S = 45.0
"""Extra gRPC deadline over the helper's own timeout: kernel start (up to 30 s), interrupt grace, kill, JSON."""
_RESULT_CHARS = 4000
_TRACEBACK_CHARS = 4000


def suite_enabled(
    settings: Settings, handler: str, group_id: int | None, *, global_settings: Settings, configured: bool
) -> bool:
    """Whether this run gets the sandbox suite: group chat runs only, when the group's ``tools.enabled``
    names ``sandbox``, the global kill switch is on and the deployment has a sandbox configured."""
    return (
        group_id is not None
        and sandbox_requested(settings.tools.enabled, handler)
        and global_settings.tools.sandbox_enabled
        and configured
    )


# --- harness capabilities -------------------------------------------------------


@dataclass
class _HarnessAccounting(AbstractCapability[Deps]):
    """Tool counts, wall time, metrics and the slow-tool reaction for harness tools, which bypass ``_tool``."""

    async def wrap_tool_execute(
        self,
        ctx: RunContext[Deps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        handler: WrapToolExecuteHandler,
    ) -> Any:
        name = call.tool_name
        if name not in HARNESS_TOOL_NAMES:
            return await handler(args)
        deps = ctx.deps
        deps.tool_counts[name] += 1
        if deps.on_slow_tool is not None and name in deps.settings.tools.slow_tools:
            await deps.on_slow_tool()
        started = time.perf_counter()
        outcome = "ok"
        try:
            return await handler(args)
        except Exception:
            outcome = "error"
            raise
        finally:
            elapsed = time.perf_counter() - started
            deps.tool_seconds[name] += elapsed
            TOOL_CALLS.add(1, {"tool": name, "outcome": outcome})
            TOOL_DURATION.record(elapsed, {"tool": name})


def capabilities() -> list[AbstractCapability[Deps]]:
    """Accounting plus the harness file tools, limited to reading and exact edits (everything else is ``sh``)."""
    return [
        _HarnessAccounting(),
        FileSystem[Deps](tools=_FS_TOOLS, max_read_chars=20_000).prefix_tools(WS_PREFIX),
    ]


# --- helpers ----------------------------------------------------------------------


def _session(ctx: RunContext[Deps]) -> Session:
    session = ctx.deps.sandbox
    if session is None:
        raise ToolError("这次没有可用的工作区")
    return session


@contextmanager
def _sandbox_errors() -> Iterator[None]:
    try:
        yield
    except SandboxError as e:
        raise ToolError(str(e)) from e
    except WorkspaceError as e:
        raise ToolError(f"工作区出错：{e}") from e


_UNSAFE_NAME = re.compile(r"[^\w.\-]+")


def safe_name(name: str, fallback: str) -> str:
    """A plain file name for ``/workspace/in/``: no directories, no shell-hostile characters."""
    base = _UNSAFE_NAME.sub("_", PurePosixPath(name.replace("\\", "/")).name).strip("._")[:120]
    return base or fallback


def _step_dir(kind: str) -> str:
    """A fresh scratch directory for one ``sh`` script or ``py`` cell (sortable by time)."""
    return f"{SCRATCH}/{kind}/{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def _helper_json(stdout: bytes, stderr: bytes, exit_code: int, what: str) -> dict[str, Any]:
    """The helper's one JSON line; ``ToolError`` with its stderr when it failed or printed something else."""
    lines = stdout.decode("utf-8", "replace").strip().splitlines()
    if exit_code == 0 and lines:
        try:
            data = json.loads(lines[-1])
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data
    detail = (stderr.decode("utf-8", "replace").strip() or stdout.decode("utf-8", "replace").strip())[-1000:]
    raise ToolError(f"工作区的 {what} 执行器出错（退出码 {exit_code}）：{detail or '没有输出'}")


def _timeout(cfg: ToolSettings, timeout: int | None) -> float:
    return float(cfg.sandbox_command_timeout_s if timeout is None or timeout <= 0 else timeout)


def _clip(text: str, limit: int, *, tail: bool = False) -> str:
    if len(text) <= limit:
        return text
    return "…" + text[-limit:] if tail else text[:limit] + "…"


async def _run_helper(
    session: Session, argv: Callable[[float], list[str]], *, wanted: float, label: str
) -> tuple[dict[str, Any], float, list[str]]:
    """Admit one command against the invocation budget, run the helper with the granted timeout and decode
    its JSON. Returns (result, granted seconds, notices for the model: attach notice, timeout cut)."""
    backend = session.backend
    with _sandbox_errors():
        granted, note = await backend.admit(wanted)
        seconds = wanted if granted is None else granted
        notices = [n for n in (backend.take_notice(), note) if n]
        # once: a lost connection after sending must not run the script or cell a second time.
        result = await backend.exec(argv(seconds), seconds=seconds + _GRACE_S, once=True, audit=label)
    return _helper_json(result.stdout, result.stderr, result.exit_code, label.split(" ", 1)[0]), seconds, notices


# --- sh -----------------------------------------------------------------------------


def format_sh(result: dict[str, Any], seconds: float) -> str:
    """The model-facing text of a ``ygg sh run`` result."""
    duration = float(result.get("duration") or 0.0)
    code = result.get("exit")
    if result.get("timed_out"):
        head = f"[超时：运行超过 {seconds:g} 秒，已终止]"
    elif code is None:
        head = f"[被信号终止，用时 {duration:.1f} 秒]"
    else:
        head = f"[退出码 {code}，用时 {duration:.1f} 秒]"
    output = str(result.get("output") or "").rstrip()
    parts = [head, output or "（没有输出）"]
    if result.get("truncated"):
        parts.append(
            f"[输出共 {result.get('output_bytes')} 字节，只显示了开头和结尾；完整输出在 {result.get('out_path')}，"
            "需要时用 ws_read_file 分段读]"
        )
    return "\n".join(parts)


@_tool("sh")
async def sh(ctx: RunContext[Deps], script: str, timeout: int | None = None) -> str:  # noqa: ASYNC109 - model-facing
    """在当前选中的工作区里用 bash 运行一整段脚本（工作目录 /workspace），返回退出码和输出（stdout、stderr 合在一起，
    太长时保留头尾）。
    一次调用把事情做完：用 heredoc 写文件、运行、检查结果都写进同一段脚本，不要一步一调。
    timeout 是秒数，不填用默认值；更久的命令改用 background。脚本里用 & 放到后台的进程在这次回复结束时会被清理，
    要常驻的服务用 py 里的 start()。"""
    session = _session(ctx)
    if not script.strip():
        raise ToolError("脚本是空的")
    step = _step_dir("sh")
    with _sandbox_errors():
        await session.backend.write_bytes(f"{step}/script.sh", script.encode("utf-8"))
    result, seconds, notices = await _run_helper(
        session,
        lambda s: [YGG, "sh", "run", step, "--timeout", f"{s:g}"],
        wanted=_timeout(ctx.deps.settings.tools, timeout),
        label=f"sh {step}: {script[:400]}",
    )
    return "\n".join([*notices, format_sh(result, seconds)])


# --- py -----------------------------------------------------------------------------


def format_py(result: dict[str, Any], seconds: float, *, restarted: bool) -> str:
    """The model-facing text of a ``ygg py run`` result (without the effects and image notes)."""
    status = result.get("status")
    duration = float(result.get("duration") or 0.0)
    kernel = result.get("kernel") or {}
    parts: list[str] = []
    if restarted:
        parts.append("[已按要求重启 kernel，之前的变量和导入都清空了]")
    elif kernel.get("previous") == "dead":
        parts.append(
            "[之前的 kernel 已经退出（崩溃、内存不够或闲置太久），这次启动了新的 kernel；之前的变量和导入都没了]"
        )
    if status == "ok":
        parts.append(f"[完成，用时 {duration:.1f} 秒]")
    elif status == "error":
        parts.append(f"[出错，用时 {duration:.1f} 秒]")
    elif status == "timeout":
        parts.append(f"[超时：运行超过 {seconds:g} 秒，已中断；kernel 还在，变量保留]")
    else:
        parts.append("[kernel 已终止，变量和导入都没了，下次 py 会启动新的 kernel；原因见下面的报错]")
    body = len(parts)
    output = str(result.get("output") or "").rstrip()
    if output:
        parts.append(output)
        if result.get("output_truncated"):
            parts.append("[输出太长，中间部分省略了]")
    if (value := result.get("result")) is not None:
        parts.append(f"结果：{_clip(str(value), _RESULT_CHARS)}")
    if error := result.get("error"):
        trace = str(error.get("traceback") or "").rstrip()
        summary = f"{error.get('ename', 'Error')}: {error.get('evalue', '')}"
        parts.append(_clip(trace, _TRACEBACK_CHARS, tail=True) if trace else summary)
        if trace and summary not in trace:
            parts.append(summary)
    if len(parts) == body:
        parts.append("（没有输出）")
    return "\n".join(parts)


async def _apply_effects(ctx: RunContext[Deps], session: Session, effects: list[Any]) -> list[str]:
    """Carry out the cell's ``show`` / ``publish`` / ``unpublish`` requests; one line per outcome."""
    lines: list[str] = []
    for effect in effects:
        if not isinstance(effect, dict):
            continue
        kind = effect.get("type")
        try:
            if kind == "show":
                path = str(effect.get("path") or "")
                with _sandbox_errors():
                    blob = await session.get_bytes(path, max_bytes=IMAGE_MAX_BYTES)
                lines.append(f"show：{await attach_image(ctx.deps, blob, str(effect.get('caption') or ''))}")
            elif kind == "publish":
                port = int(effect.get("port") or 0)
                with _sandbox_errors():
                    link = await session.publish(port)
                lines.append(f"publish：端口 {port} 的公开链接 {link.url}（任何人都能打开，回复里直接给出链接）")
            elif kind == "unpublish":
                port = int(effect.get("port") or 0)
                with _sandbox_errors():
                    closed = await session.unpublish(port)
                lines.append(
                    f"unpublish：端口 {port} 的链接已撤销" if closed else f"unpublish：端口 {port} 没有公开的链接"
                )
        except ToolError as e:
            lines.append(f"{kind} 失败：{e}")
        except (TypeError, ValueError) as e:
            lines.append(f"{kind} 失败：参数不对（{e}）")
    return lines


async def _images(session: Session, paths: list[Any]) -> list[ModelImage]:
    out: list[ModelImage] = []
    for path in paths[:PY_MAX_IMAGES]:
        try:
            with _sandbox_errors():
                blob = await session.get_bytes(str(path), max_bytes=IMAGE_MAX_BYTES)
            out.append(await asyncio.to_thread(model_image, blob))
        except ToolError as e:
            LOGGER.warning("py image {} unreadable: {}", path, e)
    return out


@_tool("py")
async def py(
    ctx: RunContext[Deps],
    code: str,
    timeout: int | None = None,  # noqa: ASYNC109 - model-facing argument
    restart: bool = False,
) -> ToolResult:
    """在当前工作区的常驻 Python kernel（IPython，工作目录 /workspace）里执行代码；变量、导入、打开的浏览器
    在多次调用之间保留，支持顶层 await。返回输出、最后一个表达式的值和报错 traceback；display() 的图、
    matplotlib 图和 shot() 截图会作为图片直接返回给你看。
    已预置：show(图/路径, caption) 把图发到群里；publish(port)/unpublish(port) 开关公开链接；
    start(cmd) 起常驻的后台服务，wait_port(port) 等它监听；page = await browser() 是 Playwright 页面，
    await shot() 截图，await snapshot() 读页面结构。
    restart=True 先重启 kernel（清空变量）。timeout 是秒数，不填用默认值。"""
    session = _session(ctx)
    if not code.strip():
        raise ToolError("代码是空的")
    if restart:
        with _sandbox_errors():
            await session.backend.exec([YGG, "py", "restart"], seconds=30)
    cell = _step_dir("cells")
    with _sandbox_errors():
        await session.backend.write_bytes(f"{cell}/code.py", code.encode("utf-8"))
    result, seconds, notices = await _run_helper(
        session,
        lambda s: [YGG, "py", "run", cell, "--timeout", f"{s:g}", "--max-images", str(PY_MAX_IMAGES)],
        wanted=_timeout(ctx.deps.settings.tools, timeout),
        label=f"py {cell}: {code[:400]}",
    )
    kernel = result.get("kernel") or {}
    pid = kernel.get("pid")
    alive = result.get("status") != "died" and type(pid) is int
    await session.keep_kernel(pid if alive else None)

    parts = [*notices, format_py(result, seconds, restarted=restart)]
    parts += await _apply_effects(ctx, session, list(result.get("effects") or []))
    images = await _images(session, list(result.get("images") or []))
    dropped = int(result.get("images_dropped") or 0) + max(0, len(result.get("images") or []) - PY_MAX_IMAGES)
    if dropped:
        parts.append(f"[另有 {dropped} 张图没有返回（每次最多 {PY_MAX_IMAGES} 张）]")
    if not images:
        return "\n".join(parts)
    if sees_images(ctx.deps.task):
        parts.append(
            f"[这次运行产生的 {len(images)} 张图附在下一条消息里，直接看图；这些图不会发到群里，要发用 show()]"
        )
        content: list[Any] = []
        for i, img in enumerate(images, 1):
            content += [f"图 {i}{img.note}：", img.content]
        return ToolReturn(return_value="\n".join(parts), content=content)
    try:
        described = await _vision(
            ctx, images, "这些是代码运行产生的图（截图或图表）。逐张详细描述画面内容、布局，并完整转写其中的文字。"
        )
        parts.append(
            f"[这次运行产生了 {len(images)} 张图，以下是另一个模型写的描述；"
            f"这些图不会发到群里，要发用 show()]\n{described}"
        )
    except TaskNotConfigured:
        parts.append(f"[这次运行产生了 {len(images)} 张图，但没有能看图的模型，看不到内容]")
    return "\n".join(parts)


# --- bot tools ------------------------------------------------------------------


@_tool("sandbox_put")
async def sandbox_put(ctx: RunContext[Deps], handle: str) -> str:
    """把聊天里的文件或图片（file#N / img#N 句柄）放进工作区的 /workspace/in/，返回路径。
    要在工作区里处理群文件或图片时先调用它；不会自动上传附件。"""
    session = _session(ctx)
    with _sandbox_errors():
        await session.require()
    h = _handle(ctx, handle, "file", "image")
    cfg = ctx.deps.settings.tools
    blob, name = await media_bytes(ctx.deps.api, ctx.deps.group_id, h, cfg.file_max_mb * 1024 * 1024)
    with _sandbox_errors():
        path = await session.put_bytes(f"in/{safe_name(name, handle.replace('#', ''))}", blob)
    return f"{handle} 已放到 {path}（{len(blob)} 字节）"


def _decode_image(blob: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(blob)) as img:
            img.verify()
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise ToolError("这个文件不是能发的图片（支持 PNG、JPEG、GIF、WebP 等）") from e
    return fit_image(blob)


def needs_image_check(settings: Settings) -> bool:
    """Workspace images follow the group's output moderation switch."""
    return settings.moderation.enabled and settings.moderation.check_output


async def attach_image(deps: Deps, blob: bytes, caption: str, *, check: Any = check_image) -> str:
    """Moderate (when the group moderates output) and queue one image for the reply's forward message."""
    reserve_images(deps, 1)
    try:
        image = await asyncio.to_thread(_decode_image, blob)
        if needs_image_check(deps.settings):
            verdict: ImageVerdict = await check(image)
            if verdict.model:
                deps.vision_tokens.append((verdict.model, verdict.input_tokens, verdict.output_tokens))
            if not verdict.safe:
                reason = "审核没通过" if verdict.category != "error" else "审核出错"
                raise ToolError(f"这张图{reason}（{verdict.category}），不能发；不要在正文里描述它的内容")
    finally:
        deps.images_reserved -= 1
    deps.images.append((image, caption.strip()))
    left = MAX_IMAGES - len(deps.images)
    return (
        f"已附上图 {len(deps.images)}（{caption.strip() or '无标题'}），会附在合并转发里，正文不用再重复图里的内容。"
        f"这次回复还能再发 {left} 张图。"
    )


@_tool("open_workspace")
async def open_workspace(
    ctx: RunContext[Deps], name: str | None = None, new: bool = False, description: str = ""
) -> str:
    """选择这次要用的工作区。每个话题一个工作区（各自独立的 Linux 环境），sh、py、ws_read_file、ws_edit_file、
    sandbox_put、background 都作用在选中的那个上，用它们之前先选。
    - 不传 name：列出本群的工作区（名字、说明、存在多久、闲置多久、还能保留多久、公开链接、后台任务）。
    - name=现有的名字：继续用它（同一话题的后续请求用原来的那个）。同时传 description 会更新它的说明。
    - name=新名字, new=True, description=说明：为新话题新建一个空工作区。名字用小写字母、数字和 -（如
      pelican-game）；description 一行（≤200 字），写清放了什么、属于哪个话题。本群工作区满了时，会回收最久没用、
      没有后台任务的那个（文件和公开链接一起没了）。
    一次回复里可以切换：切走时之前那个里面用 & 起的临时进程会被清理，文件、kernel 和公开链接保留。"""
    session = _session(ctx)
    if name is None or not name.strip():
        if new:
            raise ToolError("new=True 时要用 name 写新工作区的名字")
        with _sandbox_errors():
            return await session.listing()
    with _sandbox_errors():
        opened = await session.open(name.strip().lower(), new=new, description=description)
        return await session.summary(opened)


@_tool("reset_workspace")
async def reset_workspace(ctx: RunContext[Deps]) -> str:
    """清空当前选中的工作区（文件、装好的依赖、kernel、公开链接全部清掉），名字和说明不变，下次使用时从空白的
    新环境开始。只在群友明确要求重置、或工作区已经坏掉时用。"""
    session = _session(ctx)
    with _sandbox_errors():
        return await session.reset()


def bot_tools() -> list[Tool[Deps]]:
    """``open_workspace``, ``sh`` / ``py``, the bot's own workspace tools and the background-job tools. Tools that
    switch or empty the selected workspace run alone, never overlapping another call in the same turn."""
    from yggdrasil.agent.job_tools import job_tools

    fns: list[Callable[..., Awaitable[ToolResult]]] = [sh, py, sandbox_put]
    return [
        Tool(open_workspace, takes_ctx=True, sequential=True),
        *(Tool(fn, takes_ctx=True) for fn in fns),
        Tool(reset_workspace, takes_ctx=True, sequential=True),
        *job_tools(),
    ]
