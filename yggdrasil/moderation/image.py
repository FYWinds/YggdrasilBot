"""Image moderation for pictures the bot sends from its sandbox workspace (send_image).

There is no dedicated image guard model: the ``vision`` route classifies the image with a strict prompt.
Fails closed: any error or unparsable answer counts as unsafe.
"""

import re
from dataclasses import dataclass

from pydantic_ai import Agent, BinaryContent

from yggdrasil.agent.media import media_type
from yggdrasil.core.log import LOGGER
from yggdrasil.llm.registry import chat_model
from yggdrasil.llm.streaming import drain_events
from yggdrasil.prompts import render

_ANSWER = re.compile(r"^\s*(SAFE|UNSAFE)\b\s*:?\s*([a-z_]*)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ImageVerdict:
    safe: bool
    category: str = ""  # sexual | gore | extremism | personal_data | error
    model: str = ""  # route label, for token accounting ("" when no model was called)
    input_tokens: int = 0
    output_tokens: int = 0


def parse_answer(text: str) -> tuple[bool, str]:
    """``SAFE`` -> (True, ""); ``UNSAFE: gore`` -> (False, "gore"); anything else is unsafe ("error")."""
    m = _ANSWER.match(text)
    if m is None:
        return False, "error"
    if m.group(1).upper() == "SAFE":
        return True, ""
    return False, m.group(2).lower() or "unspecified"


async def check_image(blob: bytes) -> ImageVerdict:
    try:
        resolved = chat_model("vision")
    except Exception as e:
        LOGGER.warning("image moderation unavailable: {}", e)
        return ImageVerdict(False, "error")
    try:
        agent = Agent(resolved.model, instructions=render("image_moderation"), name="image_moderation")
        result = await agent.run(
            ["审核这张图片。", BinaryContent(data=blob, media_type=media_type(blob))],
            model_settings=resolved.settings,
            event_stream_handler=drain_events,
        )
    except Exception as e:
        LOGGER.warning("image moderation failed: {}", e)
        return ImageVerdict(False, "error", resolved.label)
    usage = result.usage
    safe, category = parse_answer(result.output)
    if category == "error":
        LOGGER.warning("image moderation gave no verdict: {!r}", result.output[:200])
    return ImageVerdict(safe, category, resolved.label, usage.input_tokens or 0, usage.output_tokens or 0)
