"""Short persona-voiced notices from the quick_ack model (currently: the pending-review notice)."""

from pydantic_ai import Agent

from yggdrasil.llm.registry import chat_model
from yggdrasil.llm.streaming import drain_events
from yggdrasil.prompts import render
from yggdrasil.settings import Settings


async def review_notice(request: str, settings: Settings) -> str:
    resolved = chat_model("quick_ack")
    agent = Agent(resolved.model, name="review_notice")
    result = await agent.run(
        render("review_notice", persona=settings.persona, request=request[:800]),
        model_settings=resolved.settings,
        event_stream_handler=drain_events,
    )
    return result.output.strip()[:60]
