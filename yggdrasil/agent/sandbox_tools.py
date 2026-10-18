"""The sandbox workspace suite for chat: harness command/file tools plus the bot's own workspace tools.

The group's persistent workspace (``yggdrasil.sandbox``) is attached lazily by the backend on the first
command or file operation, so a run that never touches it costs nothing. The suite is switched on per
group by the pseudo tool name ``sandbox`` in ``tools.enabled`` and globally by ``tools.sandbox_enabled``
(global settings only). Harness file tools get the ``ws_`` prefix: the bot's own ``read_file`` reads QQ
attachments.
"""

import asyncio
import io
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from PIL import Image
from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.capabilities.abstract import ValidatedToolArgs, WrapToolExecuteHandler
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai_harness import FileSystem, Shell, ToolOutputLimits
from pydantic_ai_harness.filesystem import DEFAULT_TOOL_NAMES
from pydantic_ai_harness.tool_output_limits import READ_TOOL_NAME, Band, Spill, Truncate, TruncationStrategy

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.media import fit_image, media_bytes
from yggdrasil.agent.tools import MAX_IMAGES, ToolResult, _handle, _tool, sandbox_requested
from yggdrasil.config import APP_CONFIG
from yggdrasil.core.metrics import TOOL_CALLS, TOOL_DURATION
from yggdrasil.moderation.image import ImageVerdict, check_image
from yggdrasil.sandbox import SandboxError, Session
from yggdrasil.settings import Settings, ToolSettings
from yggdrasil.tools.errors import ToolError

WS_PREFIX = "ws"
_FS_TOOLS = (*DEFAULT_TOOL_NAMES, "grep")
FS_TOOL_NAMES = tuple(f"{WS_PREFIX}_{name}" for name in _FS_TOOLS)
HARNESS_TOOL_NAMES = ("run_command", *FS_TOOL_NAMES, READ_TOOL_NAME)

SEND_IMAGE_MAX_BYTES = 10 * 1024 * 1024
WORKSPACE_ROOT = "/workspace"


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


def capabilities(cfg: ToolSettings) -> list[AbstractCapability[Deps]]:
    """Shell (``run_command`` only: background processes go through ``serve``), prefixed file tools and
    output limits scoped to the sandbox tools (large output spills into the workspace and is paged back
    with ``read_tool_result``)."""
    tail = Truncate(max_chars=4_000, strategy=TruncationStrategy.tail)
    head = Truncate(max_chars=4_000, strategy=TruncationStrategy.head)
    search = [Band(over=20_000, action=Spill(then=head)), Band(over=8_000, action=head)]
    per_tool = {
        "run_command": [Band(over=20_000, action=Spill(then=tail)), Band(over=4_000, action=tail)],
        **{f"{WS_PREFIX}_{name}": search for name in ("grep", "search_files", "find_files", "list_directory")},
    }
    return [
        _HarnessAccounting(),
        Shell[Deps](
            denied_commands=[],
            tools=["run_command"],
            default_timeout=float(cfg.sandbox_command_timeout_s),
            max_output_chars=100_000,
        ),
        FileSystem[Deps](tools=_FS_TOOLS, max_read_chars=20_000).prefix_tools(WS_PREFIX),
        ToolOutputLimits[Deps](bands=[], per_tool=per_tool, tool_filter=list(per_tool)),
    ]


# --- bot tools ------------------------------------------------------------------


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


_UNSAFE_NAME = re.compile(r"[^\w.\-]+")


def safe_name(name: str, fallback: str) -> str:
    """A plain file name for ``/workspace/in/``: no directories, no shell-hostile characters."""
    base = _UNSAFE_NAME.sub("_", PurePosixPath(name.replace("\\", "/")).name).strip("._")[:120]
    return base or fallback


def workspace_rel(path: str) -> str:
    """``/workspace/out/a.png`` or ``out/a.png`` -> ``out/a.png``; anything outside the workspace is refused."""
    p = PurePosixPath(path.strip())
    if p.is_absolute():
        try:
            p = p.relative_to(WORKSPACE_ROOT)
        except ValueError:
            raise ToolError(f"只能发工作区（{WORKSPACE_ROOT}）里的文件") from None
    if not p.parts or ".." in p.parts:
        raise ToolError(f"路径不对：{path}")
    return str(p)


@_tool("sandbox_put")
async def sandbox_put(ctx: RunContext[Deps], handle: str) -> str:
    """把聊天里的文件或图片（file#N / img#N 句柄）放进工作区的 /workspace/in/，返回路径。
    要在工作区里处理群文件或图片时先调用它；不会自动上传附件。"""
    session = _session(ctx)
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
    if len(deps.images) >= MAX_IMAGES:
        raise ToolError(f"这次回复已经有 {MAX_IMAGES} 张图了，不能再发")
    image = await asyncio.to_thread(_decode_image, blob)
    if needs_image_check(deps.settings):
        verdict: ImageVerdict = await check(image)
        if verdict.model:
            deps.vision_tokens.append((verdict.model, verdict.input_tokens, verdict.output_tokens))
        if not verdict.safe:
            reason = "审核没通过" if verdict.category != "error" else "审核出错"
            raise ToolError(f"这张图{reason}（{verdict.category}），不能发；不要在正文里描述它的内容")
    deps.images.append((image, caption.strip()))
    left = MAX_IMAGES - len(deps.images)
    return (
        f"已附上图 {len(deps.images)}（{caption.strip() or '无标题'}），会附在合并转发里，正文不用再重复图里的内容。"
        f"这次回复还能再发 {left} 张图。"
    )


@_tool("send_image")
async def send_image(ctx: RunContext[Deps], path: str, caption: str = "") -> str:
    """把工作区里的一张图片（PNG/JPEG/GIF/WebP，最大 10MB）附在这次回复的合并转发里发出。
    path 是工作区里的路径（如 /workspace/out/chart.png）。和 render_* 共用每次回复 4 张图的上限。"""
    session = _session(ctx)
    if len(ctx.deps.images) >= MAX_IMAGES:
        raise ToolError(f"这次回复已经有 {MAX_IMAGES} 张图了，不能再发")
    rel = workspace_rel(path)
    with _sandbox_errors():
        blob = await session.get_bytes(rel, max_bytes=SEND_IMAGE_MAX_BYTES)
    return await attach_image(ctx.deps, blob, caption)


@_tool("serve")
async def serve(ctx: RunContext[Deps], command: str, port: int) -> str:
    """在工作区后台启动一个 Web 服务并生成公网链接（https://<随机id>.<域名>），返回链接。
    command 是启动命令（在 /workspace 下执行），服务必须监听 0.0.0.0:port。链接任何拿到它的人都能打开，
    工作区回收或 unserve 后失效。"""
    session = _session(ctx)
    if not 1 <= port <= 65535:
        raise ToolError("端口要在 1-65535 之间")
    with _sandbox_errors():
        link = await session.serve(command, port)
    return f"已公开端口 {link.port}：{link.url}\n这个链接任何人都能打开，回复里直接给出链接。"


@_tool("unserve")
async def unserve(ctx: RunContext[Deps], port: int) -> str:
    """撤销某个端口的公网链接并停掉对应的服务。"""
    session = _session(ctx)
    with _sandbox_errors():
        closed = await session.unserve(port)
    return f"端口 {port} 的链接已撤销" if closed else f"端口 {port} 没有公开的链接"


@_tool("reset_workspace")
async def reset_workspace(ctx: RunContext[Deps]) -> str:
    """销毁本群的工作区（文件、装好的依赖、公开链接全部清掉），下次使用时从空白的新工作区开始。
    只在群友明确要求重置、或工作区已经坏掉时用。"""
    session = _session(ctx)
    with _sandbox_errors():
        return await session.reset()


def bot_tools() -> list[Tool[Deps]]:
    """The bot's own workspace tools; public links (serve/unserve) only when the deployment has a link domain."""
    fns: list[Callable[..., Awaitable[ToolResult]]] = [sandbox_put, send_image, reset_workspace]
    if APP_CONFIG.sandbox_link_domain:
        fns += [serve, unserve]
    return [Tool(fn, takes_ctx=True) for fn in fns]
