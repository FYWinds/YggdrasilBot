"""Tools that read what was sent in QQ: ``read_file``, ``view_image``, ``expand_forward``."""

import asyncio
from datetime import datetime
from typing import Any

from pydantic_ai import BinaryContent, RunContext, ToolReturn

from yggdrasil.agent.deps import Deps, Handle
from yggdrasil.agent.media import ModelImage, is_image, load_image, media_bytes, media_type, model_image
from yggdrasil.agent.tools.base import ToolResult, chat_tool, people_for, resolve_ref, vision
from yggdrasil.delivery.forward import META_TITLE
from yggdrasil.ingest.normalize import normalize
from yggdrasil.llm.registry import TaskNotConfigured, sees_images
from yggdrasil.people import clean_name
from yggdrasil.transcript.schema import ForwardNode, QQForwardDisplay


@chat_tool("read_file")
async def read_file(ctx: RunContext[Deps], handle: str) -> ToolResult:
    """读取文件内容（PDF/Office/文本/代码等），参数是 file#N 句柄，或聊天记录里的消息编号 #N（那条消息里的文件）。
    扫描版 PDF：能看图的模型直接收到页面图片。图片文件按看图处理。"""
    from yggdrasil.tools.docs import parse_document

    h = await resolve_ref(ctx, handle, "file")
    cfg = ctx.deps.settings.tools
    blob, name = await media_bytes(ctx.deps.api, ctx.deps.group_id, h, cfg.file_max_mb * 1024 * 1024)
    if is_image(blob):  # a picture sent as a group file
        return await _show_image(ctx, handle, h, await asyncio.to_thread(model_image, blob), "")
    doc = await asyncio.to_thread(parse_document, blob, name, max_pages=cfg.file_max_pages)
    note = "（内容已截断）" if doc.truncated else ""
    header = f"文件：{name}（{doc.kind}，{doc.pages} 页）{note}"
    if doc.scanned_pages and sees_images(ctx.deps.task):
        pages: list[Any] = []
        for i, page in enumerate(doc.scanned_pages, 1):
            pages += [f"第 {i} 页：", BinaryContent(data=page, media_type=media_type(page))]
        return ToolReturn(
            return_value=f"{header}\n\n扫描版，没有文字层；前 {len(doc.scanned_pages)} 页的图片附在下一条消息里，"
            "直接看图。",
            content=pages,
        )
    text = doc.text
    if doc.scanned_pages:
        pages = [ModelImage(page) for page in doc.scanned_pages]
        text = await vision(ctx, pages, "这是扫描版文档的页面，请按顺序完整转写文字，保留结构。")
    return f"{header}\n\n{text}"


@chat_tool("view_image")
async def view_image(ctx: RunContext[Deps], handle: str, question: str = "") -> ToolResult:
    """查看图片，可附带具体问题。handle 是 img#N 句柄、图片格式的 file#N（当群文件发的图），或聊天记录里的消息编号
    #N（那条消息里的图或文件；有多个时用 #N/2 取第 2 个），所以群里之前发过的图也能看。能看图的模型会直接收到原图，
    否则返回文字描述和转写。"""
    h = await resolve_ref(ctx, handle, "image", "file")
    return await _show_image(ctx, handle, h, await load_image(ctx.deps.api, ctx.deps.group_id, h), question)


async def _show_image(ctx: RunContext[Deps], ref: str, h: Handle, img: ModelImage, question: str) -> ToolResult:
    ctx.deps.notes.image(img.data, img.content.media_type, segment=h.data)
    if sees_images(ctx.deps.task):
        return ToolReturn(
            return_value=f"{ref} 的原图附在下一条消息里，直接看图。",
            content=[f"{ref}{img.note}：", img.content],
        )
    try:
        return "（以下是另一个模型写的图片描述）\n" + await vision(ctx, [img], question)
    except TaskNotConfigured:
        result = await ctx.deps.api.ocr_image(image=str(h.data.get("url") or h.data.get("file")))
        return "（无视觉模型，以下为 OCR 结果）\n" + "\n".join(t.text for t in result.texts)


@chat_tool("expand_forward")
async def expand_forward(ctx: RunContext[Deps], handle: str) -> str:
    """展开合并转发（聊天记录），参数是 fwd#N 句柄，或聊天记录里的消息编号 #N（那条合并转发）。嵌套的转发也会展开。"""
    h = await resolve_ref(ctx, handle, "forward")
    # (indent, time, sender id, sender name, segments, handle keys); rendered after one batched name lookup
    nodes: list[tuple[str, str, int, str, list[dict[str, Any]], list[str]]] = []
    shown: list[ForwardNode] = []  # the same nodes for the console

    async def walk(forward_id: str, depth: int, content: list | None) -> None:
        if depth > 3 or len(nodes) > 400:
            return
        records = content
        if records is None:
            records = [m.model_dump() for m in (await ctx.deps.api.get_forward_msg(id=forward_id)).messages]
        for rec in records:
            if rec.get("type") == "node":  # OneBot node format (our own stored forwards)
                rec = rec.get("data") or {}
            sender = rec.get("sender") or {}
            segs = rec.get("message") or rec.get("content") or []
            if isinstance(segs, str):
                segs = [{"type": "text", "data": {"text": segs}}]
            norm = normalize(segs)
            if norm.text.startswith(META_TITLE + "\n"):  # run metadata, kept in our older forward records
                continue
            ts = datetime.fromtimestamp(rec["time"]).strftime("%m-%d %H:%M") if rec.get("time") else ""
            name = sender.get("card") or sender.get("nickname") or rec.get("nickname") or ""
            uid = str(sender.get("user_id") or rec.get("user_id") or "")
            keys: list[str] = []
            nodes.append(("  " * depth, ts, int(uid) if uid.isdigit() else 0, str(name), segs, keys))
            sent_at = datetime.fromtimestamp(rec["time"]).astimezone() if rec.get("time") else None
            shown.append(
                ForwardNode(
                    depth=depth,
                    user_id=int(uid) if uid.isdigit() else 0,
                    sender_name=str(name),
                    sent_at=sent_at,
                    segments=segs,
                )
            )
            keys += [k for att in norm.attachments if (k := ctx.deps.handles.add(att.kind, att.data))]
            for att in norm.attachments:
                if att.kind == "forward":
                    inner = att.data.get("content")
                    await walk(str(att.data.get("id", "")), depth + 1, inner if isinstance(inner, list) else None)

    inner = h.data.get("content")
    await walk(str(h.data.get("id", "")), 0, inner if isinstance(inner, list) else None)
    ctx.deps.notes.show(ctx.tool_call_id, QQForwardDisplay(nodes=shown))
    people = await people_for(ctx, [n[2] for n in nodes] + [u for n in nodes for u in normalize(n[4]).mentions])
    lines = []
    for indent, ts, uid, name, segs, keys in nodes:
        who = people.who(uid, name) if uid else clean_name(name) or "?"
        text = normalize(segs, people.mention).text
        when = f"[{ts}] " if ts else ""
        lines.append(f"{indent}{when}{who}: {text}" + (f" ({', '.join(keys)})" if keys else ""))
    return "\n".join(lines) or "（空的合并转发）"
