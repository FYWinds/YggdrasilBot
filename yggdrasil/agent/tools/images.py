"""Images the bot sends: the reply's image slots and their moderation, ``attach_image`` (workspace ``show()``,
job reports), the ``render_*`` tools and ``generate_image``."""

import asyncio
import io
from typing import Any, Literal

from PIL import Image
from pydantic_ai import RunContext

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.media import compact_image, fit_image, load_image, media_type
from yggdrasil.agent.tools.base import chat_tool, resolve_ref
from yggdrasil.config import APP_CONFIG
from yggdrasil.delivery.send import send_image
from yggdrasil.llm.registry import ImageError, generate_images
from yggdrasil.moderation.image import ImageVerdict, check_image
from yggdrasil.settings import Settings
from yggdrasil.tools import render as renderer
from yggdrasil.tools.errors import ToolError

MAX_IMAGES = 4  # images per reply, shared by render_* and the workspace's show()


def reserve_images(deps: Deps, wanted: int) -> int:
    """Hold up to ``wanted`` free image slots before awaiting; release with ``deps.images_reserved -= held``.

    Tool calls in one turn run concurrently, so checking len(deps.images) before an await could overfill the reply.
    """
    free = MAX_IMAGES - len(deps.images) - deps.images_reserved
    if free <= 0:
        if deps.images_reserved:
            raise ToolError("剩下的图片名额正被同时进行的出图占用，等它完成后再试")
        raise ToolError(f"这次回复已经有 {MAX_IMAGES} 张图了")
    held = min(wanted, free)
    deps.images_reserved += held
    return held


def _decode_image(blob: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(blob)) as img:
            img.verify()
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise ToolError("这个文件不是能发的图片（支持 PNG、JPEG、GIF、WebP 等）") from e
    return fit_image(blob)


def needs_image_check(settings: Settings) -> bool:
    """Images the bot sends (workspace show, generated) follow the group's output moderation switch."""
    return settings.moderation.enabled and settings.moderation.check_output


async def screen_image(deps: Deps, blob: bytes, *, check: Any = None) -> bytes:
    """An image the bot is about to send: decoded, fitted, and moderated when the group moderates output."""
    image = await asyncio.to_thread(_decode_image, blob)
    if needs_image_check(deps.settings):
        verdict: ImageVerdict = await (check or check_image)(image)
        if verdict.model:
            deps.vision_tokens.append((verdict.model, verdict.input_tokens, verdict.output_tokens))
        if not verdict.safe:
            reason = "审核没通过" if verdict.category != "error" else "审核出错"
            raise ToolError(f"这张图{reason}（{verdict.category}），不能发；不要在正文里描述它的内容")
    return image


async def attach_image(
    deps: Deps, blob: bytes, caption: str, *, check: Any = check_image, call_id: str | None = None
) -> str:
    """Moderate (when the group moderates output) and queue one image for the reply's forward message."""
    reserve_images(deps, 1)
    try:
        image = await screen_image(deps, blob, check=check)
    finally:
        deps.images_reserved -= 1
    deps.images.append((image, caption.strip()))
    deps.notes.shown(call_id, [(image, caption.strip())])
    left = MAX_IMAGES - len(deps.images)
    return (
        f"已附上图 {len(deps.images)}（{caption.strip() or '无标题'}），会附在合并转发里，正文不用再重复图里的内容。"
        f"这次回复还能再发 {left} 张图。"
    )


async def _attach_render(ctx: RunContext[Deps], kind: renderer.Kind, source: str, caption: str) -> str:
    if not APP_CONFIG.render_url:
        raise ToolError("没有配置渲染服务")
    deps = ctx.deps
    # Only Markdown can come back as several pages; mermaid/html always use one slot.
    held = reserve_images(deps, MAX_IMAGES if kind == "markdown" else 1)
    try:
        pngs, warnings = await renderer.render(kind, source, base_url=APP_CONFIG.render_url, max_images=held)
    finally:
        deps.images_reserved -= held
    pngs = pngs[:held]
    caption = caption.strip()
    first = len(deps.images) + 1
    rendered: list[tuple[bytes, str]] = []
    for i, png in enumerate(pngs, 1):
        page = caption if i == 1 else (f"{caption}（{i}/{len(pngs)}）" if caption else "")
        deps.images.append((png, page))
        rendered.append((png, page))
    deps.notes.shown(ctx.tool_call_id, rendered)
    numbers = f"图 {first}" if len(pngs) == 1 else f"图 {first}–{first + len(pngs) - 1}（共 {len(pngs)} 页）"
    result = f"已生成{numbers}（{caption or '无标题'}），会附在合并转发里，正文不用再重复图里的内容。"
    if warnings:
        lines = "\n".join(f"- {w}" for w in warnings)
        result += f"\n渲染时有问题（图已经加上了，需要的话改好内容后重新生成）：\n{lines}"
    return result


@chat_tool("render_mermaid")
async def render_mermaid(ctx: RunContext[Deps], code: str, caption: str = "") -> str:
    """把 Mermaid 代码渲染成图片，附在合并转发里发出。支持所有 Mermaid 图类型：flowchart、sequenceDiagram、
    classDiagram、stateDiagram、erDiagram、gantt、mindmap、timeline、pie、sankey、xychart 等。
    节点文字用中文即可，含特殊字符的文字用双引号包起来。语法错误会返回报错和出错的行，改好后重试。"""
    return await _attach_render(ctx, "mermaid", code, caption)


@chat_tool("render_markdown")
async def render_markdown(ctx: RunContext[Deps], markdown: str, caption: str = "") -> str:
    """把 Markdown 按 GitHub 风格排版渲染成图片，附在合并转发里发出。支持 GFM 表格、代码高亮、
    数学公式 $…$ / $$…$$、提示块 > [!NOTE]、任务列表、脚注，```mermaid 代码块会直接画成图。
    内容长会自动分成多页，最多用掉这次回复剩下的图片名额（总共 4 张）；放不下会报错，删减后重试。
    适合对比表、代码、带格式的长内容这类纯文本消息里不好看的东西。"""
    return await _attach_render(ctx, "markdown", markdown, caption)


@chat_tool("render_html")
async def render_html(ctx: RunContext[Deps], html: str, caption: str = "") -> str:
    """把一段 HTML 渲染成一张图片，附在合并转发里发出。只能用内联 style，不能用脚本，也不能引用外部图片、
    字体或链接。Markdown 表达不了的排版（卡片、色块、复杂表格）再用它。"""
    return await _attach_render(ctx, "html", html, caption)


MAX_REFERENCES = 4
type AspectRatio = Literal["auto", "1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9"]


@chat_tool("generate_image")
async def generate_image(
    ctx: RunContext[Deps],
    prompt: str,
    references: list[str] | None = None,
    aspect_ratio: AspectRatio = "auto",
    text: str = "",
) -> str:
    """画图：按描述生成一张图，或参照群里的图改图。画好后直接作为一条图片消息回复到群里，不进合并转发。
    prompt 写清画面内容、风格、构图和图里要出现的文字；用户说得简单时按他的意思补全细节，不要改变意图。
    references 是参考图（最多 4 张）：img#N 句柄、图片格式的 file#N，或聊天记录里的消息编号 #N（有多张时 #N/2 取第 2
    张）；“把这张图改成……”时传被引用的那张。aspect_ratio 是画幅，用户没要求就用 auto。text 是随图发的一句话，可以留空。
    生成要几十秒。发出后没有别的要说就选 mode=done，不要在合并转发里再描述这张图。"""
    deps = ctx.deps
    if len(references or []) > MAX_REFERENCES:
        raise ToolError(f"参考图最多 {MAX_REFERENCES} 张")
    refs: list[tuple[bytes, str]] = []
    for ref in references or []:
        h = await resolve_ref(ctx, ref, "image", "file")
        img = await load_image(deps.api, deps.group_id, h)
        deps.notes.image(img.data, img.content.media_type, segment=h.data)
        refs.append((img.data, img.content.media_type))
    try:
        result = await generate_images(
            prompt, references=refs, aspect_ratio=None if aspect_ratio == "auto" else aspect_ratio
        )
    except ImageError as e:
        raise ToolError(f"画图失败：{e}") from e
    deps.vision_tokens.append((result.model, result.input_tokens, result.output_tokens))
    pictures = [compact_image(await screen_image(deps, data)) for data, _ in result.images]
    caption = text.strip()
    # Recorded before sending, so the transcript shows what was made (and paid for) even if QQ refuses it.
    for picture in pictures:
        deps.notes.shown(ctx.tool_call_id, [(picture, caption)], mime=media_type(picture))
    cost = f"（花费 ${result.cost:.3f}）" if result.cost is not None else ""
    try:
        message_id = await send_image(
            deps.api,
            group_id=deps.group_id,
            user_id=deps.user_id,
            reply_to=deps.trigger_message_id,
            images=pictures,
            text=caption,
            self_id=deps.self_id,
            nickname=deps.bot_nickname,
        )
    except Exception as e:
        raise ToolError(f"图画好了{cost}，但发到群里失败：{type(e).__name__}: {str(e)[:200]}") from e
    if message_id is None:
        deps.actions.append("image (unconfirmed)")
        return f"图已经发出{cost}，QQ 没有及时确认，但大概率已经发到群里了，不要重发。没有别的要说就选 mode=done。"
    deps.actions.append(f"image {message_id}")
    return f"图已经发到群里了{cost}。没有别的要说就选 mode=done。"
