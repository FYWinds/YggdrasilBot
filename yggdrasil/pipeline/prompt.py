"""What the agent of a chat or tldr run is told: its instructions, and the user prompt with the images the request
is about attached up front."""

from datetime import datetime

from pydantic_ai.messages import UserContent

from yggdrasil.agent.context import BuiltContext
from yggdrasil.agent.media import load_images, looks_like_image
from yggdrasil.config import APP_CONFIG
from yggdrasil.core.telemetry import TRACER
from yggdrasil.llm.registry import sees_images
from yggdrasil.napcat.api import NapCatAPI
from yggdrasil.people import self_label
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot
from yggdrasil.settings import Settings, ToolSettings
from yggdrasil.transcript.notes import TraceNotes


async def inline_images(
    ctx: BuiltContext, api: NapCatAPI, group_id: int | None, handler: str, limit: int, notes: TraceNotes
) -> str | list[UserContent]:
    """Attach the images of the messages the request is about up front when the route can see them.

    They are almost always what the request is about ("总结一下" on a reply to three screenshots),
    and a view_image round trip per image costs a model turn.
    """
    focus = (ctx.handles.get(key) for key in ctx.focus)
    handles = [h for h in focus if h is not None and looks_like_image(h)][:limit]
    if not handles or not sees_images(handler):
        return ctx.prompt
    with TRACER.start_as_current_span("ygg.inline_images") as span:
        loaded = await load_images(api, group_id, handles)
        span.set_attribute("ygg.images", len(loaded))
    if not loaded:
        return ctx.prompt
    keys = "、".join(key for key, _ in loaded)
    prompt: list[UserContent] = [ctx.prompt, f"下面直接附上了 {keys} 的原图：直接看图，不用再调用 view_image。"]
    for key, img in loaded:
        handle = ctx.handles.get(key)
        notes.image(img.data, img.content.media_type, segment=handle.data if handle else None)
        prompt += [f"{key}{img.note}：", img.content]
    return prompt


def instructions(
    handler: str,
    settings: Settings,
    group_id: int | None,
    *,
    tools: list[str],
    sandbox: ToolSettings | None,
    report: bool,
    nonce: str,
) -> str:
    """The ``<handler>_system`` prompt. ``tools`` names what the run may call; ``sandbox`` is the settings of the
    workspace suite when the run has it. Nothing about the request goes here (see ``pipeline.session``)."""
    group_name = ""
    if group_id is not None:
        info = snapshot().groups.get(group_id)
        group_name = info.name if info else ""
    return render(
        f"{handler}_system",
        persona=settings.persona,
        group_name=group_name or "本群",
        now=now_text(),
        tools=tools,
        sandbox=sandbox,
        sandbox_links=bool(APP_CONFIG.sandbox_link_domain),
        report=report,
        style=render("style_rules"),
        data_rules=render(
            "data_rules",
            nonce=nonce,
            me=self_label(settings.persona.name),
            sandbox=sandbox is not None,
        ),
    )


def now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %A")
