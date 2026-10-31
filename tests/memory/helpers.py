from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from itertools import count

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from yggdrasil.memory.search import MessageView

T0 = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)  # 12:00 Asia/Shanghai
_ids = count(1)

type Respond = Callable[[list[ModelMessage], AgentInfo], ModelResponse]


def mv(
    minute: float, text: str = "消息", *, user: int = 10001, name: str = "甲", mid: int | None = None
) -> MessageView:
    i = mid if mid is not None else next(_ids)
    return MessageView(i, i, T0 + timedelta(minutes=minute), user, name, text, None, False)


def fake_model(respond: Respond) -> FunctionModel:
    """Agents stream every request; replay ``respond``'s response as one chunk per part."""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        for i, part in enumerate(respond(messages, info).parts):
            if isinstance(part, ToolCallPart):
                yield {i: DeltaToolCall(name=part.tool_name, json_args=part.args_as_json_str())}
            elif isinstance(part, TextPart):
                yield part.content

    return FunctionModel(respond, stream_function=stream)


def prompt_of(messages: list[ModelMessage]) -> str:
    return "".join(str(getattr(p, "content", "")) for msg in messages for p in msg.parts)
