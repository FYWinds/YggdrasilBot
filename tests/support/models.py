"""Scripted models for agents."""

from collections.abc import AsyncIterator, Callable

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

type Respond = Callable[[list[ModelMessage], AgentInfo], ModelResponse]


def streaming_model(respond: Respond) -> FunctionModel:
    """A model answering each request with ``respond``'s response; agents stream every request, so it comes back as
    one chunk per part."""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        for i, part in enumerate(respond(messages, info).parts):
            if isinstance(part, ToolCallPart):
                yield {i: DeltaToolCall(name=part.tool_name, json_args=part.args_as_json_str())}
            elif isinstance(part, TextPart):
                yield part.content

    return FunctionModel(respond, stream_function=stream)


def prompt_of(messages: list[ModelMessage]) -> str:
    """Everything the model was sent, as one string."""
    return "".join(str(getattr(p, "content", "")) for msg in messages for p in msg.parts)
